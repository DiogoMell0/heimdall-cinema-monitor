"""Aplicação dos filtros e cobertura das datas do perfil."""

from dataclasses import dataclass
from datetime import date, timedelta

from heimdall.models import Profile, Session, Snapshot
from heimdall.rules import rejection_reasons


@dataclass(frozen=True)
class Analysis:
    matching: tuple[Session, ...]
    pending_dates: tuple[date, ...]


def analyze(snapshot: Snapshot, profile: Profile) -> Analysis:
    # Online usa a nova coleta; offline reproduz o instante da captura salva.
    matching = tuple(session for session in snapshot.sessions
                     if not rejection_reasons(session, profile, now=snapshot.captured_at))
    pending: list[date] = []
    day = profile.starts_at.date()
    last = (profile.ends_before - timedelta(microseconds=1)).date()
    while day <= last:
        if day not in snapshot.published_dates:
            pending.append(day)
        day += timedelta(days=1)
    return Analysis(matching=matching, pending_dates=tuple(pending))
