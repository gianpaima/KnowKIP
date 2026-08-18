"""Higiene de nombres de organización sobre el corpus real A–H.

Las salvaguardas nuevas (coletillas, catálogo de ministerios) deben ser
silenciosas sobre nombres limpios: cada falso positivo es una tarea que un
revisor tiene que descartar a mano.
"""

from __future__ import annotations

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import org_name_contamination


def test_clean_corpus_creates_no_contaminated_organizations(ingested_session):
    for org in ingested_session.execute(select(m.Organization)).scalars():
        assert org_name_contamination(org.name_normalized) is None, org.preferred_name


def test_clean_corpus_opens_no_org_hygiene_tasks(ingested_session):
    noisy = (
        ingested_session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.target_type == "organization",
                m.ReviewTask.task_type.in_(
                    (
                        e.ReviewTaskType.EXTRACTION_CONFLICT,
                        e.ReviewTaskType.ONTOLOGY_CANDIDATE,
                    )
                ),
            )
        )
        .scalars()
        .all()
    )
    assert noisy == [], [task.reason for task in noisy]


def test_catalogued_ministries_are_enriched_at_creation(ingested_session):
    """El caso A designa en el MIDAGRI: la ficha nace con sigla y tipo declarados."""
    org = ingested_session.execute(
        select(m.Organization).where(
            m.Organization.name_normalized == "MINISTERIO DE DESARROLLO AGRARIO Y RIEGO"
        )
    ).scalar_one()
    assert org.acronym == "MIDAGRI"
    assert org.organization_type == "MINISTRY"


def test_two_current_spellings_of_a_catalogued_entity_share_one_ficha(ingested_session):
    """ "Instituto Peruano de Energía Nuclear" y "… – IPEN" son la misma entidad
    por dato declarado del catálogo: dos fichas repartirían sus puestos y solo
    una fusión humana podría deshacerlo. La segunda grafía reutiliza la ficha y
    deja su mención apuntándola."""
    from kipu_knowledge.application.ingest import _ResultPersister
    from kipu_knowledge.domain.parsed import ParsedDocument

    doc = ingested_session.execute(select(m.LegalDocument)).scalars().first()
    run = ingested_session.execute(select(m.ExtractionRun)).scalars().first()
    version = ingested_session.get(m.ArtifactVersion, doc.parsed_from_artifact_version_id)
    persister = _ResultPersister(
        ingested_session, doc, run, version, {}, ParsedDocument.model_construct()
    )

    first = persister._organization("Instituto Peruano de Energía Nuclear", None)
    second = persister._organization("Instituto Peruano de Energía Nuclear – IPEN", None)
    assert second.id == first.id
    rows = (
        ingested_session.execute(select(m.Organization).where(m.Organization.acronym == "IPEN"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].organization_type == "PUBLIC_EXECUTING_BODY"
    # La grafía con sigla queda documentada como mención de la misma ficha.
    mention = (
        ingested_session.execute(
            select(m.OrganizationMention).where(
                m.OrganizationMention.text_raw == "Instituto Peruano de Energía Nuclear – IPEN"
            )
        )
        .scalars()
        .one()
    )
    assert mention.canonical_organization_id == first.id
