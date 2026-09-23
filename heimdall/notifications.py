"""Consulta manual com avisos e comandos para acompanhar/resolver as entregas."""

from datetime import datetime, timezone
from pathlib import Path
import sys

from heimdall.config import load_profile
from heimdall.delivery import CHANNELS, DeliveryError, DeliveryQueue, notification_lock, read_deliveries
from heimdall.models import ValidationError
from heimdall.notifiers import make_notifier
from heimdall.sources.http import DEFAULT_TIMEOUT
from heimdall.storage import History, HistoryError, default_database_path
from heimdall.tracking import check_online


def utc_now():
    return datetime.now(timezone.utc)


def verify_and_notify(profile, database: Path, *, channels=CHANNELS, timeout=DEFAULT_TIMEOUT,
                      factory=make_notifier, clock=utc_now):
    if not channels or any(channel not in CHANNELS for channel in channels):
        raise ValidationError("O único canal de avisos deste projeto é o Telegram.")
    results = []
    with notification_lock(database):
        with History(database) as history:
            DeliveryQueue(history, profile).recover_interrupted(clock())
        result = check_online(profile, database, timeout=timeout)
        with History(database) as history:
            queue = DeliveryQueue(history, profile)
            queue.enqueue(channels, clock())
            for channel in dict.fromkeys(channels):
                pending = history.connection.execute("""SELECT COUNT(*) FROM deliveries d JOIN events e ON e.id=d.event_id
                    WHERE e.profile_key=? AND d.channel=? AND d.status='pending'""", (queue.key, channel)).fetchone()[0]
                if not pending:
                    continue
                try:
                    notifier = factory(channel)
                except DeliveryError as exc:
                    results.append({"channel": channel, "status": "configuration_error", "error": str(exc)})
                    continue
                for _ in range(100):
                    try:
                        batch = queue.claim(channel, notifier.target_key, result.changes.run_id, clock())
                    except DeliveryError as exc:
                        results.append({"channel": channel, "status": "pending", "error": str(exc)})
                        break
                    if batch is None:
                        break
                    try:
                        receipt = notifier.send(batch)
                    except DeliveryError as exc:
                        queue.finish(batch["id"], clock(), failure=exc)
                        results.append({"channel": channel, "batch": batch["id"],
                                        "status": "uncertain" if exc.uncertain else "pending", "error": str(exc)})
                        break
                    except Exception:
                        # Não imprimir exceções de terceiros, que podem conter credenciais.
                        queue.finish(batch["id"], clock(), failure=DeliveryError("unexpected_sender_error", uncertain=True))
                        results.append({"channel": channel, "batch": batch["id"], "status": "uncertain", "error": "unexpected_sender_error"})
                        break
                    queue.finish(batch["id"], clock(), receipt=receipt)
                    results.append({"channel": channel, "batch": batch["id"], "status": "sent", "events": len(batch["events"])})
    return result, results


def print_results(results):
    if not results:
        print("Nenhum aviso novo enviado. Use avisos historico para conferir pendências anteriores.")
    for item in results:
        if item["status"] == "sent":
            print(f"{item['channel']}: envio confirmado pelo canal, {item['events']} novidade(s) | lote {item['batch']}")
        else:
            print(f"{item['channel']}: {item['status']} | {item['error']} | lote {item.get('batch', 'não reservado')}")


def run_command(args):
    database = args.banco if args.banco is not None else default_database_path()
    try:
        profile = load_profile(args.perfil)
        if args.notice_command == "resolver":
            if not database.exists():
                raise HistoryError("O banco de avisos ainda não existe.")
            with notification_lock(database), History(database) as history:
                queue = DeliveryQueue(history, profile)
                queue.recover_interrupted(utc_now())
                queue.resolve(args.lote, args.acao, utc_now())
            print("Lote atualizado. Reenvio autorizado será reavaliado na próxima execução de verificar --avisar.")
            return 0
        summary = read_deliveries(database, profile)
        print("HEIMDALL | Entregas por canal")
        for row in summary["counts"]:
            print(f"{row['channel']}: {row['status']} = {row['total']}")
        for batch in summary["batches"]:
            print(f"Lote {batch['id']} | {batch['channel']} | {batch['status']} | {batch['created_at']} | {batch['error_code'] or 'sem erro'}")
            if batch["status"] == "uncertain":
                print(batch["message"])
        if not summary["counts"]:
            print("Nenhuma entrega registrada.")
        return 0
    except (DeliveryError, HistoryError, ValidationError) as exc:
        print(f"Falha nos avisos: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Falha ao acessar o armazenamento local de avisos.", file=sys.stderr)
        return 1
