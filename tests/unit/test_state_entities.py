"""Catálogo curado de entidades del Poder Ejecutivo: consulta, no inferencia."""

from __future__ import annotations

from kipu_knowledge.domain.normalization import normalize_org_name
from kipu_knowledge.domain.state_entities import (
    MINISTRIES,
    catalog_entity,
    catalog_knows,
    looks_like_uncatalogued_ministry,
    parent_entity,
    succession_chain,
)


def test_current_name_matches_and_carries_declared_data():
    entry = catalog_entity(normalize_org_name("Ministerio de Vivienda, Construcción y Saneamiento"))
    assert entry is not None
    assert entry.acronym == "MVCS"
    assert entry.entity_type == "MINISTRY"


def test_former_name_is_known_but_does_not_resolve_to_the_current_entity():
    """ "Ministerio de Agricultura y Riego" habla de la cartera en su época:
    responder con MIDAGRI colapsaría la sucesión que "¿cuántos ministros
    tuvo?" necesita mantener separada."""
    former = normalize_org_name("Ministerio de Agricultura y Riego")
    assert catalog_knows(former)
    assert catalog_entity(former) is None


def test_midagri_succession_chain_cites_its_norms():
    midagri = catalog_entity(normalize_org_name("Ministerio de Desarrollo Agrario y Riego"))
    assert midagri is not None
    chain = succession_chain(midagri)
    assert [f.name for f in chain] == [
        "Ministerio de Agricultura y Riego",
        "Ministerio de Agricultura",
    ]
    assert all(f.basis for f in chain)


def test_contaminated_ministry_name_is_flagged_as_uncatalogued():
    polluted = normalize_org_name(
        "Ministerio de Vivienda, Construcción y Saneamiento, "
        "bajo el régimen de la Ley N° 30057, Ley del Servicio Civil"
    )
    assert looks_like_uncatalogued_ministry(polluted)


def test_known_ministries_and_non_ministries_are_not_flagged():
    assert not looks_like_uncatalogued_ministry(normalize_org_name("Ministerio de Defensa"))
    assert not looks_like_uncatalogued_ministry(normalize_org_name("Biblioteca Nacional del Perú"))


def test_catalogue_has_no_duplicate_normalized_names():
    from kipu_knowledge.domain.state_entities import ATTACHED_ENTITIES

    seen: set[str] = set()
    for entity in MINISTRIES + ATTACHED_ENTITIES:
        for name in (entity.canonical_name, *(f.name for f in entity.former_names)):
            normalized = normalize_org_name(name)
            assert normalized not in seen, f"nombre repetido en el catálogo: {name}"
            seen.add(normalized)


def test_attached_program_resolves_with_and_without_acronym_suffix():
    """Las publicaciones rematan el nombre con la sigla ("… - PRONIED"): es la
    misma grafía vigente y debe enriquecer la ficha igual que la desnuda."""
    plain = catalog_entity(normalize_org_name("Programa Nacional de Infraestructura Educativa"))
    suffixed = catalog_entity(
        normalize_org_name("Programa Nacional de Infraestructura Educativa - PRONIED")
    )
    assert plain is not None and suffixed is plain
    assert plain.acronym == "PRONIED"
    assert plain.entity_type == "NATIONAL_PROGRAM"


def test_attached_program_declares_its_ministry():
    pronied = catalog_entity(normalize_org_name("Programa Nacional de Infraestructura Educativa"))
    assert pronied is not None
    parent = parent_entity(pronied)
    assert parent is not None
    assert parent.acronym == "MINEDU"
    # Un ministerio no depende de nadie: la adscripción es del programa.
    assert parent_entity(parent) is None


def test_every_attached_entity_is_curated_and_resolves_in_all_its_spellings():
    """Cada adscrita declara su norma de creación y su ministerio, y resuelve
    con y sin sigla, con guion corto o largo — las tres grafías reales."""
    from kipu_knowledge.domain.state_entities import ATTACHED_ENTITIES

    for entity in ATTACHED_ENTITIES:
        assert entity.creation_basis, f"{entity.acronym}: sin norma de creación curada"
        parent = parent_entity(entity)
        assert parent is not None, f"{entity.acronym}: adscripción sin resolver"
        assert parent.entity_type in ("MINISTRY", "EXECUTIVE_OFFICE")
        for spelling in (
            entity.canonical_name,
            f"{entity.canonical_name} - {entity.acronym}",
            f"{entity.canonical_name} – {entity.acronym}",
        ):
            assert catalog_entity(normalize_org_name(spelling)) is entity, (
                f"{entity.acronym}: la grafía «{spelling}» no resuelve"
            )


def test_every_ministry_declares_its_organic_norm():
    """Cada cartera cita su norma de creación u organización vigente: sin la
    cita, "¿desde cuándo existe X?" no tendría con qué responderse."""
    for entity in MINISTRIES:
        assert entity.creation_basis, f"{entity.acronym}: sin norma curada"


def test_acronym_alone_is_a_current_spelling():
    """ "… del FONDEPES" nombra a la entidad solo por su sigla: es grafía
    vigente declarada por el catálogo, no una variante que curar."""
    fondepes = catalog_entity(normalize_org_name("FONDEPES"))
    assert fondepes is not None
    assert fondepes.canonical_name == "Fondo Nacional de Desarrollo Pesquero"


def test_catalog_acronyms_exclude_homographs_of_name_words():
    """ "CULTURA" es palabra de "Ministerio de Cultura": como cabecera de
    segmentación partiría el nombre completo, así que no se ofrece."""
    from kipu_knowledge.domain.state_entities import catalog_acronyms

    acronyms = catalog_acronyms()
    assert "FONDEPES" in acronyms
    assert "AGRO RURAL" in acronyms
    assert "CULTURA" not in acronyms


def test_parenthesized_acronym_is_a_current_spelling():
    """ "… Eficientes (OECE)" es la tercera grafía real con que la fuente remata
    un nombre; debe converger en la misma ficha que la desnuda."""
    plain = catalog_entity(
        normalize_org_name("Organismo Especializado para las Contrataciones Públicas Eficientes")
    )
    parenthesized = catalog_entity(
        normalize_org_name(
            "Organismo Especializado para las Contrataciones Públicas Eficientes (OECE)"
        )
    )
    assert plain is not None and parenthesized is plain


def test_oece_succeeds_osce_without_collapsing_the_succession():
    former = normalize_org_name("Organismo Supervisor de las Contrataciones del Estado")
    assert catalog_knows(former)
    assert catalog_entity(former) is None  # el OSCE habla de la entidad en su época


def test_sector_label_of_the_daily_index_resolves_to_the_ministry():
    """El índice del diario rotula al emisor por su sector, sin "Ministerio"
    ("VIVIENDA, CONSTRUCCIÓN Y SANEAMIENTO"): misma entidad, misma grafía
    vigente. La palabra completa sigue resolviendo igual."""
    sector = catalog_entity(normalize_org_name("VIVIENDA, CONSTRUCCIÓN Y SANEAMIENTO"))
    assert sector is not None and sector.acronym == "MVCS"
    assert catalog_entity(normalize_org_name("RELACIONES EXTERIORES")).acronym == "RREE"


def test_autonomous_bodies_are_curated_without_attachment():
    """La SBS, el BCRP y el JNE no dependen de ministerio alguno: su
    adscripción vacía es un hecho constitucional, no curaduría pendiente."""
    from kipu_knowledge.domain.state_entities import AUTONOMOUS_ENTITIES

    assert {entity.acronym for entity in AUTONOMOUS_ENTITIES} >= {"SBS", "BCRP", "JNE"}
    for entity in AUTONOMOUS_ENTITIES:
        assert entity.creation_basis, f"{entity.acronym}: sin norma curada"
        assert entity.attached_to is None
        assert parent_entity(entity) is None
    sbs = catalog_entity(
        normalize_org_name(
            "Superintendencia de Banca, Seguros y Administradoras Privadas de Fondos de Pensiones"
        )
    )
    assert sbs is not None and sbs.acronym == "SBS"


def test_essalud_and_ipen_resolve_from_their_published_spellings():
    essalud = catalog_entity(normalize_org_name("Seguro Social de Salud – ESSALUD"))
    assert essalud is not None and essalud.acronym == "ESSALUD"
    assert parent_entity(essalud).acronym == "MTPE"
    ipen = catalog_entity(normalize_org_name("Instituto Peruano de Energía Nuclear"))
    assert ipen is not None
    assert parent_entity(ipen).acronym == "MINEM"
