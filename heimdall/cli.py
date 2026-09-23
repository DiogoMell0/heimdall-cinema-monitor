"""Consulta manual online e reprodução de capturas offline."""

import argparse
from pathlib import Path
import sys

from heimdall.config import load_profile
from heimdall.models import ValidationError
from heimdall.rules import rejection_reasons
from heimdall.service import analyze
from heimdall.sources.http import CollectionError, DEFAULT_TIMEOUT, collect
from heimdall.sources.snapshot import load_snapshot, save_envelope
from heimdall.storage import HistoryError, default_database_path, read_history
from heimdall.tracking import check_online, register_capture

ROOT = Path(__file__).resolve().parent.parent


def _print_events(events):
    for event in events:
        row = event["payload"]
        kind = "nova sessão compatível" if event["kind"] == "new_matching_session" else "passou a ser compatível"
        print(f"  Evento {event['id']}: {kind} | {row['starts_at']} | {row['theater_name']} | {row['room']} | id={row['id']}")
        print(f"    {row['purchase_url']}")


def _history_command(args) -> int:
    database = args.banco if args.banco is not None else default_database_path()
    try:
        profile = load_profile(args.perfil)
        if args.command == "historico":
            if not 1 <= args.limite <= 100:
                raise ValidationError("O limite do histórico deve estar entre 1 e 100.")
            summary = read_history(database, profile, limit=args.limite)
            print(f"HEIMDALL | Histórico local | Banco: {database.resolve()}")
            if summary is None or summary["latest_run"] is None:
                print("Nenhuma consulta registrada para este perfil.")
                return 0
            print(f"Modo: {summary['dataset']} | Consultas: {summary['runs']} | Falhas: {summary['failures']}")
            print(f"Sessões conhecidas: {summary['known_sessions']} | Novidades registradas: {summary['event_count']}")
            latest = summary["latest_run"]
            print(f"Última execução: {latest['recorded_at']} | Resultado: {latest['status']}")
            if latest["status"] == "failure":
                print(f"  {latest['error_category']}: {latest['error_message']}")
            if summary["last_success"]:
                success = summary["last_success"]
                print(f"Última coleta válida: {success['observed_at']} | Sessões: {success['session_count']} | Compatíveis: {success['matching_count']}")
            print("Novidades recentes (o registro não significa envio de aviso):")
            _print_events(summary["events"])
            return 0
        if args.command == "verificar":
            if args.avisar:
                from heimdall.notifications import verify_and_notify
                result, deliveries = verify_and_notify(profile, database, timeout=args.timeout)
            else:
                result = check_online(profile, database, timeout=args.timeout)
            print("HEIMDALL | Verificação ONLINE com histórico — execução manual única")
        else:
            result = register_capture(profile, args.arquivo, database)
            print("HEIMDALL | Registro OFFLINE — reprodução da captura salva")
        analysis = analyze(result.snapshot, profile)
        print(f"Banco: {database.resolve()}")
        print(f"Coleta: {result.snapshot.captured_at.isoformat()} | Sessões: {len(result.snapshot.sessions)} | Compatíveis: {len(analysis.matching)}")
        print(f"Sessões novas no histórico: {result.changes.new_sessions} | Sessões alteradas: {result.changes.changed_sessions}")
        print(f"Novidades compatíveis nesta execução: {len(result.changes.new_events)}")
        print("Datas não listadas: " + (", ".join(day.isoformat() for day in analysis.pending_dates) or "nenhuma"))
        _print_events(result.changes.new_events)
        if args.command == "verificar" and args.avisar:
            from heimdall.notifications import print_results
            from heimdall.delivery import read_deliveries
            print_results(deliveries)
            counts = read_deliveries(database, profile)["counts"]
            pending = sum(row["total"] for row in counts if row["status"] == "pending")
            uncertain = sum(row["total"] for row in counts if row["status"] in ("uncertain", "sending"))
            print(f"Fila Telegram: {pending} pendente(s), {uncertain} envio(s) a conferir.")
            print("Execução manual concluída. Confira monitor status para o controle da rotina periódica.")
            return int(bool(uncertain) or any(row["status"] != "sent" for row in deliveries))
        print("Histórico salvo. Nenhum aviso enviado; use verificar --avisar para enviar novidades.")
        return 0
    except (CollectionError, HistoryError, ValidationError, OSError) as exc:
        print(f"Falha no histórico ou na verificação: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heimdall")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("analisar", help="Analisar uma captura local; não consulta a programação atual.")
    command.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
    command.add_argument("--arquivo", type=Path, default=ROOT / "examples/historico/api-2026-09-07.json")
    command.add_argument("--listar", action="store_true", help="Mostrar todas as sessões e os motivos de rejeição.")
    online = commands.add_parser("consultar", help="Consultar a API uma vez e aplicar os filtros do perfil.")
    online.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
    online.add_argument("--listar", action="store_true", help="Mostrar todas as sessões retornadas e motivos de rejeição.")
    online.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout de operações de rede em segundos (padrão: 20; máximo: 60).")
    online.add_argument("--salvar", type=Path, help="Salvar a consulta válida em um novo JSON, para análise offline.")
    check = commands.add_parser("verificar", help="Consultar uma vez, salvar o histórico e identificar novidades.")
    check.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
    check.add_argument("--banco", type=Path, help="Banco online; padrão: pasta do usuário/.heimdall-cinema-monitor/heimdall.sqlite3.")
    check.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    check.add_argument("--avisar", action="store_true", help="Enviar novidades após consultar e gravar o histórico.")
    register = commands.add_parser("registrar", help="Comparar uma captura salva em um banco de reprodução offline.")
    register.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
    register.add_argument("--arquivo", type=Path, required=True)
    register.add_argument("--banco", type=Path, required=True, help="Banco separado para demonstrações offline.")
    history = commands.add_parser("historico", help="Ler o histórico salvo sem consultar a API.")
    history.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
    history.add_argument("--banco", type=Path)
    history.add_argument("--limite", type=int, default=10, help="Quantidade de eventos recentes, entre 1 e 100.")
    monitor = commands.add_parser("monitor", help="Configurar, executar e consultar a rotina local.")
    monitor_commands = monitor.add_subparsers(dest="monitor_command", required=True)
    from heimdall.monitor import default_directory
    for name in ("configurar", "executar", "status", "pausar", "retomar", "diagnosticar", "feedback"):
        command = monitor_commands.add_parser(name)
        command.add_argument("--diretorio", type=Path, default=default_directory())
        if name == "feedback":
            command.add_argument("feedback_action", choices=("ativar", "desativar"),
                                 help="Enviar ou silenciar resumos de cada ciclo, preservando os avisos de sessões.")
        if name == "configurar":
            command.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
            command.add_argument("--banco", type=Path)
            command.add_argument("--intervalo", type=int, default=30, help="Intervalo entre consultas em minutos.")
    telegram = commands.add_parser("telegram", help="Vincular sua conversa privada e testar o bot do Telegram.")
    telegram_commands = telegram.add_subparsers(dest="telegram_command", required=True)
    setup = telegram_commands.add_parser("configurar", help="Inserir token oculto e vincular sua conversa pelo Telegram.")
    setup.add_argument("--substituir", action="store_true", help="Substituir a configuração existente depois de validar o novo vínculo.")
    setup.add_argument("--janela", action="store_true", help="Colar o token em uma janela com campo de senha, usando Ctrl+V.")
    telegram_commands.add_parser("status", help="Verificar somente a configuração local, sem rede ou envio.")
    telegram_commands.add_parser("testar", help="Enviar uma mensagem identificada como teste à conversa configurada.")
    notices = commands.add_parser("avisos", help="Acompanhar e resolver entregas do Telegram.")
    notice_commands = notices.add_subparsers(dest="notice_command", required=True)
    for name, help_text in (("historico", "Ler entregas e envios incertos sem rede."),
                            ("resolver", "Confirmar recebimento ou autorizar outra tentativa de um envio incerto.")):
        notice = notice_commands.add_parser(name, help=help_text)
        notice.add_argument("--banco", type=Path)
        notice.add_argument("--perfil", type=Path, default=ROOT / "config/perfil.toml")
        if name == "resolver":
            notice.add_argument("--lote", required=True)
            notice.add_argument("--acao", required=True, choices=("confirmar", "tentar-novamente"))
    args = parser.parse_args(argv)
    if args.command == "monitor":
        from heimdall.monitor import run_command
        return run_command(args)
    if args.command == "avisos":
        from heimdall.notifications import run_command
        return run_command(args)
    if args.command == "telegram":
        from heimdall.telegram_setup import run_command
        return run_command(args)
    if args.command in ("verificar", "registrar", "historico"):
        return _history_command(args)
    try:
        profile = load_profile(args.perfil)
        collection = None
        if args.command == "consultar":
            if args.salvar and (args.salvar.exists() or not args.salvar.parent.is_dir()):
                raise ValidationError("Para salvar, escolha um arquivo novo em uma pasta existente.")
            collection = collect(profile, timeout=args.timeout)
            snapshot = collection.snapshot
        else:
            snapshot = load_snapshot(args.arquivo, profile)
        result = analyze(snapshot, profile)
    except CollectionError as exc:
        print(f"Falha na consulta [{exc.category}]: {exc}", file=sys.stderr)
        print("Resultado inconclusivo; nenhuma captura anterior foi usada como resultado atual.", file=sys.stderr)
        return 1
    except (OSError, ValidationError) as exc:
        operation = "consulta" if args.command == "consultar" else "análise"
        print(f"Falha na {operation}: {exc}", file=sys.stderr)
        return 1

    if collection is not None:
        print("HEIMDALL | Consulta ONLINE — execução manual única")
        print(f"HTTP {collection.envelope['status']} | {collection.envelope['elapsedMs']} ms | {collection.envelope['bytes']} bytes")
    else:
        print("HEIMDALL | Análise OFFLINE — não é uma consulta atual")
    print(f"Captura e instante de avaliação: {snapshot.captured_at.isoformat()}")
    print(f"Filme: {profile.movie_name} | Cidade: {profile.city_name}")
    print(f"Período: {profile.starts_at.isoformat()} até {profile.ends_before.isoformat()} (fim exclusivo)")
    print(f"Sessões na captura: {len(snapshot.sessions)} | Compatíveis: {len(result.matching)}")
    if not snapshot.sessions:
        print("Resposta válida sem sessões; as datas não listadas continuam pendentes.")
    print("Programação presente na captura:")
    for day in snapshot.published_dates:
        count = sum(session.programming_date == day for session in snapshot.sessions)
        print(f"  {day:%d/%m/%Y}: {count} sessões")
    pending = ", ".join(f"{day:%d/%m/%Y}" for day in result.pending_dates)
    print(f"Datas do perfil não listadas na captura: {pending or 'nenhuma'}")
    rows = snapshot.sessions if args.listar else result.matching
    for session in sorted(rows, key=lambda row: (row.starts_at, row.theater_name, row.id)):
        reasons = rejection_reasons(session, profile, now=snapshot.captured_at)
        status = "; ".join(reasons) if reasons else "COMPATÍVEL"
        print(f"  {session.starts_at.isoformat()} | {session.theater_name} | {session.room} | "
              f"{', '.join(sorted(session.labels))} | {status} | id={session.id}")
        if not reasons:
            print(f"    {session.purchase_url}")
    print("Nenhum aviso enviado por este comando. Confira monitor status para a rotina periódica.")
    if collection is not None and args.salvar:
        try:
            save_envelope(args.salvar, collection.envelope)
        except OSError as exc:
            print(f"Consulta concluída, mas não foi possível salvar a captura: {exc}", file=sys.stderr)
            return 1
        print(f"Captura salva: {args.salvar.resolve()}")
    return 0
