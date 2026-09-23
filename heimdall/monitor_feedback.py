"""Formatação do resumo de cada consulta."""

from datetime import date, timedelta

from heimdall.models import parse_instant
from heimdall.service import analyze


ERROR_LABELS = {
    "rede": "Falha de conexão com a fonte",
    "timeout": "A fonte não respondeu dentro do prazo",
    "limite_de_acesso": "A fonte pediu uma espera antes de novas consultas",
    "acesso_recusado": "A fonte recusou o acesso",
    "redirecionamento": "A fonte redirecionou a consulta",
    "dados_invalidos": "A fonte retornou dados inesperados",
    "resposta_invalida": "A resposta da fonte não pôde ser validada",
    "resposta_grande": "A resposta excedeu o tamanho permitido",
    "nao_encontrado": "A consulta não foi encontrada na fonte",
    "http": "A fonte respondeu com erro HTTP",
    "collection_error": "Falha na consulta à fonte",
    "local_storage_or_configuration": "Falha no histórico ou na configuração local",
    "unexpected_error": "Falha interna; confira o log local",
    "delivery_attention": "Consulta concluída; há avisos de sessões sem confirmação",
}


def summarize(result, profile, deliveries):
    analysis = analyze(result.snapshot, profile)
    days = (profile.ends_before - timedelta(microseconds=1)).date() - profile.starts_at.date()
    return {
        "collected_at": result.snapshot.captured_at.isoformat(),
        "sessions": len(result.snapshot.sessions),
        "matching": len(analysis.matching),
        "new_sessions": result.changes.new_sessions,
        "changed_sessions": result.changes.changed_sessions,
        "new_events": len(result.changes.new_events),
        "total_dates": days.days + 1,
        "pending_dates": [day.isoformat() for day in analysis.pending_dates],
        "sent_messages": sum(item["status"] == "sent" for item in deliveries),
        "sent_events": sum(item.get("events", 0) for item in deliveries if item["status"] == "sent"),
    }


def format_feedback(state, profile):
    def local(value):
        return parse_instant(value, "feedback_time").astimezone(profile.starts_at.tzinfo).strftime("%d/%m/%Y %H:%M:%S %z")

    def text(value, limit):
        return " ".join(value.split())[:limit]

    duration = max(0, (parse_instant(state["last_finished"], "last_finished")
                       - parse_instant(state["last_started"], "last_started")).total_seconds())
    outcome = "Concluída" if state["status"] == "ok" else "Atenção"
    last_day = (profile.ends_before - timedelta(microseconds=1)).date()
    lines = [
        f"HEIMDALL — RESUMO DA CONSULTA #{state['cycles']}",
        text(profile.movie_name, 140),
        f"{text(profile.city_name, 80)} | {text(profile.certification, 80)} + {text(profile.language, 40)}",
        f"Sessões de {profile.starts_at:%d/%m} a {last_day:%d/%m/%Y}",
        "", f"{outcome} em {local(state['last_finished'])}",
        f"Duração da verificação: {duration:.1f} s",
    ]
    summary = state.get("cycle_summary")
    if summary is not None:
        lines.extend([
            f"Sessões retornadas pela API: {summary['sessions']}",
            f"Compatíveis com seus filtros: {summary['matching']}",
            f"IDs novos: {summary['new_sessions']} | Sessões alteradas: {summary['changed_sessions']}",
            f"Novidades compatíveis: {summary['new_events']}",
            f"Avisos de sessões enviados: {summary['sent_messages']} mensagem(ns), {summary['sent_events']} sessão(ões)",
        ])
        if "pending_deliveries" in summary:
            lines.append(f"Avisos pendentes: {summary['pending_deliveries']} | Incertos: {summary['uncertain_deliveries']}")
        else:
            lines.append("Não foi possível conferir a fila de avisos.")
        pending = summary["pending_dates"]
        lines.append(f"Datas publicadas no período: {summary['total_dates'] - len(pending)}/{summary['total_dates']}")
        if pending:
            labels = ", ".join(date.fromisoformat(day).strftime("%d/%m") for day in pending[:8])
            suffix = f" (+{len(pending) - 8})" if len(pending) > 8 else ""
            lines.append(f"Datas ainda não listadas: {labels}{suffix}")
    else:
        lines.append("Sem resultado completo desta verificação. Isso não significa zero sessões.")
        if state.get("last_collection"):
            lines.append(f"Última consulta válida anterior: {local(state['last_collection'])}")
    if state.get("last_error"):
        lines.extend(["", ERROR_LABELS.get(state["last_error"], "Falha na verificação; confira o estado local"),
                      f"Falhas consecutivas: {state['consecutive_failures']}"])
    if state["blocked"]:
        lines.append("Consultas interrompidas até conferência e retomada manual.")
    elif parse_instant(state["next_due"], "next_due") >= profile.ends_before:
        lines.append("Não há outra tentativa prevista dentro do período deste perfil.")
    else:
        lines.append(f"Próxima consulta: no disparo a partir de {local(state['next_due'])}")
    return "\n".join(lines)
