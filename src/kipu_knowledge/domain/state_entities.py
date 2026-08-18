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

import re
from dataclasses import dataclass, field

from kipu_knowledge.domain.normalization import normalize_org_name, strip_accents


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
    # Nombre canónico de la entidad de la que depende (adscripción declarada por
    # su norma de creación). Solo se cura aquí; nunca se infiere del texto.
    attached_to: str | None = None


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


# Programas y organismos adscritos que aparecen en resoluciones de personal.
# Mismo contrato que MINISTRIES: referencia declarada y curada a mano, con la
# norma de creación cuando la cita está verificada. `attached_to` registra la
# adscripción que su norma dispone; es lo que permite colgar el programa de su
# ministerio sin inferir nada del texto.
ATTACHED_ENTITIES: tuple[StateEntity, ...] = (
    StateEntity(
        "Programa Nacional de Infraestructura Educativa",
        "PRONIED",
        entity_type="NATIONAL_PROGRAM",
        creation_basis="Decreto Supremo N° 004-2014-MINEDU (crea el PRONIED)",
        attached_to="Ministerio de Educación",
    ),
    StateEntity(
        "Seguro Social de Salud",
        "ESSALUD",
        entity_type="DECENTRALIZED_PUBLIC_BODY",
        creation_basis="Ley N° 27056 (crea el Seguro Social de Salud, adscrito al Sector Trabajo)",
        attached_to="Ministerio de Trabajo y Promoción del Empleo",
    ),
    StateEntity(
        "Instituto Peruano de Energía Nuclear",
        "IPEN",
        entity_type="PUBLIC_EXECUTING_BODY",
        creation_basis="Decreto Ley N° 21094 (Ley del Sector Energía y Minas, crea el IPEN)",
        attached_to="Ministerio de Energía y Minas",
    ),
    StateEntity(
        "Comisión de Promoción del Perú para la Exportación y el Turismo",
        "PROMPERÚ",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        creation_basis=(
            "Decreto Supremo N° 003-2007-MINCETUR (fusiona PromPerú y PROMPEX en la "
            "Comisión de Promoción del Perú para la Exportación y el Turismo)"
        ),
        attached_to="Ministerio de Comercio Exterior y Turismo",
    ),
    StateEntity(
        "Fondo Nacional de Desarrollo Pesquero",
        "FONDEPES",
        entity_type="PUBLIC_EXECUTING_BODY",
        creation_basis=(
            "Decreto Supremo N° 010-92-PE (crea el FONDEPES; con fuerza de ley por el "
            "artículo 57 del Decreto Ley N° 25977, Ley General de Pesca)"
        ),
        attached_to="Ministerio de la Producción",
    ),
    StateEntity(
        "Programa de Desarrollo Productivo Agrario Rural",
        "AGRO RURAL",
        entity_type="NATIONAL_PROGRAM",
        creation_basis=(
            "Decreto Legislativo N° 997, Segunda Disposición Complementaria Final (crea "
            "AGRO RURAL); creación formalizada por Decreto Supremo N° 012-2020-MIDAGRI"
        ),
        attached_to="Ministerio de Desarrollo Agrario y Riego",
    ),
    StateEntity(
        "Servicio Nacional de Capacitación para la Industria de la Construcción",
        "SENCICO",
        entity_type="DECENTRALIZED_PUBLIC_BODY",
        creation_basis="Decreto Ley N° 21673 (Ley Orgánica del SENCICO)",
        attached_to="Ministerio de Vivienda, Construcción y Saneamiento",
    ),
    StateEntity(
        "Agencia Peruana de Cooperación Internacional",
        "APCI",
        entity_type="PUBLIC_EXECUTING_BODY",
        creation_basis="Ley N° 27692 (Ley de Creación de la APCI)",
        attached_to="Ministerio de Relaciones Exteriores",
    ),
    StateEntity(
        "Instituto Geológico, Minero y Metalúrgico",
        "INGEMMET",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        creation_basis=(
            "Decreto Supremo N° 021-78-EM/OR (fusiona INGEOMIN e INCITEMI en el "
            "INGEMMET); Ley Orgánica: Decreto Ley N° 22631"
        ),
        attached_to="Ministerio de Energía y Minas",
    ),
    StateEntity(
        "Dirección Nacional de Inteligencia",
        "DINI",
        entity_type="PUBLIC_EXECUTING_BODY",
        creation_basis=(
            "Ley N° 28664 (Ley del Sistema de Inteligencia Nacional - SINA y de la "
            "DINI); régimen vigente: Decreto Legislativo N° 1141"
        ),
        attached_to="Presidencia del Consejo de Ministros",
    ),
    StateEntity(
        "Instituto Tecnológico de la Producción",
        "ITP",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        former_names=(
            FormerName(
                "Instituto Tecnológico Pesquero del Perú",
                basis=(
                    "Ley N° 29951, Ley de Presupuesto 2013 (renombra el Instituto "
                    "Tecnológico Pesquero del Perú como Instituto Tecnológico de la "
                    "Producción)"
                ),
            ),
        ),
        creation_basis=(
            "Decreto Legislativo N° 92 (crea el Instituto Tecnológico Pesquero del Perú)"
        ),
        attached_to="Ministerio de la Producción",
    ),
    StateEntity(
        "Autoridad Nacional de Infraestructura",
        "ANIN",
        entity_type="PUBLIC_EXECUTING_BODY",
        creation_basis="Ley N° 31841 (crea la Autoridad Nacional de Infraestructura)",
        attached_to="Presidencia del Consejo de Ministros",
    ),
    StateEntity(
        "Superintendencia Nacional de Bienes Estatales",
        "SBN",
        entity_type="PUBLIC_EXECUTING_BODY",
        former_names=(
            FormerName(
                "Superintendencia de Bienes Nacionales",
                basis=(
                    "Ley N° 29151, Ley General del Sistema Nacional de Bienes Estatales "
                    "(fija la denominación Superintendencia Nacional de Bienes Estatales)"
                ),
            ),
        ),
        creation_basis=(
            "Decreto Ley N° 25556, Cuarta Disposición Final, modificado por Decreto Ley "
            "N° 25738 (crea la Superintendencia de Bienes Nacionales)"
        ),
        attached_to="Ministerio de Vivienda, Construcción y Saneamiento",
    ),
    StateEntity(
        "Centro Nacional de Planeamiento Estratégico",
        "CEPLAN",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        creation_basis=(
            "Decreto Legislativo N° 1088 (Ley del Sistema Nacional de Planeamiento "
            "Estratégico y del CEPLAN)"
        ),
        attached_to="Presidencia del Consejo de Ministros",
    ),
    StateEntity(
        "Organismo Especializado para las Contrataciones Públicas Eficientes",
        "OECE",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        former_names=(
            FormerName(
                "Organismo Supervisor de las Contrataciones del Estado",
                basis=(
                    "Ley N° 32069, Ley General de Contrataciones Públicas (crea el OECE; "
                    "toda referencia al OSCE se entiende hecha al OECE)"
                ),
            ),
        ),
        creation_basis="Ley N° 32069, Ley General de Contrataciones Públicas (crea el OECE)",
        attached_to="Ministerio de Economía y Finanzas",
    ),
    StateEntity(
        "Superintendencia Nacional de Aduanas y de Administración Tributaria",
        "SUNAT",
        entity_type="SPECIALIZED_TECHNICAL_BODY",
        former_names=(
            FormerName(
                "Superintendencia Nacional de Administración Tributaria",
                basis=(
                    "Ley N° 29816, Ley de Fortalecimiento de la SUNAT (fija la "
                    "denominación Superintendencia Nacional de Aduanas y de "
                    "Administración Tributaria)"
                ),
            ),
        ),
        creation_basis="Ley N° 24829 (crea la SUNAT)",
        attached_to="Ministerio de Economía y Finanzas",
    ),
)

# Organismos constitucionalmente autónomos que publican actos de personal en el
# diario oficial. No dependen de ministerio alguno: su `attached_to` es None por
# derecho propio, no por curaduría pendiente.
AUTONOMOUS_ENTITIES: tuple[StateEntity, ...] = (
    StateEntity(
        "Superintendencia de Banca, Seguros y Administradoras Privadas de Fondos de Pensiones",
        "SBS",
        entity_type="CONSTITUTIONAL_AUTONOMOUS_BODY",
        creation_basis=(
            "Constitución Política del Perú, artículo 87; Ley N° 26702, Ley General del "
            "Sistema Financiero y del Sistema de Seguros y Orgánica de la SBS"
        ),
    ),
    StateEntity(
        "Banco Central de Reserva del Perú",
        "BCRP",
        entity_type="CONSTITUTIONAL_AUTONOMOUS_BODY",
        creation_basis=(
            "Constitución Política del Perú, artículo 84; Decreto Ley N° 26123 "
            "(Ley Orgánica del BCRP)"
        ),
    ),
    StateEntity(
        "Jurado Nacional de Elecciones",
        "JNE",
        entity_type="CONSTITUTIONAL_AUTONOMOUS_BODY",
        creation_basis=(
            "Constitución Política del Perú, artículo 177; Ley N° 26486 (Ley Orgánica del JNE)"
        ),
    ),
)

_ALL_ENTITIES: tuple[StateEntity, ...] = MINISTRIES + ATTACHED_ENTITIES + AUTONOMOUS_ENTITIES

_SECTOR_PREFIX_RE = re.compile(r"^Ministerio de(?:l| la)?\s+")


def _index() -> dict[str, tuple[StateEntity, bool]]:
    """Nombre normalizado → (entidad, es_nombre_vigente)."""
    table: dict[str, tuple[StateEntity, bool]] = {}
    for entity in _ALL_ENTITIES:
        table[normalize_org_name(entity.canonical_name)] = (entity, True)
        if entity.acronym:
            # Las publicaciones suelen rematar el nombre con su sigla, unas veces
            # con guion corto ("… - PRONIED") y otras con guion largo
            # ("… – IPEN"): es la misma grafía vigente, no una variante que
            # curar. La normalización conserva el guion largo, así que ambas
            # formas se registran.
            for dash in ("-", "–"):
                table[normalize_org_name(f"{entity.canonical_name} {dash} {entity.acronym}")] = (
                    entity,
                    True,
                )
            # También rematan el nombre con la sigla entre paréntesis
            # ("… Eficientes (OECE)"): misma grafía vigente.
            table[normalize_org_name(f"{entity.canonical_name} ({entity.acronym})")] = (
                entity,
                True,
            )
            # También nombran a la entidad por su sigla sola ("… del FONDEPES"):
            # es la misma grafía vigente, declarada por el catálogo.
            table[normalize_org_name(entity.acronym)] = (entity, True)
        if entity.entity_type == "MINISTRY":
            # El índice del diario rotula al emisor por su sector, sin la
            # palabra "Ministerio" ("VIVIENDA, CONSTRUCCIÓN Y SANEAMIENTO"):
            # es el mismo nombre vigente dicho como sector.
            sector = _SECTOR_PREFIX_RE.sub("", entity.canonical_name)
            if sector != entity.canonical_name:
                table[normalize_org_name(sector)] = (entity, True)
        for former in entity.former_names:
            table[normalize_org_name(former.name)] = (entity, False)
    return table


_BY_NORMALIZED = _index()

_MINISTRY_HEAD = "MINISTERIO "


def catalog_acronyms() -> tuple[str, ...]:
    """Siglas vigentes que nombran a la entidad por sí solas ("del FONDEPES").

    Sirven de cabecera de organización al segmentador de rutas de puesto. Se
    excluye toda sigla que además sea palabra de un nombre del catálogo
    ("CULTURA" lo es de "Ministerio de Cultura"): como cabecera partiría el
    nombre completo por la mitad. La comparación del segmentador es sensible a
    mayúsculas, así que una sigla nunca calza con la palabra homógrafa en
    minúsculas de un nombre corriente.
    """
    name_words = {
        strip_accents(word).upper()
        for entity in _ALL_ENTITIES
        for name in (entity.canonical_name, *(f.name for f in entity.former_names))
        for word in name.replace(",", " ").split()
    }
    return tuple(
        entity.acronym
        for entity in _ALL_ENTITIES
        if entity.acronym and entity.acronym not in name_words
    )


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


def parent_entity(entity: StateEntity) -> StateEntity | None:
    """Entidad del catálogo a la que `entity` está adscrita, si declara una."""
    if entity.attached_to is None:
        return None
    hit = _BY_NORMALIZED.get(normalize_org_name(entity.attached_to))
    return hit[0] if hit is not None else None
