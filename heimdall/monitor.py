"""Agendamento, execução de ciclos e estado do monitor."""

from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile

from heimdall.config import load_profile
from heimdall.delivery import DeliveryError, read_deliveries
from heimdall.locking import LockBusy, exclusive_lock
from heimdall.models import ValidationError, parse_instant
from heimdall.monitor_feedback import format_feedback, summarize
from heimdall.notifications import utc_now, verify_and_notify
from heimdall.notifiers import make_notifier
from heimdall.sources.http import CollectionError
from heimdall.storage import History, HistoryError, profile_key
from heimdall.telegram_setup import load_credentials

BLOCKING_ERRORS = {"acesso_recusado", "redirecionamento", "dados_invalidos", "resposta_invalida"}


class MonitorError(ValueError):
    pass


def default_directory() -> Path:
    return Path.home() / ".heimdall-cinema-monitor"


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise MonitorError("Arquivo do monitor excedeu o limite de tamanho.")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise MonitorError("Versão do arquivo do monitor não reconhecida.")
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MonitorError("Arquivo do monitor inválido; nenhum estado foi substituído.") from None


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="monitor-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _log(directory: Path, now: datetime, event: str, **fields):
    # Uma lista fixa de campos evita gravar credenciais vindas de exceções HTTP.
    handler = RotatingFileHandler(directory / "monitor.log", maxBytes=512 * 1024,
                                  backupCount=3, encoding="utf-8")
    try:
        message = json.dumps({"at": _stamp(now), "event": event, **fields}, ensure_ascii=False)
        handler.emit(logging.LogRecord("heimdall", logging.INFO, "", 0, message, (), None))
    finally:
        handler.close()


def load_config(directory: Path) -> dict:
    path = directory / "monitor.json"
    if not path.exists():
        raise MonitorError("Monitor não configurado. Execute monitor configurar primeiro.")
    config = _read(path)
    if type(config.get("paused")) is not bool or type(config.get("interval_minutes")) is not int:
        raise MonitorError("Configuração de pausa ou intervalo inválida.")
    if not 1 <= config["interval_minutes"] <= 1440:
        raise MonitorError("O intervalo deve estar entre 1 e 1440 minutos.")
    config.setdefault("cycle_feedback", False)
    if type(config["cycle_feedback"]) is not bool:
        raise MonitorError("Configuração de feedback inválida.")
    for name in ("profile", "database"):
        value = config.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute() or any(ord(c) < 32 for c in value):
            raise MonitorError("O monitor exige caminhos absolutos válidos.")
    if not isinstance(config.get("profile_key"), str):
        raise MonitorError("Identidade do perfil ausente na configuração.")
    parse_instant(config.get("anchor_at"), "anchor_at")
    profile = load_profile(Path(config["profile"]))
    if profile_key(profile) != config["profile_key"]:
        raise MonitorError("O perfil mudou. Desative a tarefa e use outro diretório de controle para o novo perfil.")
    if config.get("ends_before") != profile.ends_before.isoformat():
        raise MonitorError("O fim do agendamento não corresponde ao perfil.")
    return config


def _state(directory: Path, config: dict) -> dict:
    path = directory / "monitor-state.json"
    if not path.exists():
        return {"version": 1, "profile_key": config["profile_key"], "database": config["database"],
                "status": "never_run", "consecutive_failures": 0, "failure_notice_attempted": False,
                "blocked": False, "cycles": 0}
    state = _read(path)
    if state.get("profile_key") != config["profile_key"] or state.get("database") != config["database"]:
        raise MonitorError("O estado do monitor pertence a outro perfil ou banco.")
    for name in ("consecutive_failures", "cycles"):
        if type(state.get(name)) is not int or state[name] < 0:
            raise MonitorError("Contadores inválidos no estado do monitor.")
    for name in ("failure_notice_attempted", "blocked"):
        if type(state.get(name)) is not bool:
            raise MonitorError("Estado de controle do monitor inválido.")
    for name in ("last_tick", "last_started", "last_finished", "last_success", "next_due", "feedback_retry_at"):
        if state.get(name) is not None:
            parse_instant(state[name], name)
    return state


def configure(directory: Path, profile_path: Path, database: Path, interval: int, *, now=None) -> dict:
    now = now or utc_now()
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise MonitorError("O intervalo deve estar entre 1 e 1440 minutos.")
    profile = load_profile(profile_path)
    if profile.ends_before <= now:
        raise MonitorError("O período deste perfil já terminou.")
    config = {"version": 1, "profile": str(profile_path.resolve()), "database": str(database.resolve()),
              "profile_key": profile_key(profile), "interval_minutes": interval, "paused": True,
              "cycle_feedback": False,
              "ends_before": profile.ends_before.isoformat(),
              "anchor_at": _stamp(now.replace(microsecond=0) + timedelta(minutes=2))}
    with exclusive_lock(directory / "monitor.lock"):
        if (directory / "monitor.json").exists():
            old = load_config(directory)
            if not old["paused"]:
                raise MonitorError("Pause o monitor antes de alterar sua configuração.")
            if any(old[key] != config[key] for key in ("profile", "database", "profile_key")):
                raise MonitorError("Use outro diretório de controle para um novo perfil ou banco.")
            config["cycle_feedback"] = old["cycle_feedback"]
        # A preparação usa somente a configuração; a migração ocorre no primeiro ciclo.
        _write(directory / "monitor.json", config)
        _log(directory, now, "configured_paused", interval_minutes=interval)
    return config


def set_feedback(directory: Path, enabled: bool, *, now=None):
    if type(enabled) is not bool:
        raise MonitorError("Configuração de feedback inválida.")
    load_config(directory)
    with exclusive_lock(directory / "monitor.lock"):
        config = load_config(directory)
        config["cycle_feedback"] = enabled
        _write(directory / "monitor.json", config)
        _log(directory, now or utc_now(), "feedback_configured", enabled=enabled)


def set_paused(directory: Path, paused: bool, *, now=None):
    now = now or utc_now()
    with exclusive_lock(directory / "monitor.lock"):
        config = load_config(directory)
        state = _state(directory, config)
        if not paused:
            profile = load_profile(Path(config["profile"]))
            if now >= profile.ends_before:
                raise MonitorError("O período já terminou; este monitor não pode ser retomado.")
            state.update(blocked=False, consecutive_failures=0, failure_notice_attempted=False)
            state["status"] = "ready"
            # Preservar next_due: pausa/retomada não antecipa Retry-After.
            _write(directory / "monitor-state.json", state)
        config["paused"] = paused
        _write(directory / "monitor.json", config)
        _log(directory, now, "paused" if paused else "resumed")


def _next_slot(config: dict, now: datetime) -> datetime:
    anchor = parse_instant(config["anchor_at"], "anchor_at")
    interval = timedelta(minutes=config["interval_minutes"])
    slots = max(0, (now - anchor) // interval + 1)
    return anchor + interval * slots


def _health_notice(directory, state, now, message, sender):
    # Grava a reserva antes do envio para evitar duplicatas após uma interrupção.
    state["last_health_notice"] = "sending"
    _write(directory / "monitor-state.json", state)
    try:
        if sender is None:
            make_notifier("telegram").send({"message": message})
        else:
            sender(message)
    except Exception:
        state["last_health_notice"] = "unconfirmed"
    else:
        state["last_health_notice"] = "sent"
    _write(directory / "monitor-state.json", state)
    _log(directory, now, "health_notice", status=state["last_health_notice"])


def _warn_if_needed(directory, state, now, sender):
    if not (state["blocked"] or state["consecutive_failures"] >= 3) or state["failure_notice_attempted"]:
        return
    state["failure_notice_attempted"] = True
    _health_notice(directory, state, now,
        "HEIMDALL — ATENÇÃO AO MONITOR\n"
        + ("As consultas foram interrompidas e precisam de conferência.\n" if state["blocked"] else
           "Houve três ou mais ciclos consecutivos com falha.\n")
        + "Categoria: " + state["last_error"]
        + "\nConfira monitor status e o log local. Isso não significa ausência de sessões.", sender)


def _cycle_feedback(directory, config, state, profile, now, sender):
    if not config["cycle_feedback"]:
        return
    previous = state.get("last_cycle_feedback", {})
    if previous.get("cycle") == state["cycles"]:
        return
    notice = {"cycle": state["cycles"], "at": _stamp(now), "status": "sending"}
    state["last_cycle_feedback"] = notice
    if state.get("feedback_retry_at") and now < parse_instant(state["feedback_retry_at"], "feedback_retry_at"):
        notice["status"] = "deferred"
    else:
        message = format_feedback(state, profile)
        # Uma reserva persistida por ciclo; falha de envio não refaz a consulta.
        _write(directory / "monitor-state.json", state)
        try:
            if sender is None:
                make_notifier("telegram").send({"message": message})
            else:
                sender(message)
        except DeliveryError as exc:
            notice["status"] = "unconfirmed" if exc.uncertain else "failed"
            try:
                due = now + timedelta(seconds=max(0, exc.retry_after))
            except OverflowError:
                due = datetime.max.replace(tzinfo=timezone.utc)
            state["feedback_retry_at"] = _stamp(due)
        except Exception:
            notice["status"] = "unconfirmed"
        else:
            notice["status"] = "sent"
            state.pop("feedback_retry_at", None)
    _write(directory / "monitor-state.json", state)
    _log(directory, now, "cycle_feedback", cycle=state["cycles"], status=notice["status"])


def execute(directory: Path, *, clock=utc_now, verifier=None, health_sender=None, feedback_sender=None) -> int:
    # Ler antes de criar o bloqueio, para não criar pastas em comandos sem configuração.
    load_config(directory)
    with exclusive_lock(directory / "monitor.lock"):
        config = load_config(directory)
        profile = load_profile(Path(config["profile"]))
        state = _state(directory, config)
        now = clock()
        state["last_tick"] = _stamp(now)
        notice = state.get("last_cycle_feedback", {})
        if notice.get("status") == "sending":
            notice["status"] = "unconfirmed"
            _log(directory, now, "cycle_feedback_interrupted", cycle=notice["cycle"])
        if state["status"] == "running":
            state.update(status="interrupted", last_error="interrupted")
            state["consecutive_failures"] += 1
            _log(directory, now, "previous_cycle_interrupted")
        if now >= profile.ends_before:
            state["status"] = "completed"
            _write(directory / "monitor-state.json", state)
            return 0
        if config["paused"] or state["blocked"]:
            _write(directory / "monitor-state.json", state)
            return 0
        if state.get("next_due") and now < parse_instant(state["next_due"], "next_due"):
            _write(directory / "monitor-state.json", state)
            return 0
        _warn_if_needed(directory, state, now, health_sender)
        previous = state.get("last_started")
        if previous:
            gap = (now - parse_instant(previous, "last_started")).total_seconds()
            missed = max(0, int(gap // (config["interval_minutes"] * 60)) - 1)
            if missed:
                _log(directory, now, "coverage_gap", estimated_missed_cycles=missed)
        state.update(status="running", last_started=_stamp(now), cycles=state["cycles"] + 1, cycle_summary=None,
                     next_due=_stamp(_next_slot(config, now)))
        _write(directory / "monitor-state.json", state)
        _log(directory, now, "cycle_started", cycle=state["cycles"])
        error, retry_at, blocked = None, None, False
        try:
            verifier = verifier or verify_and_notify
            result, deliveries = verifier(profile, Path(config["database"]))
            state["cycle_summary"] = summarize(result, profile, deliveries)
            state["last_collection"] = _stamp(result.snapshot.captured_at)
            counts = read_deliveries(Path(config["database"]), profile)["counts"]
            uncertain = sum(row["total"] for row in counts if row["status"] in ("sending", "uncertain"))
            state.update(last_collection=_stamp(result.snapshot.captured_at),
                         sessions=len(result.snapshot.sessions), new_events=len(result.changes.new_events),
                         pending_deliveries=sum(row["total"] for row in counts if row["status"] == "pending"),
                         uncertain_deliveries=uncertain)
            state["cycle_summary"].update(pending_deliveries=state["pending_deliveries"],
                                          uncertain_deliveries=uncertain)
            if uncertain or any(item["status"] != "sent" for item in deliveries):
                error = "delivery_attention"
        except CollectionError as exc:
            # O estado usa categorias estáveis, independentes do texto da exceção.
            error = exc.category if exc.category in BLOCKING_ERRORS | {"rede", "timeout", "limite_de_acesso", "http", "nao_encontrado", "resposta_grande"} else "collection_error"
            blocked, retry_at = error in BLOCKING_ERRORS, exc.retry_at
        except (HistoryError, ValidationError, OSError):
            error = "local_storage_or_configuration"
            blocked = True
        except Exception:
            error, blocked = "unexpected_error", True
        finished = clock()
        state["last_finished"] = _stamp(finished)
        if error:
            state["consecutive_failures"] += 1
            delay = min(config["interval_minutes"] * 2 ** min(state["consecutive_failures"] - 1, 10),
                        max(360, config["interval_minutes"]))
            due = finished + timedelta(minutes=delay)
            if retry_at is not None:
                due = max(due, retry_at)
            state.update(status="blocked" if blocked else "failure", last_error=error,
                         blocked=blocked, next_due=_stamp(due))
            _write(directory / "monitor-state.json", state)
            _log(directory, finished, "cycle_failed", category=error, blocked=blocked,
                 consecutive_failures=state["consecutive_failures"], next_due=state["next_due"])
            _warn_if_needed(directory, state, finished, health_sender)
            _cycle_feedback(directory, config, state, profile, clock(), feedback_sender)
            return 1
        alert_attempted = state["failure_notice_attempted"]
        failures = state["consecutive_failures"]
        state.update(status="ok", last_success=_stamp(finished), last_error=None,
                     consecutive_failures=0, failure_notice_attempted=False, blocked=False,
                     next_due=_stamp(_next_slot(config, finished)))
        _write(directory / "monitor-state.json", state)
        _log(directory, finished, "cycle_ok", sessions=state["sessions"], new_events=state["new_events"],
             pending_deliveries=state["pending_deliveries"])
        if alert_attempted:
            _health_notice(directory, state, finished,
                f"HEIMDALL — MONITOR RECUPERADO\nA verificação voltou a funcionar após {failures} ciclo(s) com falha. "
                "Os avisos de novas sessões continuam sendo avaliados normalmente.", health_sender)
        _cycle_feedback(directory, config, state, profile, clock(), feedback_sender)
        return 0


def diagnose(directory: Path, *, now=None) -> dict:
    """Confere o acesso ao banco e à credencial no ambiente do Agendador."""
    now = now or utc_now()
    config = load_config(directory)
    report = {"version": 1, "at": _stamp(now), "status": "failure", "network_calls": 0,
              "executable": sys.executable, "working_directory": str(Path.cwd())}
    try:
        load_credentials()
        with History(Path(config["database"]), read_only=True) as history:
            if history.dataset != "online":
                raise MonitorError("O monitor exige um banco online.")
            if history.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MonitorError("Integridade do banco não confirmada.")
        report.update(status="ok", telegram_credential_readable=True, database_integrity="ok")
    except Exception:
        report["error"] = "credential_or_database_unavailable"
    _write(directory / "diagnostic.json", report)
    _log(directory, now, "diagnostic", status=report["status"])
    return report


def run_command(args):
    directory = args.diretorio.resolve()
    try:
        action = args.monitor_command
        if action == "configurar":
            config = configure(directory, args.perfil, args.banco or directory / "heimdall.sqlite3", args.intervalo)
            print(f"Monitor configurado e pausado. Intervalo: {config['interval_minutes']} minutos.")
        elif action in ("pausar", "retomar"):
            set_paused(directory, action == "pausar")
            print("Controle local atualizado. Use scripts/monitor.ps1 para também atualizar a tarefa do Windows.")
        elif action == "feedback":
            set_feedback(directory, args.feedback_action == "ativar")
            print("Feedback por consulta no Telegram: " + ("ativado." if args.feedback_action == "ativar" else "desativado."))
        elif action == "executar":
            return execute(directory)
        elif action == "diagnosticar":
            result = diagnose(directory)
            print(f"Diagnóstico sem rede: {result['status']}. Relatório: {directory / 'diagnostic.json'}")
            return int(result["status"] != "ok")
        else:
            config = load_config(directory)
            state = _state(directory, config)
            profile = load_profile(Path(config["profile"]))
            mode = "período encerrado" if utc_now() >= profile.ends_before else (
                "pausado" if config["paused"] else ("interrompido: precisa de conferência" if state["blocked"] else "liberado"))
            print(f"HEIMDALL | Monitor local: {mode} | Intervalo: {config['interval_minutes']} minutos")
            print(f"Último disparo: {state.get('last_tick', 'nenhum')} | Resultado: {state['status']}")
            print(f"Última consulta válida: {state.get('last_collection', 'nenhuma')}")
            print(f"Último ciclo sem falhas: {state.get('last_success', 'nenhum')}")
            print(f"Próxima tentativa permitida: {state.get('next_due', 'no próximo disparo')}")
            print(f"Falhas consecutivas: {state['consecutive_failures']} | Erro: {state.get('last_error') or 'nenhum'}")
            print("Feedback por consulta no Telegram: " + ("ativado" if config["cycle_feedback"] else "desativado"))
            notice = state.get("last_cycle_feedback")
            if notice:
                print(f"Último feedback: ciclo {notice['cycle']} | {notice['status']} | {notice['at']}")
            if state.get("feedback_retry_at"):
                print(f"Próximo envio de feedback permitido a partir de: {state['feedback_retry_at']}")
            print(f"Log: {directory / 'monitor.log'}")
            print("Estado do Windows e próximo disparo: scripts/monitor.ps1 -Acao Status. Instantes acima em UTC.")
        return 0
    except LockBusy:
        print("Outra execução do monitor está em andamento. Aguarde sua conclusão.", file=sys.stderr)
        return 3
    except (MonitorError, ValidationError) as exc:
        print(f"Falha no monitor: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Falha ao acessar arquivos locais do monitor.", file=sys.stderr)
        return 1
