"""Leitura e validação do perfil de busca."""

from pathlib import Path
import tomllib

from heimdall.models import Profile, ValidationError, parse_instant


def load_profile(path: Path) -> Profile:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise ValidationError(f"Perfil TOML inválido: {exc}") from exc

    def text(section: str, key: str) -> str:
        group = data.get(section)
        value = group.get(key) if isinstance(group, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Perfil: {section}.{key} deve ser texto não vazio.")
        return value.strip()

    start = parse_instant(text("periodo", "inicio"), "periodo.inicio")
    end = parse_instant(text("periodo", "fim_exclusivo"), "periodo.fim_exclusivo")
    if start >= end:
        raise ValidationError("O início deve ser anterior ao fim exclusivo.")
    if start.utcoffset() != end.utcoffset():
        raise ValidationError("O início e o fim do período devem usar o mesmo deslocamento UTC.")
    return Profile(
        movie_id=text("filme", "id"), movie_name=text("filme", "nome"),
        city_id=text("cidade", "id"), city_name=text("cidade", "nome"),
        starts_at=start, ends_before=end,
        certification=text("filtros", "certificacao"), language=text("filtros", "idioma"),
    )
