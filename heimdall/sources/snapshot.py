"""Leitura e gravação de capturas para análise offline."""

import json
from pathlib import Path

from heimdall.models import Profile, Snapshot, ValidationError, parse_instant
from heimdall.sources.ingresso import parse_sessions, sessions_url


def load_snapshot(path: Path, profile: Profile) -> Snapshot:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError("Captura JSON inválida ou incompleta.") from exc
    return snapshot_from_envelope(data, profile)


def snapshot_from_envelope(data: object, profile: Profile) -> Snapshot:
    if not isinstance(data, dict) or data.get("status") not in (200, 204):
        raise ValidationError("Captura sem confirmação HTTP 200/204; resultado inconclusivo.")
    if data["status"] == 204 and data.get("data") != []:
        raise ValidationError("HTTP 204 deve representar uma programação explicitamente vazia.")
    if data.get("source") != sessions_url(profile):
        raise ValidationError("A captura deve ser da consulta geral do filme e cidade configurados.")
    captured_at = parse_instant(data.get("checkedAt"), "checkedAt")
    dates, sessions = parse_sessions(data.get("data"), movie_id=profile.movie_id, city_id=profile.city_id)
    return Snapshot(captured_at=captured_at, published_dates=dates, sessions=sessions)


def save_envelope(path: Path, envelope: dict) -> None:
    """Guarda uma consulta para reprodução offline, sem sobrescrever arquivos."""
    serialized = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8") as file:
        file.write(serialized)
