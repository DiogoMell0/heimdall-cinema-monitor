"""Coleta da programação no Ingresso.com."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from heimdall.models import Profile, Snapshot, ValidationError
from heimdall.sources.ingresso import sessions_url
from heimdall.sources.snapshot import snapshot_from_envelope

DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CollectionError(RuntimeError):
    """Erro de coleta com categoria e prazo para nova tentativa."""

    def __init__(self, category: str, message: str, *, status: int | None = None,
                 retry_at: datetime | None = None):
        super().__init__(message)
        self.category = category
        self.status = status
        self.retry_at = retry_at


@dataclass(frozen=True)
class Collection:
    snapshot: Snapshot
    envelope: dict


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Uma mudança de endpoint exige revisão, não troca automática de destino.
        return None


def _retry_hint(value: str | None) -> str:
    if value:
        try:
            seconds = int(value)
            if seconds >= 0:
                return f" Aguarde pelo menos {seconds} segundos antes de outra consulta."
        except ValueError:
            try:
                instant = parsedate_to_datetime(value)
                if instant.utcoffset() is not None:
                    return f" Aguarde até {instant.isoformat()} antes de outra consulta."
            except (ValueError, TypeError, OverflowError):
                pass
    return " Aguarde antes de consultar novamente."


def _retry_at(value: str | None, now: datetime | None = None) -> datetime | None:
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        seconds = int(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            return instant.astimezone(timezone.utc) if instant.utcoffset() is not None else None
        except (ValueError, TypeError, OverflowError):
            return None
    if seconds < 0:
        return None
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _status_error(status: int, retry_after: str | None = None) -> CollectionError:
    if status in (401, 403):
        return CollectionError("acesso_recusado", f"HTTP {status}: acesso recusado pela API; revisar as condições de integração.", status=status)
    if status == 429:
        return CollectionError("limite_de_acesso", "HTTP 429: limite de acesso atingido." + _retry_hint(retry_after), status=status, retry_at=_retry_at(retry_after))
    if status == 404:
        return CollectionError("nao_encontrado", "HTTP 404: consulta não encontrada; não é possível concluir que não há sessões.", status=status)
    if 300 <= status < 400:
        return CollectionError("redirecionamento", f"HTTP {status}: redirecionamento não seguido; o endpoint precisa ser revisado.", status=status)
    return CollectionError("http", f"HTTP {status}: a API não retornou uma programação válida." + (_retry_hint(retry_after) if retry_after else ""), status=status, retry_at=_retry_at(retry_after))


def _read_body(response, max_bytes: int) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise CollectionError("resposta_invalida", "A API retornou uma codificação de conteúdo não suportada.")
    length = response.headers.get("Content-Length")
    expected_length = None
    if length is not None:
        try:
            expected_length = int(length)
        except ValueError as exc:
            raise CollectionError("resposta_invalida", "Content-Length inválido.") from exc
        if expected_length < 0:
            raise CollectionError("resposta_invalida", "Content-Length negativo.")
        if expected_length > max_bytes:
            raise CollectionError("resposta_grande", f"Resposta excede o limite de {max_bytes} bytes.")
    # O byte adicional detecta excesso mesmo sem Content-Length.
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise CollectionError("resposta_grande", f"Resposta excede o limite de {max_bytes} bytes.")
    if expected_length is not None and len(body) != expected_length:
        raise CollectionError("resposta_invalida", "Resposta incompleta: tamanho diferente do informado pela API.")
    return body


def collect(profile: Profile, *, timeout: float = DEFAULT_TIMEOUT,
            max_bytes: int = MAX_RESPONSE_BYTES, opener=None) -> Collection:
    """Coleta a programação em uma única requisição."""
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValidationError("O timeout deve ser maior que zero e no máximo 60 segundos.")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValidationError("O limite de resposta deve ser um inteiro positivo.")
    url = sessions_url(profile)
    request = Request(url, headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "User-Agent": "Heimdall/0.6 (local session monitor)",
    }, method="GET")
    client = opener if opener is not None else build_opener(_NoRedirect())
    started = monotonic()
    try:
        with client.open(request, timeout=timeout) as response:
            status = response.status
            if status not in (200, 204):
                raise _status_error(status, response.headers.get("Retry-After"))
            if response.geturl() != url:
                raise CollectionError("resposta_invalida", "A resposta veio de uma URL diferente da consulta solicitada.")
            body = _read_body(response, max_bytes)
            if status == 204:
                if body:
                    raise CollectionError("resposta_invalida", "HTTP 204 inesperadamente acompanhado de conteúdo.")
                payload = []
            else:
                content_type = response.headers.get_content_type()
                if content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
                    raise CollectionError("resposta_invalida", "A API não retornou JSON; a resposta pode ser uma página de erro ou bloqueio.")
                try:
                    payload = json.loads(body.decode("utf-8-sig"))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise CollectionError("resposta_invalida", "JSON inválido ou incompleto retornado pela API.") from exc
    except HTTPError as exc:
        failure = _status_error(exc.code, exc.headers.get("Retry-After") if exc.headers else None)
        exc.close()
        raise failure from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise CollectionError("timeout", "A API não respondeu dentro do timeout configurado.") from exc
        raise CollectionError("rede", "Não foi possível conectar à API. Verifique internet, DNS e conexão HTTPS.") from exc
    except TimeoutError as exc:
        raise CollectionError("timeout", "A API não respondeu dentro do timeout configurado.") from exc
    except (OSError, HTTPException) as exc:
        raise CollectionError("rede", "A conexão com a API falhou ou foi interrompida durante a leitura.") from exc

    captured_at = datetime.now(timezone.utc).astimezone(profile.starts_at.tzinfo)
    envelope = {
        "source": url, "checkedAt": captured_at.isoformat(), "status": status,
        "bytes": len(body), "elapsedMs": round((monotonic() - started) * 1000),
        "data": payload,
    }
    try:
        snapshot = snapshot_from_envelope(envelope, profile)
    except ValidationError as exc:
        raise CollectionError("dados_invalidos", f"Programação inconclusiva: {exc}") from exc
    return Collection(snapshot=snapshot, envelope=envelope)
