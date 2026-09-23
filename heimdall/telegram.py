"""Cliente da Telegram Bot API."""

from dataclasses import dataclass, field
from http.client import HTTPException
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TelegramError(ValueError):
    """Falha de envio ou configuração com mensagem própria para o terminal."""

    def __init__(self, message: str, *, uncertain: bool = False, retry_after: int = 60):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_after = retry_after


def validate_token(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,200}", token):
        raise TelegramError("Formato de token inválido. Copie o token fornecido pelo BotFather.")
    return token


def private_chat_id(value) -> int:
    if type(value) is not int or not 0 < value < 2**52:
        raise TelegramError("O destino precisa ser uma conversa privada válida.")
    return value


@dataclass(frozen=True)
class TelegramCredentials:
    token: str = field(repr=False)
    chat_id: int
    bot_username: str

    def __post_init__(self):
        validate_token(self.token)
        private_chat_id(self.chat_id)
        if not isinstance(self.bot_username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", self.bot_username):
            raise TelegramError("Nome de usuário do bot inválido.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 20, opener=None):
        self._token = validate_token(token)
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise TelegramError("O timeout deve ser maior que zero e no máximo 60 segundos.")
        self._timeout = timeout
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def _call(self, method: str, payload: dict):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "sendMessage"}:
            raise TelegramError("Método Telegram não permitido.")
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Heimdall (local Telegram integration)",
        }, method="POST")
        try:
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code if isinstance(response, HTTPError) else response.status
                if 300 <= status < 400 or response.geturl() != url:
                    raise TelegramError("Redirecionamento inesperado do Telegram; requisição interrompida.", uncertain=method == "sendMessage")
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise TelegramError("Resposta do Telegram excedeu o limite de tamanho.", uncertain=method == "sendMessage")
                try:
                    data = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise TelegramError("O Telegram retornou uma resposta inválida.", uncertain=method == "sendMessage") from None
                if not isinstance(data, dict):
                    raise TelegramError("O Telegram retornou uma resposta inválida.", uncertain=method == "sendMessage")
                if status != 200 or data.get("ok") is not True:
                    code = data.get("error_code")
                    if type(code) is not int:
                        raise TelegramError("O Telegram retornou um código de erro inválido.", uncertain=method == "sendMessage")
                    # Só recusas explícitas e coerentes permitem repetir sem confirmação.
                    rejected = (data.get("ok") is False and code in (400, 401, 403, 409, 429)
                                and status in (200, code))
                    if not rejected:
                        raise TelegramError("Resposta de erro do Telegram sem confirmação confiável.", uncertain=method == "sendMessage")
                    if code == 429:
                        parameters = data.get("parameters")
                        retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
                        wait = f" Aguarde {retry} segundos." if type(retry) is int and 0 < retry <= 86400 else " Aguarde antes de tentar novamente."
                        raise TelegramError("Limite de envio do Telegram atingido." + wait, retry_after=retry if type(retry) is int and retry > 0 else 60)
                    messages = {
                        401: "Token recusado pelo Telegram. Confira ou renove o token no BotFather.",
                        403: "Acesso recusado. Confira se você iniciou a conversa e não bloqueou o bot.",
                        409: "Existe outra integração recebendo mensagens deste bot. Use um bot exclusivo.",
                        400: "O Telegram recusou a requisição. Confira a configuração da conversa.",
                    }
                    raise TelegramError(messages[code])
                if "result" not in data:
                    raise TelegramError("Resposta do Telegram sem resultado.", uncertain=method == "sendMessage")
                return data["result"]
        except (URLError, TimeoutError, OSError, HTTPException):
            raise TelegramError("Falha de conexão com o Telegram. Se houve envio, o resultado é incerto; confira a conversa antes de repetir.", uncertain=method == "sendMessage") from None

    def bot_username(self) -> str:
        result = self._call("getMe", {})
        if (not isinstance(result, dict) or result.get("is_bot") is not True
                or type(result.get("id")) is not int or result["id"] != int(self._token.split(":")[0])):
            raise TelegramError("Identidade do bot não confirmada pelo Telegram.")
        username = result.get("username")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise TelegramError("O Telegram não informou um usuário válido para o bot.")
        return username

    def require_no_webhook(self):
        result = self._call("getWebhookInfo", {})
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise TelegramError("Não foi possível verificar a configuração do bot.")
        if result["url"]:
            raise TelegramError("Este bot tem outra integração ativa. Crie um bot exclusivo para o Heimdall.")

    def pairing_chat(self, code: str) -> int:
        result = self._call("getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message"]})
        if not isinstance(result, list):
            raise TelegramError("Resposta de vinculação inválida.")
        matches = set()
        for update in result:
            msg = update.get("message") if isinstance(update, dict) else None
            if not isinstance(msg, dict) or msg.get("text") != f"/start {code}":
                continue
            chat, sender = msg.get("chat"), msg.get("from")
            if (isinstance(chat, dict) and chat.get("type") == "private"
                    and isinstance(sender, dict) and sender.get("is_bot") is False
                    and sender.get("id") == chat.get("id")):
                matches.add(private_chat_id(chat.get("id")))
        if len(matches) != 1:
            raise TelegramError("Conversa não identificada de forma única. Execute a configuração novamente, abra o novo link e toque em Iniciar. Use um bot exclusivo.")
        return matches.pop()

    def send_text(self, chat_id: int, text: str) -> int:
        private_chat_id(chat_id)
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-16-le")) // 2 <= 4096:
            raise TelegramError("A mensagem precisa ter entre 1 e 4096 caracteres Telegram.")
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text,
            "link_preview_options": {"is_disabled": True}})
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                or result["message_id"] <= 0 or not isinstance(result.get("chat"), dict)
                or result["chat"].get("id") != chat_id or result["chat"].get("type") != "private"):
            raise TelegramError("Envio sem confirmação válida. Confira a conversa antes de repetir o teste.", uncertain=True)
        return result["message_id"]
