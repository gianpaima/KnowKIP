"""La página del expediente, servida de verdad: GET /review/persons/{id}.

`test_person_dossier.py` prueba `build_dossier()`; nada probaba la ruta ni la
plantilla, así que un template roto o una etiqueta huérfana ("Mandato: None")
pasaban sin que ninguna prueba lo viera. Aquí se pide la página como lo haría
un revisor, con los tres perfiles que la ficha distingue: quien solo firma,
quien fue designada, y quien comparte grafía con menciones sin resolver.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.person_dossier import build_dossier
from kipu_knowledge.domain import enums as e


def _dossiers(session: Session):
    for person in session.execute(
        select(m.Person).where(m.Person.merged_into_person_id.is_(None))
    ).scalars():
        dossier = build_dossier(session, person.id)
        assert dossier is not None
        yield person, dossier


def test_signatory_only_page_shows_capacity_and_signed_acts(api_client, ingested_session):
    person = next(
        p
        for p, d in _dossiers(ingested_session)
        if d.signing_capacities and not d.appointments and d.signed_acts
    )
    page = api_client.get(f"/review/persons/{person.id}")
    assert page.status_code == 200
    html = page.text
    assert "Cargo con que firma" in html
    assert "Actos que firmó" in html
    # El acto firmado nombra a su sujeto: la página deja de ser solo
    # "aparece en N documentos".
    dossier = build_dossier(ingested_session, person.id)
    subject = dossier.signed_acts[0]["acts"][0]["subjects"][0]["name"]
    assert subject in html


def test_appointed_person_page_shows_rich_conditional_fields(api_client, ingested_session):
    person, dossier = next(
        (p, d)
        for p, d in _dossiers(ingested_session)
        if d.appointments and d.appointments[0]["position_slots"]
    )
    page = api_client.get(f"/review/persons/{person.id}")
    assert page.status_code == 200
    html = page.text
    row = dossier.appointments[0]
    assert row["position_label"] in html
    slot = row["position_slots"][0]
    assert f"{slot['scheme']} {slot['code']}" in html, "el correlativo CAP se muestra"
    assert row["legal_verb_raw"] in html, "el verbo literal del acto se muestra"
    assert row["document"]["title_raw"] in html, "la sumilla del documento se muestra"
    # Condicional de verdad: lo que esta ficha no tiene, no se pinta.
    if not row["mandate"]:
        assert "Mandato:" not in html
    if not (row["document"] and row["document"]["issuer"]):
        assert "Entidad emisora" not in html


def test_unresolved_homonym_page_lists_the_mention_apart(api_client, ingested_session):
    linked = (
        ingested_session.execute(
            select(m.PersonMention).where(m.PersonMention.canonical_person_id.is_not(None))
        )
        .scalars()
        .first()
    )
    assert linked is not None and linked.canonical_person_id
    ingested_session.add(
        m.PersonMention(
            legal_document_id=linked.legal_document_id,
            text_raw=linked.text_raw,
            text_normalized=linked.text_normalized,
            evidence_span_id=linked.evidence_span_id,
            canonical_person_id=None,
            resolution_status=e.ResolutionStatus.CANDIDATE_MATCH,
        )
    )
    ingested_session.commit()

    page = api_client.get(f"/review/persons/{linked.canonical_person_id}")
    assert page.status_code == 200
    assert "Menciones con la misma grafía, sin atribuir" in page.text
    assert "no ha decidido" in page.text


def test_no_person_page_paints_an_empty_field(api_client, ingested_session):
    """Nada se rellena con "None": lo ausente no se pinta, no se maquilla.

    Es la regla de render que pidió el usuario —solo se muestra lo que existe—
    congelada sobre todas las fichas del corpus.
    """
    for person, _dossier in _dossiers(ingested_session):
        page = api_client.get(f"/review/persons/{person.id}")
        assert page.status_code == 200
        assert "None" not in page.text, f"campo vacío pintado en la ficha de {person.id}"


def test_web_context_section_renders_with_citation_anchor(api_client, ingested_session, store):
    """La sección "Contexto público" servida de verdad, con su ancla al acto."""
    from tests.unit.test_web_enrich import (
        ARTICLE_PARAGRAPHS,
        ARTICLE_URL,
        FakeFetcher,
        article_html,
    )

    from kipu_knowledge.application.web_enrich import enrich_person

    person = ingested_session.execute(
        select(m.Person)
        .join(m.PersonMention, m.PersonMention.canonical_person_id == m.Person.id)
        .where(m.PersonMention.text_normalized == "CESAR ALFONSO LUNA VICTORIA LEON")
        .limit(1)
    ).scalar_one()
    fetcher = FakeFetcher({ARTICLE_URL: article_html(ARTICLE_PARAGRAPHS)})
    enrich_person(ingested_session, store, person.id, [ARTICLE_URL], fetcher)
    ingested_session.commit()

    page = api_client.get(f"/review/persons/{person.id}")
    assert page.status_code == 200
    html = page.text
    assert "Contexto público (prensa y web)" in html
    assert "RPP Noticias" in html
    # El ancla al acto ingerido y su condición de contexto, visibles.
    assert "Resolución Suprema 027-2026-EF" in html
    assert "AUTO_LINKED" in html
