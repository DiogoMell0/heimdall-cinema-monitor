"""Coordena coleta e histórico; a rede é acessada fora da transação SQLite."""

from dataclasses import dataclass
from pathlib import Path

from heimdall.models import Profile, Snapshot, ValidationError
from heimdall.sources.http import CollectionError, DEFAULT_TIMEOUT, collect
from heimdall.sources.snapshot import load_snapshot
from heimdall.storage import ChangeSet, History


@dataclass(frozen=True)
class TrackingResult:
    snapshot: Snapshot
    changes: ChangeSet


def check_online(profile: Profile, database: Path, *, timeout: float = DEFAULT_TIMEOUT) -> TrackingResult:
    with History(database, dataset="online") as history:
        try:
            collection = collect(profile, timeout=timeout)
            changes = history.record_success(collection.snapshot, profile)
        except CollectionError as exc:
            history.record_failure(profile, exc.category, str(exc))
            raise
        except ValidationError as exc:
            history.record_failure(profile, "validacao", str(exc))
            raise
    return TrackingResult(collection.snapshot, changes)


def register_capture(profile: Profile, capture: Path, database: Path) -> TrackingResult:
    with History(database, dataset="replay") as history:
        try:
            snapshot = load_snapshot(capture, profile)
            changes = history.record_success(snapshot, profile)
        except (OSError, ValidationError) as exc:
            history.record_failure(profile, "captura_invalida", str(exc))
            raise
    return TrackingResult(snapshot, changes)
