"""Conversão da resposta do Ingresso.com em sessões."""

from datetime import date
from urllib.parse import parse_qs, urlsplit

from heimdall.models import Profile, Session, ValidationError, parse_instant


def sessions_url(profile: Profile) -> str:
    """Monta a URL da programação por filme e cidade."""
    for value in (profile.city_id, profile.movie_id):
        if not value.isascii() or not value.isdigit():
            raise ValidationError("Os IDs de cidade e filme da API devem conter apenas dígitos.")
    return f"https://api-content.ingresso.com/v0/sessions/city/{profile.city_id}/event/{profile.movie_id}"


def _object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{field}: esperado um objeto.")
    return value


def _list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise ValidationError(f"{field}: esperada uma lista explícita.")
    return value


def _text(data: dict, key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{key}: texto obrigatório ausente ou inválido.")
    return value.strip()


def _enabled(data: dict) -> bool:
    value = data.get("enabled")
    if not isinstance(value, bool):
        raise ValidationError("enabled: esperado true ou false.")
    block = _text(data, "blockMessage", allow_empty=True)
    return value and not block


def parse_sessions(payload: object, *, movie_id: str, city_id: str) -> tuple[tuple[date, ...], tuple[Session, ...]]:
    days = _list(payload, "programação")
    published: set[date] = set()
    seen: set[str] = set()
    sessions: list[Session] = []
    for raw_day in days:
        day = _object(raw_day, "dia")
        try:
            programming_date = date.fromisoformat(_text(day, "date"))
        except ValueError as exc:
            raise ValidationError("Dia da programação inválido.") from exc
        if programming_date in published:
            raise ValidationError("Dia duplicado na resposta.")
        published.add(programming_date)
        for raw_theater in _list(day.get("theaters"), "theaters"):
            theater = _object(raw_theater, "cinema")
            theater_id, theater_name = _text(theater, "id"), _text(theater, "name")
            theater_enabled = _enabled(theater)
            for raw_room in _list(theater.get("rooms"), "rooms"):
                room = _object(raw_room, "sala")
                room_name = _text(room, "name")
                for raw_session in _list(room.get("sessions"), "sessions"):
                    item = _object(raw_session, "sessão")
                    session_id = _text(item, "id")
                    if session_id in seen:
                        raise ValidationError(f"ID de sessão duplicado: {session_id}.")
                    seen.add(session_id)
                    labels = _list(item.get("type"), "type")
                    if not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
                        raise ValidationError(f"Sessão {session_id}: características ausentes ou inválidas.")
                    starts_at = parse_instant(_object(item.get("realDate"), "realDate").get("localDate"), "realDate.localDate")
                    displayed_at = parse_instant(_object(item.get("date"), "date").get("localDate"), "date.localDate")
                    session_enabled = _enabled(item)
                    url = _text(item, "siteURL", allow_empty=True)
                    if url:
                        try:
                            parsed = urlsplit(url)
                        except ValueError as exc:
                            raise ValidationError(f"Sessão {session_id}: link de compra inválido.") from exc
                        if (parsed.scheme != "https" or parsed.netloc != "checkout.ingresso.com"
                                or parse_qs(parsed.query).get("sessionId") != [session_id]):
                            raise ValidationError(f"Sessão {session_id}: link de compra inesperado.")
                    sessions.append(Session(
                        id=session_id, movie_id=movie_id, city_id=city_id,
                        theater_id=theater_id, theater_name=theater_name, room=room_name,
                        programming_date=programming_date, starts_at=starts_at, displayed_at=displayed_at,
                        labels=frozenset(label.strip() for label in labels),
                        sale_enabled=theater_enabled and session_enabled,
                        purchase_url=url,
                    ))
    return tuple(sorted(published)), tuple(sessions)
