"""Perfil, sessão e resultado da coleta."""

from dataclasses import dataclass
from datetime import date, datetime


class ValidationError(ValueError):
    """Perfil ou programação com dados inválidos."""


def parse_instant(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field}: esperado um instante em texto com fuso.")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}: data/hora inválida.") from exc
    if instant.utcoffset() is None:
        raise ValidationError(f"{field}: o fuso horário é obrigatório.")
    return instant


@dataclass(frozen=True)
class Profile:
    movie_id: str
    movie_name: str
    city_id: str
    city_name: str
    starts_at: datetime
    ends_before: datetime
    certification: str
    language: str


@dataclass(frozen=True)
class Session:
    id: str
    movie_id: str
    city_id: str
    theater_id: str
    theater_name: str
    room: str
    programming_date: date
    starts_at: datetime
    displayed_at: datetime
    labels: frozenset[str]
    sale_enabled: bool
    purchase_url: str


@dataclass(frozen=True)
class Snapshot:
    captured_at: datetime
    published_dates: tuple[date, ...]
    sessions: tuple[Session, ...]
