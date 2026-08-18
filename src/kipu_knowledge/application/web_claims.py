"""Clasificación de afirmaciones de contexto sobre un documento web capturado.

Paso 5 del diseño (docs/web-context-design.md §5.4). El clasificador —un LLM—
solo propone; aquí se decide qué entra, con reglas mecánicas:

- El texto se re-extrae de los bytes del CAS con el parser versionado, no de
  la red: clasificar sobre otra cosa que la evidencia rompería la cadena.
- Cada cita propuesta debe aparecer **byte a byte** en ese texto. La que no,
  se descarta y se cuenta: el modelo no goza de confianza, se le verifica.
- Solo documentos con mención vinculada a una persona (el sujeto de las
  afirmaciones); sin sujeto resuelto no se clasifica nada.
- `web:cites_official_act` no le pertenece al modelo: esa afirmación es del
  extractor determinista y aquí se filtra para que no haya dos orígenes del
  mismo predicado.
- Re-clasificar el mismo documento con la misma corrida vigente no duplica:
  las afirmaciones anteriores de esa captura se reemplazan con supersede.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.extraction.web_llm import WebContextClaim, WebContextClassifier
from kipu_knowledge.adapters.parsing.web_parser import WEB_PARSER_VERSION, parse_web_page
from kipu_knowledge.application.source_links import verified_bytes
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain import web_context as wc
from kipu_knowledge.domain.contracts import ArtifactStore
from kipu_knowledge.ontology_version import ONTOLOGY_VERSION

WEB_CLAIMS_EXTRACTOR_VERSION = "web-context-claims/1.0"


class ClassificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimOutcome:
    predicate: str
    quote_preview: str
    accepted: bool
    reason: str


@dataclass
class ClassificationReport:
    web_document_id: str
    person_id: str
    extraction_run_id: str | None
    accepted: int
    rejected: int
    superseded: int
    outcomes: list[ClaimOutcome]


def classify_web_document(
    session: Session,
    store: ArtifactStore,
    classifier: WebContextClassifier,
    web_document_id: str,
) -> ClassificationReport:
    doc = session.get(m.WebDocument, web_document_id)
    if doc is None:
        raise ClassificationError(f"No existe el documento web {web_document_id}")

    person = _linked_person(session, doc)
    if person is None:
        raise ClassificationError(
            f"El documento {web_document_id} no tiene mención vinculada a ninguna persona: "
            f"sin sujeto resuelto no se clasifica (resuelve antes la tarea "
            f"WEB_MENTION_RESOLUTION)"
        )

    version = session.get(m.ArtifactVersion, doc.parsed_from_artifact_version_id)
    if version is None:
        raise ClassificationError(
            f"No existe la versión de artefacto {doc.parsed_from_artifact_version_id}"
        )
    content = verified_bytes(store, version)
    if content is None:
        raise ClassificationError(
            f"Los bytes de la captura {version.object_key} no están en el CAS o no "
            f"coinciden con su sha256: no se clasifica sobre evidencia dudosa"
        )
    page = parse_web_page(content)
    if not page.paragraphs:
        raise ClassificationError(
            f"El documento {web_document_id} no tiene cuerpo extraíble "
            f"(body_scope={doc.body_scope}): no hay texto que clasificar"
        )
    full_text = page.full_text

    claims = classifier.classify(person.preferred_name, full_text)
    outcomes: list[ClaimOutcome] = []
    verified: list[tuple[WebContextClaim, int]] = []
    for claim in claims:
        reason, paragraph_index = _verify(claim, page.paragraphs)
        if reason is not None:
            outcomes.append(ClaimOutcome(claim.predicate, _preview(claim.quote), False, reason))
            continue
        verified.append((claim, paragraph_index))
        outcomes.append(ClaimOutcome(claim.predicate, _preview(claim.quote), True, "verificada"))

    run: m.ExtractionRun | None = None
    superseded = 0
    if verified:
        run = m.ExtractionRun(
            artifact_version_id=version.id,
            parser_version=WEB_PARSER_VERSION,
            extractor_version=WEB_CLAIMS_EXTRACTOR_VERSION,
            ontology_version=ONTOLOGY_VERSION,
            model_provider=classifier.provider,
            model_name=classifier.model,
            prompt_version=classifier.prompt_version,
            status=e.ExtractionStatus.COMPLETED,
            completed_at=m.utcnow(),
        )
        session.add(run)
        session.flush()
        superseded = _supersede_previous_claims(session, version, person, run)
        for claim, paragraph_index in verified:
            span = m.EvidenceSpan(
                artifact_version_id=version.id,
                quoted_text=claim.quote,
                quoted_text_sha256=hashlib.sha256(claim.quote.encode("utf-8")).hexdigest(),
                locator_json={
                    "paragraph_index": paragraph_index,
                    "parser_version": WEB_PARSER_VERSION,
                },
            )
            session.add(span)
            session.flush()
            session.add(
                m.Assertion(
                    extraction_run_id=run.id,
                    subject_type="person",
                    subject_id=person.id,
                    predicate=claim.predicate,
                    object_type=None,
                    object_id=None,
                    object_value_json=claim.data or None,
                    # El techo lo pone la naturaleza de la fuente, no el modelo:
                    # es lo que un medio afirma, verificado como cita, nada más.
                    confidence=0.7,
                    evidence_span_id=span.id,
                    review_status=e.ReviewStatus.CANDIDATE,
                )
            )
        session.flush()

    return ClassificationReport(
        web_document_id=web_document_id,
        person_id=person.id,
        extraction_run_id=run.id if run else None,
        accepted=len(verified),
        rejected=len(outcomes) - len(verified),
        superseded=superseded,
        outcomes=outcomes,
    )


def _linked_person(session: Session, doc: m.WebDocument) -> m.Person | None:
    mention = session.execute(
        select(m.WebPersonMention)
        .where(
            m.WebPersonMention.web_document_id == doc.id,
            m.WebPersonMention.canonical_person_id.is_not(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if mention is None:
        return None
    return session.get(m.Person, mention.canonical_person_id)


def _verify(claim: WebContextClaim, paragraphs: tuple[str, ...]) -> tuple[str | None, int]:
    """Veredicto mecánico sobre una afirmación propuesta.

    Devuelve (None, índice_de_párrafo) si la cita aparece literalmente;
    (razón, -1) si se rechaza. El vocabulario se re-comprueba aquí aunque el
    adaptador ya filtre: ninguna capa depende sola de la otra.
    """
    if claim.predicate not in wc.CONTEXT_PREDICATES:
        return (f"predicado fuera del vocabulario: {claim.predicate}", -1)
    if claim.predicate == wc.CITES_OFFICIAL_ACT:
        return ("web:cites_official_act es del extractor determinista, no del modelo", -1)
    quote = claim.quote.strip()
    if len(quote) < 15:
        return ("cita demasiado corta para ser verificable sin ambigüedad", -1)
    for index, paragraph in enumerate(paragraphs):
        if quote in paragraph:
            return (None, index)
    return ("la cita no aparece byte a byte en el texto extraído de la captura", -1)


def _supersede_previous_claims(
    session: Session, version: m.ArtifactVersion, person: m.Person, new_run: m.ExtractionRun
) -> int:
    """Reemplaza las afirmaciones LLM vigentes de esta captura y persona.

    Re-clasificar es corregir la lectura, no acumular lecturas: las anteriores
    quedan SUPERSEDED (nunca borradas), enlazadas por la corrida nueva. Las del
    extractor determinista no se tocan: son de otro origen y otra regla.
    """
    previous = (
        session.execute(
            select(m.Assertion)
            .join(m.ExtractionRun, m.ExtractionRun.id == m.Assertion.extraction_run_id)
            .where(
                m.ExtractionRun.artifact_version_id == version.id,
                m.ExtractionRun.extractor_version == WEB_CLAIMS_EXTRACTOR_VERSION,
                m.ExtractionRun.id != new_run.id,
                m.Assertion.subject_id == person.id,
                m.Assertion.superseded_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for assertion in previous:
        assertion.superseded_at = m.utcnow()
        assertion.review_status = e.ReviewStatus.SUPERSEDED
    return len(previous)


def _preview(quote: str, limit: int = 80) -> str:
    cleaned = " ".join(quote.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
