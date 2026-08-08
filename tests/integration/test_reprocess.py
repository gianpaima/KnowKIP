"""Reproceso de una publicación ya ingerida.

El reproceso re-extrae desde el artefacto inmutable ya capturado: es la única vía
de reparación que no inventa datos, porque vuelve a leer los mismos bytes con el
extractor corregido. Que funcione de extremo a extremo no estaba cubierto, y no
funcionaba: el borrado de las filas derivadas violaba claves foráneas que SQLite
no comprobaba en las pruebas y PostgreSQL sí rechaza en producción.
"""

from pathlib import Path

from sqlalchemy import func, select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

# Caso F: encargatura cuya asignación de rol cuelga del evento por `start_event_id`.
# Es el que exhibe el orden de borrado entre `role_assignment` y `personnel_event`.
ENCARGO_CODE = "2540702-1"


def _counts(session) -> dict[str, int]:
    return {
        table.__tablename__: session.execute(select(func.count(table.id))).scalar_one()
        for table in (
            m.PersonnelEvent,
            m.RoleAssignment,
            m.EventParticipant,
            m.LegalDocument,
            m.DocumentSection,
            m.PersonMention,
        )
        # organization_mention queda fuera a propósito: `_organization` solo la
        # escribe cuando la organización es nueva, así que un reproceso —donde la
        # organización ya existe— no la recrea. Es un hueco de evidencia propio,
        # anterior a esto y de alcance mayor: afecta también a cualquier segundo
        # documento que nombre una organización ya conocida.
    }


def _ingest_then_forget(session, ingest_service) -> None:
    """Ingiere y vacía el identity map.

    El reproceso real corre en una sesión nueva (`session_scope` del CLI). Con las
    filas aún en el identity map de la ingesta, la unit of work las ordena por otro
    camino y los fallos de orden de borrado no se manifiestan.
    """
    ingest_service.ingest_fixture(ENCARGO_CODE, FIXTURES_DIR)
    session.commit()
    session.expunge_all()


def test_reprocess_rebuilds_the_document_without_violating_foreign_keys(session, ingest_service):
    """Regresión: `kipu reprocess` abortaba con ForeignKeyViolation.

    Los modelos no declaran `relationship()`, así que la unit of work no deduce que
    role_assignment depende de personnel_event —las FKs del metadata no ordenan por
    sí solas— y emitía el DELETE del evento primero. Detrás venían las menciones de
    organización, que nadie retiraba. El resultado era que ningún documento mal
    extraído tenía vía de reparación.
    """
    _ingest_then_forget(session, ingest_service)
    before = _counts(session)
    assert before["role_assignment"] > 0, "el caso debe producir al menos una asignación"

    outcome = ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    assert outcome.legal_document_id is not None
    assert _counts(session) == before, "el reproceso reconstruye lo mismo, no duplica"


def test_reprocess_supersedes_previous_assertions(session, ingest_service):
    """Las afirmaciones previas no se borran: quedan SUPERSEDED para auditoría."""
    _ingest_then_forget(session, ingest_service)

    ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    statuses = session.execute(select(m.Assertion.review_status)).scalars().all()
    assert e.ReviewStatus.SUPERSEDED in statuses
    # Y siguen existiendo afirmaciones vivas: las que produjo la corrida nueva.
    assert any(s != e.ReviewStatus.SUPERSEDED for s in statuses)


def test_superseded_evidence_keeps_its_quote_after_sections_are_rebuilt(session, ingest_service):
    """La evidencia de una afirmación supersedida sobrevive a su sección.

    `evidence_span.document_section_id` se suelta al reconstruir las secciones, pero
    la cita literal y su sha256 —lo que hace auditable la afirmación— siguen ahí.
    """
    _ingest_then_forget(session, ingest_service)

    ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    detached = (
        session.execute(select(m.EvidenceSpan).where(m.EvidenceSpan.document_section_id.is_(None)))
        .scalars()
        .all()
    )
    assert detached, "el reproceso debe dejar spans desanclados de la sección vieja"
    for span in detached:
        assert span.quoted_text
        assert span.quoted_text_sha256
        assert span.artifact_version_id


def _precedent_on_a_mention_of(session, code: str) -> tuple[str, str]:
    """Registra un precedente humano sobre una mención del documento dado."""
    mention = (
        session.execute(
            select(m.PersonMention)
            .join(m.LegalDocument, m.LegalDocument.id == m.PersonMention.legal_document_id)
            .join(m.PublicationItem, m.PublicationItem.id == m.LegalDocument.publication_item_id)
            .where(m.PublicationItem.publication_code == code)
        )
        .scalars()
        .first()
    )
    assert mention is not None and mention.canonical_person_id
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
        target_type="person_mention",
        target_id=mention.id,
        reason="prueba",
        status=e.ReviewTaskStatus.RESOLVED,
    )
    session.add(task)
    session.flush()
    decision = m.ReviewDecision(
        review_task_id=task.id, action=e.DecisionAction.LINK_ENTITY, reviewer="prueba"
    )
    session.add(decision)
    session.flush()
    precedent = m.IdentityPrecedent(
        subject_type="person",
        name_normalized=mention.text_normalized,
        role_context=mention.role_context_normalized,
        person_id=mention.canonical_person_id,
        source_person_mention_id=mention.id,
        review_decision_id=decision.id,
        reviewer="prueba",
    )
    session.add(precedent)
    session.flush()
    return precedent.id, mention.text_normalized


def test_reprocess_repoints_a_human_precedent_to_the_rebuilt_mention(session, ingest_service):
    """Un precedente cita la mención que motivó la decisión humana.

    Esa mención se retira al re-extraer, pero la decisión sigue en pie. Si el
    puntero se perdiera, la cadena que va de una vinculación automática a la
    decisión que la autoriza quedaría rota — y esa cadena es el único motivo
    por el que vincular sin preguntar es admisible.
    """
    _ingest_then_forget(session, ingest_service)
    precedent_id, name_normalized = _precedent_on_a_mention_of(session, ENCARGO_CODE)
    session.commit()
    session.expunge_all()

    ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    precedent = session.get(m.IdentityPrecedent, precedent_id)
    assert precedent is not None, "el precedente no se borra al re-extraer"
    assert precedent.source_person_mention_id is not None, "vuelve a apuntar a una mención"
    mention = session.get(m.PersonMention, precedent.source_person_mention_id)
    assert mention is not None
    assert mention.text_normalized == name_normalized, "y a la equivalente, no a cualquiera"


def test_reprocess_leaves_no_empty_person_behind(session, ingest_service):
    """Una ficha que la extracción anterior creó y esta ya no sostiene es residuo.

    Si se quedara, la mención equivalente crearía otra ficha y habría dos donde
    hay una persona, con una de ellas vacía. Una ficha vacía se lee como "de
    esta persona no consta nada", que es distinto de "esta persona no existe".
    """
    _ingest_then_forget(session, ingest_service)
    ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    empty = (
        session.execute(
            select(m.Person).where(
                m.Person.merged_into_person_id.is_(None),
                ~select(m.PersonMention.id)
                .where(m.PersonMention.canonical_person_id == m.Person.id)
                .exists(),
            )
        )
        .scalars()
        .all()
    )
    assert empty == [], f"fichas sin ninguna mención tras el reproceso: {[p.id for p in empty]}"


def test_reprocess_keeps_the_corroborating_sources_a_human_linked(session, ingest_service):
    """`kipu link-source --matched-by` es trabajo humano; re-extraer no lo deshace.

    El acto no cambió al releerlo: dónde más está publicado sigue siendo cierto.
    """
    _ingest_then_forget(session, ingest_service)
    doc_id = session.execute(
        select(m.LegalDocument.id)
        .join(m.PublicationItem, m.PublicationItem.id == m.LegalDocument.publication_item_id)
        .where(m.PublicationItem.publication_code == ENCARGO_CODE)
    ).scalar_one()
    other = m.PublicationItem(
        source_system_id=session.execute(select(m.SourceSystem.id)).scalars().first(),
        source_series="WEB",
        publication_code="portal-de-la-entidad",
        canonical_url="https://www.gob.pe/institucion/x/normas-legales/1",
    )
    session.add(other)
    session.flush()
    other_id = other.id
    session.add(
        m.DocumentSource(
            legal_document_id=doc_id,
            publication_item_id=other_id,
            role=e.DocumentSourceRole.CORROBORATING,
            matched_by="mismo número de resolución y misma fecha (decisión del operador)",
        )
    )
    session.commit()
    session.expunge_all()

    ingest_service.reprocess(ENCARGO_CODE)
    session.commit()

    carried = (
        session.execute(
            select(m.DocumentSource).where(
                m.DocumentSource.role == e.DocumentSourceRole.CORROBORATING,
                m.DocumentSource.publication_item_id == other_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(carried) == 1, "el enlace corroborante viaja al documento reconstruido"
    assert "decisión del operador" in carried[0].matched_by
