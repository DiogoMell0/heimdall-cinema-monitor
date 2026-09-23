"""Critérios de compatibilidade de uma sessão com o perfil."""

from datetime import datetime

from heimdall.models import Profile, Session, ValidationError


def rejection_reasons(session: Session, profile: Profile, *, now: datetime) -> tuple[str, ...]:
    if now.utcoffset() is None:
        raise ValidationError("O instante de avaliação precisa de fuso horário.")
    reasons: list[str] = []
    if session.movie_id != profile.movie_id:
        reasons.append("outro filme")
    if session.city_id != profile.city_id:
        reasons.append("outra cidade")
    labels = {label.strip().casefold() for label in session.labels}
    if profile.certification.strip().casefold() not in labels:
        reasons.append(f"sem {profile.certification}")
    if profile.language.strip().casefold() not in labels:
        reasons.append(f"sem {profile.language}")
    if not profile.starts_at <= session.starts_at < profile.ends_before:
        reasons.append("fora do período")
    if session.starts_at <= now:
        reasons.append("sessão já iniciada")
    if not session.sale_enabled or not session.purchase_url:
        reasons.append("compra não habilitada")
    return tuple(reasons)
