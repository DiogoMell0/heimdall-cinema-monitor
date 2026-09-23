"""Fila durável: reservar antes do envio e confirmar depois, sem rede em transações."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from heimdall.models import Profile, Session, ValidationError
from heimdall.locking import LockBusy, exclusive_lock
from heimdall.rules import rejection_reasons
from heimdall.storage import History, HistoryError, _utc, profile_key

CHANNELS = ("telegram",)


class DeliveryError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False, retry_after: int = 60):
        super().__init__(code)
        self.uncertain = uncertain
        self.retry_after = retry_after


@contextmanager
def notification_lock(database: Path):
    path = Path(str(database.resolve()) + ".notify.lock")
    try:
        with exclusive_lock(path):
            yield
    except LockBusy:
        raise HistoryError("Outra execução está cuidando dos avisos deste banco. Aguarde sua conclusão.") from None


def session_from_payload(data: dict) -> Session:
    return Session(
        id=data["id"], movie_id=data["movie_id"], city_id=data["city_id"],
        theater_id=data["theater_id"], theater_name=data["theater_name"], room=data["room"],
        programming_date=date.fromisoformat(data["programming_date"]),
        starts_at=datetime.fromisoformat(data["starts_at"]), displayed_at=datetime.fromisoformat(data["displayed_at"]),
        labels=frozenset(data["labels"]), sale_enabled=data["sale_enabled"], purchase_url=data["purchase_url"],
    )


def _text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_message(sessions: list[Session], profile: Profile, channel: str) -> str:
    title = "HEIMDALL — Novas sessões compatíveis"
    movie = _text(profile.movie_name, 140)
    header = f"{title}\n{movie}\n{_text(profile.city_name, 80)} · {_text(profile.certification, 80)} · {_text(profile.language, 40)}"
    entries = []
    for row in sessions:
        when = row.starts_at.astimezone(profile.starts_at.tzinfo)
        entries.append(f"{when:%d/%m/%Y às %H:%M}\n{_text(row.theater_name, 180)} · {_text(row.room, 100)}\n{row.purchase_url}")
    return header + "\n\n" + "\n\n".join(entries)


class DeliveryQueue:
    def __init__(self, history: History, profile: Profile):
        if history.dataset != "online":
            raise HistoryError("Avisos reais não podem usar um banco de reprodução offline.")
        self.history, self.profile = history, profile
        self.db, self.key = history.connection, profile_key(profile)

    def recover_interrupted(self, now: datetime):
        # O chamador possui o bloqueio do processo; nenhum envio anterior continua ativo.
        with self.history._transaction():
            self.db.execute("""UPDATE deliveries SET status='uncertain', updated_at=?
                WHERE status='sending' AND event_id IN (SELECT id FROM events WHERE profile_key=?)""", (_utc(now), self.key))
            self.db.execute("""UPDATE delivery_batches SET status='uncertain', completed_at=?, error_code='interrupted'
                WHERE profile_key=? AND status='sending'""", (_utc(now), self.key))

    def enqueue(self, channels, now: datetime):
        if not channels or any(channel not in CHANNELS for channel in channels):
            raise ValidationError("O único canal de avisos deste projeto é o Telegram.")
        with self.history._transaction():
            for channel in channels:
                self.db.execute("""INSERT OR IGNORE INTO deliveries(event_id, channel, status, available_at, updated_at)
                    SELECT id, ?, 'pending', ?, ? FROM events WHERE profile_key=?""", (channel, _utc(now), _utc(now), self.key))

    def claim(self, channel: str, target_key: str, run_id: int, now: datetime) -> dict | None:
        with self.history._transaction():
            latest = self.db.execute("SELECT * FROM runs WHERE profile_key=? ORDER BY id DESC LIMIT 1", (self.key,)).fetchone()
            if (not latest or latest["id"] != run_id or latest["status"] != "ok"
                    or not 0 <= (now - datetime.fromisoformat(latest["observed_at"])).total_seconds() <= 300):
                raise HistoryError("Envio exige a última consulta válida com no máximo cinco minutos. Execute verificar --avisar novamente.")
            rows = self.db.execute("""SELECT d.event_id, e.eligibility_version AS event_version,
                s.eligibility_version, s.eligible, s.last_run_id, s.payload_json
                FROM deliveries d JOIN events e ON e.id=d.event_id
                JOIN sessions s ON s.profile_key=e.profile_key AND s.session_id=e.session_id
                WHERE e.profile_key=? AND d.channel=? AND d.status='pending' AND d.available_at<=?
                ORDER BY e.id""", (self.key, channel, _utc(now))).fetchall()
            selected, sessions = [], []
            for row in rows:
                session = session_from_payload(json.loads(row["payload_json"]))
                if (row["event_version"] != row["eligibility_version"] or not row["eligible"]
                        or rejection_reasons(session, self.profile, now=now)):
                    self.db.execute("UPDATE deliveries SET status='obsolete', updated_at=? WHERE event_id=? AND channel=?",
                                    (_utc(now), row["event_id"], channel))
                    continue
                if row["last_run_id"] != run_id:
                    continue  # Ausente agora: manter pendente, sem enviar a oferta antiga.
                candidate = format_message(sessions + [session], self.profile, channel)
                if len(candidate.encode("utf-16-le")) // 2 > 3800:
                    if not sessions:
                        raise DeliveryError("message_too_long")
                    break
                selected.append(row["event_id"])
                sessions.append(session)
                if len(sessions) >= 10:
                    break
            if not selected:
                return None
            batch = {"id": uuid4().hex, "message": format_message(sessions, self.profile, channel),
                     "channel": channel, "events": selected, "sessions": sessions}
            self.db.execute("""INSERT INTO delivery_batches(id, profile_key, run_id, channel, target_key, message, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'sending', ?)""", (batch["id"], self.key, run_id, channel, target_key, batch["message"], _utc(now)))
            self.db.executemany("""UPDATE deliveries SET status='sending', batch_id=?, attempts=attempts+1, updated_at=?
                WHERE event_id=? AND channel=? AND status='pending'""", [(batch["id"], _utc(now), event, channel) for event in selected])
            return batch

    def finish(self, batch_id: str, now: datetime, *, receipt: str | None = None, failure: DeliveryError | None = None):
        status = "sent" if failure is None else ("uncertain" if failure.uncertain else "failed")
        row_status = "pending" if status == "failed" else status
        available_at = now
        if status == "failed":
            try:
                available_at = now + timedelta(seconds=max(30, failure.retry_after))
            except OverflowError:
                # Uma espera impossível de representar não autoriza antecipar o envio.
                available_at = datetime.max.replace(tzinfo=timezone.utc)
        with self.history._transaction():
            changed = self.db.execute("""UPDATE delivery_batches SET status=?, completed_at=?, receipt=?, error_code=?
                WHERE id=? AND profile_key=? AND status='sending'""", (status, _utc(now), receipt,
                str(failure) if failure else None, batch_id, self.key)).rowcount
            if changed != 1:
                raise HistoryError("Lote de envio não está reservado por esta operação.")
            self.db.execute("""UPDATE deliveries SET status=?, available_at=?, updated_at=?
                WHERE batch_id=? AND status='sending'""", (row_status, _utc(available_at), _utc(now), batch_id))

    def resolve(self, batch_id: str, action: str, now: datetime):
        if action not in ("confirmar", "tentar-novamente"):
            raise ValidationError("Ação de resolução inválida.")
        with self.history._transaction():
            batch = self.db.execute("SELECT status FROM delivery_batches WHERE id=? AND profile_key=?", (batch_id, self.key)).fetchone()
            if not batch or batch["status"] != "uncertain":
                raise HistoryError("Somente um lote de envio incerto deste perfil pode ser resolvido.")
            confirmed = action == "confirmar"
            self.db.execute("UPDATE delivery_batches SET status=?, receipt=?, error_code=?, completed_at=? WHERE id=?",
                            ("sent" if confirmed else "failed", "manual" if confirmed else None,
                             None if confirmed else "manual_retry", _utc(now), batch_id))
            self.db.execute("UPDATE deliveries SET status=?, available_at=?, updated_at=? WHERE batch_id=? AND status='uncertain'",
                            ("sent" if confirmed else "pending", _utc(now), _utc(now), batch_id))


def read_deliveries(database: Path, profile: Profile) -> dict:
    empty = {"counts": [], "batches": []}
    if not database.exists():
        return empty
    with History(database, read_only=True) as history:
        if history.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
            return empty
        key = profile_key(profile)
        history.connection.execute("BEGIN")
        counts = history.connection.execute("""SELECT channel, status, COUNT(*) AS total FROM deliveries
            JOIN events ON events.id=deliveries.event_id WHERE profile_key=? GROUP BY channel, status""", (key,)).fetchall()
        batches = history.connection.execute("SELECT * FROM delivery_batches WHERE profile_key=? ORDER BY created_at DESC LIMIT 20", (key,)).fetchall()
        history.connection.commit()
        return {"counts": [dict(row) for row in counts], "batches": [dict(row) for row in batches]}
