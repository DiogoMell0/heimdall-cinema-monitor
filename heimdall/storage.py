"""Histórico SQLite: uma transação reúne consulta, sessões e novidades."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from heimdall.models import Profile, Session, Snapshot, ValidationError
from heimdall.rules import rejection_reasons

SCHEMA_VERSION = 2

SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE runs (
        id INTEGER PRIMARY KEY, profile_key TEXT NOT NULL, profile_json TEXT NOT NULL,
        observed_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ok', 'failure')),
        error_category TEXT, error_message TEXT, snapshot_digest TEXT,
        published_dates_json TEXT, session_count INTEGER, matching_count INTEGER,
        new_session_count INTEGER, changed_session_count INTEGER, new_event_count INTEGER
    )""",
    "CREATE INDEX runs_by_profile ON runs(profile_key, id)",
    """CREATE TABLE sessions (
        profile_key TEXT NOT NULL, session_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
        eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
        eligibility_version INTEGER NOT NULL CHECK(eligibility_version >= 0),
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        last_run_id INTEGER NOT NULL REFERENCES runs(id),
        PRIMARY KEY(profile_key, session_id)
    )""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY, profile_key TEXT NOT NULL, session_id TEXT NOT NULL,
        eligibility_version INTEGER NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL CHECK(kind IN ('new_matching_session', 'became_matching')),
        detected_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        UNIQUE(profile_key, session_id, eligibility_version),
        FOREIGN KEY(profile_key, session_id) REFERENCES sessions(profile_key, session_id)
    )""",
    "CREATE INDEX events_by_profile ON events(profile_key, id)",
)

DELIVERY_SCHEMA = (
    """CREATE TABLE delivery_batches (
        id TEXT PRIMARY KEY, profile_key TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(id),
        channel TEXT NOT NULL CHECK(channel = 'telegram'), target_key TEXT NOT NULL,
        message TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('sending', 'sent', 'failed', 'uncertain')),
        created_at TEXT NOT NULL, completed_at TEXT, receipt TEXT, error_code TEXT
    )""",
    """CREATE TABLE deliveries (
        event_id INTEGER NOT NULL REFERENCES events(id),
        channel TEXT NOT NULL CHECK(channel = 'telegram'),
        status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'uncertain', 'obsolete')),
        batch_id TEXT REFERENCES delivery_batches(id), attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(event_id, channel)
    )""",
    "CREATE INDEX batches_by_profile ON delivery_batches(profile_key, created_at)",
)


class HistoryError(RuntimeError):
    """Falha ao abrir, validar ou atualizar o histórico."""


@dataclass(frozen=True)
class ChangeSet:
    run_id: int
    new_sessions: int
    changed_sessions: int
    new_events: tuple[dict, ...]


def default_database_path() -> Path:
    # AppData pode ser redirecionado por aplicativos empacotados no Windows.
    # Uma pasta dedicada no perfil mantém o caminho fora dessa virtualização.
    return Path.home() / ".heimdall-cinema-monitor" / "heimdall.sqlite3"


def _utc(instant: datetime) -> str:
    if instant.utcoffset() is None:
        raise ValidationError("O histórico exige instantes com fuso horário.")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def profile_definition(profile: Profile) -> dict:
    # Nomes de exibição não mudam a busca. Alterar filtros cria outro escopo.
    return {
        "source": "ingresso.com", "rules_version": 1,
        "movie_id": profile.movie_id, "city_id": profile.city_id,
        "starts_at": _utc(profile.starts_at), "ends_before": _utc(profile.ends_before),
        "certification": profile.certification.strip().casefold(),
        "language": profile.language.strip().casefold(),
    }


def profile_key(profile: Profile) -> str:
    return _digest(profile_definition(profile))


def _payload(session: Session) -> dict:
    return {
        "id": session.id, "movie_id": session.movie_id, "city_id": session.city_id,
        "theater_id": session.theater_id, "theater_name": session.theater_name,
        "room": session.room, "programming_date": session.programming_date.isoformat(),
        "starts_at": session.starts_at.isoformat(), "displayed_at": session.displayed_at.isoformat(),
        "labels": sorted(session.labels), "sale_enabled": session.sale_enabled,
        "purchase_url": session.purchase_url,
    }


def _fingerprint(session: Session) -> str:
    data = _payload(session)
    data.update(starts_at=_utc(session.starts_at), displayed_at=_utc(session.displayed_at),
                labels=sorted({label.strip().casefold() for label in session.labels}))
    return _digest(data)


class History:
    def __init__(self, path: Path, *, dataset: str = "online", read_only: bool = False):
        self.path = path.resolve()
        self.read_only = read_only
        self.connection = None
        if dataset not in ("online", "replay"):
            raise HistoryError("Modo de histórico inválido.")
        try:
            if read_only:
                target = self.path.as_uri() + "?mode=ro"
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                target = str(self.path)
            self.connection = sqlite3.connect(target, uri=read_only, timeout=5, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            if read_only:
                self._validate_schema()
            else:
                if self.connection.execute("PRAGMA user_version").fetchone()[0] == 1:
                    self._validate_schema()
                    if self.dataset != dataset:
                        raise HistoryError("Use bancos separados para consultas online e reprodução offline.")
                    self._backup_before_upgrade()
                with self._transaction():
                    version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                    tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if version == 0 and not tables:
                        for statement in SCHEMA:
                            self.connection.execute(statement)
                        self.connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)",
                                                    [("application", "heimdall"), ("dataset", dataset)])
                        self.connection.execute("PRAGMA user_version = 1")
                    self._validate_schema()
                    if self.dataset != dataset:
                        raise HistoryError("Este banco pertence a outro modo. Use bancos separados para consultas online e reprodução offline.")
                    if self.connection.execute("PRAGMA user_version").fetchone()[0] == 1:
                        for statement in DELIVERY_SCHEMA:
                            self.connection.execute(statement)
                        self.connection.execute("PRAGMA user_version = 2")
        except (OSError, sqlite3.Error, HistoryError) as exc:
            self.close()
            if isinstance(exc, HistoryError):
                raise
            raise HistoryError(f"Não foi possível abrir o histórico: {exc}") from exc

    def _validate_schema(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, SCHEMA_VERSION):
            raise HistoryError("Versão de banco não reconhecida; nenhum esquema será substituído.")
        metadata = dict(self.connection.execute("SELECT key, value FROM metadata").fetchall())
        if metadata.get("application") != "heimdall" or metadata.get("dataset") not in ("online", "replay"):
            raise HistoryError("O arquivo não é um histórico reconhecido do Heimdall.")
        self.dataset = metadata["dataset"]

    def _backup_before_upgrade(self):
        backup = Path(str(self.path) + ".before-v2.bak")
        try:
            with backup.open("xb"):
                pass
        except FileExistsError:
            # Preserva o backup da primeira tentativa se a migração for retomada.
            with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as saved:
                if saved.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise HistoryError("A cópia anterior à migração não é reconhecida; confira o arquivo .before-v2.bak.")
                if saved.execute("SELECT value FROM metadata WHERE key='application'").fetchone() != ("heimdall",):
                    raise HistoryError("A cópia anterior à migração não pertence ao Heimdall.")
            return
        try:
            destination = sqlite3.connect(backup)
            try:
                self.connection.backup(destination)
            finally:
                destination.close()
        except BaseException:
            backup.unlink(missing_ok=True)
            raise

    @contextmanager
    def _transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except BaseException as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            if isinstance(exc, sqlite3.Error):
                raise HistoryError(f"A gravação do histórico falhou; a transação foi revertida: {exc}") from exc
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()

    def record_success(self, snapshot: Snapshot, profile: Profile) -> ChangeSet:
        key, observed = profile_key(profile), _utc(snapshot.captured_at)
        rows = sorted(snapshot.sessions, key=lambda session: session.id)
        if len({row.id for row in rows}) != len(rows):
            raise ValidationError("IDs duplicados: o histórico não foi atualizado.")
        if any(row.movie_id != profile.movie_id or row.city_id != profile.city_id for row in rows):
            raise ValidationError("A captura não corresponde ao filme/cidade do perfil.")
        prepared = [(row, _payload(row), _fingerprint(row),
                     not rejection_reasons(row, profile, now=snapshot.captured_at)) for row in rows]
        dates = sorted(day.isoformat() for day in snapshot.published_dates)
        snapshot_digest = _digest({"dates": dates, "sessions": [(row.id, fingerprint) for row, _, fingerprint, _ in prepared]})
        events, new_count, changed_count = [], 0, 0
        with self._transaction():
            last = self.connection.execute(
                "SELECT observed_at, snapshot_digest FROM runs WHERE profile_key=? AND status='ok' ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            if last and (observed < last["observed_at"] or (observed == last["observed_at"] and snapshot_digest != last["snapshot_digest"])):
                raise ValidationError("Captura antiga ou conflitante com o mesmo horário; o último estado válido foi preservado.")
            cursor = self.connection.execute(
                """INSERT INTO runs(profile_key, profile_json, observed_at, recorded_at, status,
                   snapshot_digest, published_dates_json, session_count, matching_count,
                   new_session_count, changed_session_count, new_event_count)
                   VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?, 0, 0, 0)""",
                (key, _json(profile_definition(profile)), observed, _utc(datetime.now(timezone.utc)),
                 snapshot_digest, _json(dates), len(rows), sum(item[3] for item in prepared)),
            )
            run_id = cursor.lastrowid
            for session, payload, fingerprint, eligible in prepared:
                previous = self.connection.execute(
                    "SELECT * FROM sessions WHERE profile_key=? AND session_id=?", (key, session.id)
                ).fetchone()
                is_new = previous is None
                became_matching = eligible and (is_new or not previous["eligible"])
                version = (previous["eligibility_version"] if previous else 0) + int(became_matching)
                new_count += int(is_new)
                changed_count += int(not is_new and (previous["fingerprint"] != fingerprint or bool(previous["eligible"]) != eligible))
                self.connection.execute(
                    """INSERT INTO sessions(profile_key, session_id, payload_json, fingerprint, eligible,
                       eligibility_version, first_seen_at, last_seen_at, last_run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(profile_key, session_id) DO UPDATE SET
                       payload_json=excluded.payload_json, fingerprint=excluded.fingerprint,
                       eligible=excluded.eligible, eligibility_version=excluded.eligibility_version,
                       last_seen_at=excluded.last_seen_at, last_run_id=excluded.last_run_id""",
                    (key, session.id, _json(payload), fingerprint, int(eligible), version,
                     observed, observed, run_id),
                )
                if became_matching:
                    kind = "new_matching_session" if is_new else "became_matching"
                    event_cursor = self.connection.execute(
                        """INSERT INTO events(profile_key, session_id, eligibility_version, run_id,
                           kind, detected_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (key, session.id, version, run_id, kind, observed, _json(payload)),
                    )
                    events.append({"id": event_cursor.lastrowid, "kind": kind, "session_id": session.id,
                                   "detected_at": observed, "payload": payload})
            # Ausência em uma coleta não apaga sessão nem altera sua compatibilidade conhecida.
            self.connection.execute(
                "UPDATE runs SET new_session_count=?, changed_session_count=?, new_event_count=? WHERE id=?",
                (new_count, changed_count, len(events), run_id),
            )
        return ChangeSet(run_id, new_count, changed_count, tuple(events))

    def record_failure(self, profile: Profile, category: str, message: str) -> None:
        now = _utc(datetime.now(timezone.utc))
        with self._transaction():
            self.connection.execute(
                """INSERT INTO runs(profile_key, profile_json, observed_at, recorded_at, status,
                   error_category, error_message) VALUES (?, ?, ?, ?, 'failure', ?, ?)""",
                (profile_key(profile), _json(profile_definition(profile)), now, now, category, message),
            )

    def summary(self, profile: Profile, *, limit: int = 10) -> dict:
        if not 1 <= limit <= 100:
            raise ValidationError("O limite do histórico deve estar entre 1 e 100.")
        key = profile_key(profile)
        try:
            # Uma leitura consistente mesmo se outro processo terminar uma gravação.
            self.connection.execute("BEGIN")
            latest = self.connection.execute("SELECT * FROM runs WHERE profile_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            success = self.connection.execute("SELECT * FROM runs WHERE profile_key=? AND status='ok' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            counts = self.connection.execute("SELECT COUNT(*), SUM(status='failure') FROM runs WHERE profile_key=?", (key,)).fetchone()
            known = self.connection.execute("SELECT COUNT(*) FROM sessions WHERE profile_key=?", (key,)).fetchone()[0]
            total_events = self.connection.execute("SELECT COUNT(*) FROM events WHERE profile_key=?", (key,)).fetchone()[0]
            events = self.connection.execute("SELECT * FROM events WHERE profile_key=? ORDER BY id DESC LIMIT ?", (key, limit)).fetchall()
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise HistoryError(f"Não foi possível ler o histórico: {exc}") from exc
        return {
            "dataset": self.dataset, "profile_key": key, "known_sessions": known,
            "runs": counts[0], "failures": counts[1] or 0, "event_count": total_events,
            "latest_run": dict(latest) if latest else None,
            "last_success": dict(success) if success else None,
            "events": [{**dict(event), "payload": json.loads(event["payload_json"])} for event in events],
        }


def read_history(path: Path, profile: Profile, *, limit: int = 10) -> dict | None:
    if not path.exists():
        return None
    with History(path, read_only=True) as history:
        return history.summary(profile, limit=limit)
