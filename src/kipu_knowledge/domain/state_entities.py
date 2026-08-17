"""Catálogo curado de entidades del Poder Ejecutivo peruano.

Es una referencia declarada, no una extracción: sirve para (1) enriquecer con
sigla y tipo la ficha de una organización cuya grafía coincide exactamente con
el catálogo, y (2) hacer visible —vía ONTOLOGY_CANDIDATE— todo nombre que
parece un ministerio pero no figura aquí, que es la firma tanto de una entidad
nueva o renombrada como de una extracción contaminada.

Reglas de diseño:
- El catálogo NO crea filas en la base: una Organization sigue naciendo de una
  mención con evidencia. Aquí solo viven datos declarados y estables.
- Los nombres históricos registran la sucesión (el ejemplo canónico: Ministerio
  de Agricultura → MINAGRI → MIDAGRI) citando la norma del cambio cuando la
  cita está verificada. `basis=None` significa "pendiente de curar", nunca
  "no existe": completar la cita es trabajo humano, no una inferencia.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kipu_knowledge.domain.normalization import normalize_org_name


@dataclass(frozen=True)
class FormerName:
    """Nombre anterior de una entidad, con la norma del cambio si está curada."""

    name: str
    basis: str | None = None  # cita de la norma que dispuso el cambio


@dataclass(frozen=True)
class StateEntity:
    canonical_name: str
    acronym: str | None
    entity_type: str = "MINISTRY"
    former_names: tuple[FormerName, ...] = field(default_factory=tuple)
    creation_basis: str | None = None  # norma de creación, cuando está curada


# Ministerios del Poder Ejecutivo (más la PCM, que encabeza el Consejo de
# Ministros sin ser ministerio). Fuente de la lista: Ley N° 29158, Ley Orgánica
# del Poder Ejecutivo, y sus leyes modificatorias de carteras.
MINISTRIES: tuple[StateEntity, ...] = (
    StateEntity(
        "Presidencia del Consejo de Ministros",
        "PCM",
        entity_type="EXECUTIVE_OFFICE",
    ),
    StateEntity("Ministerio de Relaciones Exteriores", "RREE"),
    StateEntity("Ministerio de Defensa", "MINDEF"),
    StateEntity("Ministerio de Economía y Finanzas", "MEF"),
    StateEntity("Ministerio del Interior", "MININTER"),
    StateEntity("Ministerio de Justicia y Derechos Humanos", "MINJUSDH"),
    StateEntity("Ministerio de Educación", "MINEDU"),
    StateEntity("Ministerio de Salud", "MINSA"),
    StateEntity(
        "Ministerio de Desarrollo Agrario y Riego",
        "MIDAGRI",
        former_names=(
            FormerName(
                "Ministerio de Agricultura y Riego",
                basis="Ley N° 31075 (renombra el MINAGRI como MIDAGRI)",
            ),
            FormerName(
                "Ministerio de Agricultura",
                basis="Ley N° 30048 (renombra el Ministerio de Agricultura como MINAGRI)",
            ),
        ),
    ),
    StateEntity("Ministerio de Trabajo y Promoción del Empleo", "MTPE"),
    StateEntity("Ministerio de la Producción", "PRODUCE"),
    StateEntity("Ministerio de Comercio Exterior y Turismo", "MINCETUR"),
    StateEntity("Ministerio de Energía y Minas", "MINEM"),
    StateEntity("Ministerio de Transportes y Comunicaciones", "MTC"),
    StateEntity("Ministerio de Vivienda, Construcción y Saneamiento", "MVCS"),
    StateEntity(
        "Ministerio de la Mujer y Poblaciones Vulnerables",
        "MIMP",
        former_names=(FormerName("Ministerio de la Mujer y Desarrollo Social"),),
    ),
    StateEntity(
        "Ministerio del Ambiente",
        "MINAM",
        creation_basis="Decreto Legislativo N° 1013",
    ),
    StateEntity(
        "Ministerio de Cultura",
        "CULTURA",
        creation_basis="Ley N° 29565",
    ),
    StateEntity(
        "Ministerio de Desarrollo e Inclusión Social",
        "MIDIS",
        creation_basis="Ley N° 29792",
    ),
)


def _index() -> dict[str, tuple[StateEntity, bool]]:
    """Nombre normalizado → (entidad, es_nombre_vigente)."""
    table: dict[str, tuple[StateEntity, bool]] = {}
    for entity in MINISTRIES:
        table[normalize_org_name(entity.canonical_name)] = (entity, True)
        for former in entity.former_names:
            table[normalize_org_name(former.name)] = (entity, False)
    return table


_BY_NORMALIZED = _index()

_MINISTRY_HEAD = "MINISTERIO "


def catalog_entity(name_normalized: str) -> StateEntity | None:
    """Entidad del catálogo cuyo nombre vigente coincide exactamente.

    Un nombre histórico NO devuelve la entidad vigente: el documento que dice
    "Ministerio de Agricultura y Riego" habla de la cartera en su época, y
    responder con MIDAGRI colapsaría la sucesión que la pregunta "¿cuántos
    ministros tuvo?" necesita mantener separada.
    """
    hit = _BY_NORMALIZED.get(name_normalized)
    if hit is None or not hit[1]:
        return None
    return hit[0]


def catalog_knows(name_normalized: str) -> bool:
    """¿Figura el nombre en el catálogo, vigente o histórico?"""
    return name_normalized in _BY_NORMALIZED


def looks_like_uncatalogued_ministry(name_normalized: str) -> bool:
    """¿Se llama "Ministerio …" sin figurar en el catálogo?

    Es la señal de prevención: un ministerio es un conjunto pequeño, cerrado y
    conocido, así que un nombre nuevo con esa cabecera o es un cambio real del
    Estado (que merece curaduría) o es una extracción defectuosa (que merece
    corrección). En ambos casos, tarea; en ninguno, silencio.
    """
    return name_normalized.startswith(_MINISTRY_HEAD) and not catalog_knows(name_normalized)


def succession_chain(entity: StateEntity) -> list[FormerName]:
    """Nombres anteriores, del más reciente al más antiguo."""
    return list(entity.former_names)
