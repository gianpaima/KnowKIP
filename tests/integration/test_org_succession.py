"""La sucesión de una cartera como vínculo persistente entre sus fichas.

El catálogo declara la cadena de nombres (MIDAGRI → MINAGRI → Ministerio de
Agricultura) con sus normas; `sync-org-catalog` la tiende entre las fichas que
existan y las consultas la recorren sin colapsar las épocas: cada asignación
dice en qué época del nombre ocurrió.
"""

from datetime import date

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.org_catalog_sync import sync_org_catalog
from kipu_knowledge.application.queries import (
    assignments_across_succession,
    succession_chain_ids,
)
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.normalization import normalize_org_name

MIDAGRI = "Ministerio de Desarrollo Agrario y Riego"
MINAGRI = "Ministerio de Agricultura y Riego"


def _org(session, name: str) -> m.Organization:
    org = m.Organization(preferred_name=name, name_normalized=normalize_org_name(name))
    session.add(org)
    session.flush()
    return org


def test_sync_links_the_declared_succession_between_existing_records(session):
    current = _org(session, MIDAGRI)
    former = _org(session, MINAGRI)
    session.commit()

    report = sync_org_catalog(session)
    session.commit()

    assert any(MINAGRI in line for line in report.succession)
    session.expire_all()
    assert session.get(m.Organization, current.id).predecessor_organization_id == former.id
    # Idempotente: una segunda pasada no reporta eslabones nuevos.
    assert sync_org_catalog(session).succession == []


def test_succession_chain_walks_both_directions(session):
    current = _org(session, MIDAGRI)
    former = _org(session, MINAGRI)
    session.commit()
    sync_org_catalog(session)
    session.commit()

    assert succession_chain_ids(session, current.id) == [current.id, former.id]
    assert succession_chain_ids(session, former.id) == [current.id, former.id]


def test_assignments_across_succession_keep_each_era_visible(session, ingest_service):
    """ "¿Cuántos ministros tuvo Agricultura?" se responde a través de la cadena,
    pero cada fila dice en qué época del nombre ocurrió: recorrer no es colapsar."""
    from pathlib import Path

    ingest_service.ingest_fixture("2540702-1", Path(__file__).resolve().parents[2] / "fixtures")
    session.commit()
    # Una mención real cualquiera ancla las asignaciones sintéticas: lo que la
    # consulta recorre es person_id + organization_id, no la mención.
    anchor_mention = session.execute(select(m.PersonMention)).scalars().first()

    current = _org(session, MIDAGRI)
    former = _org(session, MINAGRI)
    session.commit()
    sync_org_catalog(session)
    session.commit()

    for org, name, since in ((former, "ANA QUISPE ROJAS", 2019), (current, "LUIS SOTO VEGA", 2021)):
        person = m.Person(preferred_name=name)
        session.add(person)
        session.flush()
        session.add(
            m.RoleAssignment(
                person_id=person.id,
                person_mention_id=anchor_mention.id,
                organization_id=org.id,
                position_label_raw="Ministro de Estado",
                assignment_kind=e.AssignmentKind.TITULAR,
                valid_from=date(since, 1, 1),
                valid_from_status=e.DateStatus.EXPLICIT,
                valid_to_status=e.DateStatus.NOT_STATED,
            )
        )
    session.commit()

    rows = assignments_across_succession(session, current.id)
    ministros = [row for row in rows if row["position_label"] == "Ministro de Estado"]
    assert len(ministros) == 2
    eras = {row["person_name"]: row["organization_era"] for row in ministros}
    assert eras["ANA QUISPE ROJAS"] == MINAGRI
    assert eras["LUIS SOTO VEGA"] == MIDAGRI
    # Y desde la ficha antigua se ve la misma historia completa.
    former_rows = assignments_across_succession(session, former.id)
    assert [r for r in former_rows if r["position_label"] == "Ministro de Estado"] == ministros
