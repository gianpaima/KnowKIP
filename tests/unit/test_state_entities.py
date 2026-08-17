"""Catálogo curado de entidades del Poder Ejecutivo: consulta, no inferencia."""

from __future__ import annotations

from kipu_knowledge.domain.normalization import normalize_org_name
from kipu_knowledge.domain.state_entities import (
    MINISTRIES,
    catalog_entity,
    catalog_knows,
    looks_like_uncatalogued_ministry,
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
    seen: set[str] = set()
    for entity in MINISTRIES:
        for name in (entity.canonical_name, *(f.name for f in entity.former_names)):
            normalized = normalize_org_name(name)
            assert normalized not in seen, f"nombre repetido en el catálogo: {name}"
            seen.add(normalized)
