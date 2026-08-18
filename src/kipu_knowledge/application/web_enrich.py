"""Enriquecimiento de una persona con contexto web (docs/web-context-design.md).

Orquesta el paso 4 del diseño, sin LLM: URLs explícitas → lista blanca →
captura con `PoliteFetcher` → `web_document` con metadatos declarados →
menciones con guard de homonimia → referencias a normas ya ingeridas →
afirmación determinista `web:cites_official_act`.

Dos decisiones de política encarnadas aquí:
- **El descubrimiento no usa los buscadores de los medios** (sus robots.txt lo
  prohíben a `*`; ver domain/web_sources). Las URLs las trae el operador — o,
  en el futuro, los RSS/sitemaps que los medios sí permiten. Cada URL pedida
  queda en la bitácora del recolector con su desenlace, como el índice diario.
- **El nombre solo nunca vincula.** La mención se vincula a la persona si el
  documento cita una norma ya ingerida que la involucra, o si el párrafo de la
  mención contiene el cargo/organización de una asignación vigente. Sin señal,
  queda UNRESOLVED con tarea WEB_MENTION_RESOLUTION.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge import CRAWLER_VERSION
from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.parsing.web_parser import (
    WEB_PARSER_VERSION,
    ParsedWebPage,
    WebParseError,
    parse_web_page,
)
from kipu_knowledge.adapters.sources.http_capture import (
    CaptureHttpError,
    CaptureNetworkError,
    PoliteFetcher,
)
from kipu_knowledge.application.capture import store_capture
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain import normalization
from kipu_knowledge.domain import web_context as wc
from kipu_knowledge.domain.contracts import ArtifactStore
from kipu_knowledge.domain.normalization import (
    collapse_whitespace,
    normalize_document_number,
    normalize_person_name,
    strip_accents,
)
from kipu_knowledge.domain.web_sources import WebSourceSpec, spec_for_url, url_disallowed_reason
from kipu_knowledge.ontology_version import ONTOLOGY_VERSION

WEB_EXTRACTOR_VERSION = "web-context-deterministic/1.0"

# Señal B del guard: una cadena corroborante más corta que esto no corrobora
# nada ("JEFE" aparece en cualquier párrafo). El umbral descarta etiquetas
# genéricas sin perder nombres de entidad reales.
_MIN_CORROBORATION_CHARS = 10

_DOC_KIND_BY_CITATION = {
    "RESOLUCION SUPREMA": e.DocumentTypeCode.RESOLUCION_SUPREMA,
    "RESOLUCION MINISTERIAL": e.DocumentTypeCode.RESOLUCION_MINISTERIAL,
    "RESOLUCION JEFATURAL": e.DocumentTypeCode.RESOLUCION_JEFATURAL,
    "RESOLUCION DIRECTORAL": e.DocumentTypeCode.RESOLUCION_DIRECTORAL,
    "RESOLUCION DE INTENDENCIA": e.DocumentTypeCode.RESOLUCION_DE_INTENDENCIA,
}


class UrlOutcome(StrEnum):
    INGESTED = "INGERIDA"
    ALREADY_PRESENT = "YA_PRESENTE"
    OUT_OF_WHITELIST = "FUERA_DE_LISTA_BLANCA"
    DISALLOWED_PATH = "RUTA_PROHIBIDA"
    RETRY_PENDING = "REINTENTO_PENDIENTE"
    FAILED = "FALLIDA"


@dataclass(frozen=True)
class UrlResult:
    url: str
    outcome: UrlOutcome
    detail: str
    web_document_id: str | None = None
    mentions: int = 0
    linked_mentions: int = 0
    references: int = 0
    resolved_references: int = 0
    assertions: int = 0
    review_tasks: int = 0


@dataclass
class EnrichmentReport:
    person_id: str
    person_name: str
    crawl_run_id: str
    results: list[UrlResult] = field(default_factory=list)


class EnrichmentError(RuntimeError):
    pass


def suggested_queries(session: Session, person_id: str) -> list[str]:
    """Consultas deterministas para buscar cobertura de la persona.

    Construidas de lo que la base ya sabe: grafías reales, cargos y entidades
    de asignaciones vigentes, y números de las normas que las respaldan. Son
    para el operador (o un conector futuro); el sistema no las lanza solo.
    """
    person = session.get(m.Person, person_id)
    if person is None:
        raise EnrichmentError(f"No existe la persona {person_id}")
    queries: list[str] = []
    seen: set[str] = set()

    def add(query: str) -> None:
        cleaned = collapse_whitespace(query)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            queries.append(cleaned)

    aliases = _alias_spellings(session, person)
    for assignment, corroborations in _assignment_signals(session, person_id):
        entity = corroborations[0] if corroborations else ""
        add(f'"{person.preferred_name}" {entity.title()}')
        doc = _backing_document(session, assignment)
        if doc is not None:
            add(f"{doc.document_type_raw} {doc.number_raw}")
    for alias in aliases:
        add(f'"{alias.title()}"')
    return queries


def _backing_document(session: Session, assignment: m.RoleAssignment) -> m.LegalDocument | None:
    """La norma que sostiene la asignación, para consultas de alta precisión."""
    event_id = assignment.start_event_id or assignment.end_event_id
    if event_id is None:
        return None
    event = session.get(m.PersonnelEvent, event_id)
    if event is None or event.legal_document_id is None:
        return None
    return session.get(m.LegalDocument, event.legal_document_id)


def enrich_person(
    session: Session,
    store: ArtifactStore,
    person_id: str,
    urls: list[str],
    fetcher: PoliteFetcher | None = None,
) -> EnrichmentReport:
    """Captura y procesa URLs de contexto para una persona ya registrada."""
    person = session.get(m.Person, person_id)
    if person is None:
        raise EnrichmentError(f"No existe la persona {person_id}")
    if person.merged_into_person_id:
        raise EnrichmentError(
            f"La ficha {person_id} fue absorbida por {person.merged_into_person_id}; "
            f"enriquece la ficha vigente"
        )
    if not urls:
        raise EnrichmentError("No hay URLs que procesar")

    run = m.CrawlRun(
        crawler_version=CRAWLER_VERSION,
        parameters={"command": "enrich-person", "person_id": person_id, "urls": urls},
    )
    session.add(run)
    session.flush()
    report = EnrichmentReport(
        person_id=person_id, person_name=person.preferred_name, crawl_run_id=run.id
    )

    # Un solo fetcher para toda la corrida: comparte el reloj del rate limit.
    shared_fetcher = fetcher or PoliteFetcher()
    aliases = _alias_spellings(session, person)
    signals = _assignment_signals(session, person_id)

    for url in urls:
        result = _process_url(
            session,
            store,
            run=run,
            person=person,
            aliases=aliases,
            signals=signals,
            url=url,
            fetcher=shared_fetcher,
        )
        report.results.append(result)

    run.completed_at = m.utcnow()
    run.status = "COMPLETED"
    session.flush()
    return report


# ---------------------------------------------------------------------------
# Procesamiento de una URL
# ---------------------------------------------------------------------------


def _process_url(
    session: Session,
    store: ArtifactStore,
    *,
    run: m.CrawlRun,
    person: m.Person,
    aliases: list[str],
    signals: list[tuple[m.RoleAssignment, list[str]]],
    url: str,
    fetcher: PoliteFetcher,
) -> UrlResult:
    code = wc.web_publication_code(url)
    spec = spec_for_url(url)
    if spec is None:
        detail = "dominio fuera de la lista blanca de publicadores (domain/web_sources)"
        _log_item(session, run, code, url, e.CrawlItemStatus.SKIPPED_NOT_RELEVANT, detail)
        return UrlResult(url, UrlOutcome.OUT_OF_WHITELIST, detail)

    disallowed = url_disallowed_reason(spec, url)
    if disallowed is not None:
        _log_item(session, run, code, url, e.CrawlItemStatus.SKIPPED_NOT_RELEVANT, disallowed)
        return UrlResult(url, UrlOutcome.DISALLOWED_PATH, disallowed)

    system = get_or_create_web_source(session, spec)
    item = session.execute(
        select(m.PublicationItem).where(
            m.PublicationItem.source_system_id == system.id,
            m.PublicationItem.source_series == wc.WEB_SOURCE_SERIES,
            m.PublicationItem.publication_code == code,
        )
    ).scalar_one_or_none()
    if item is not None:
        existing = session.execute(
            select(m.WebDocument).where(m.WebDocument.publication_item_id == item.id)
        ).scalar_one_or_none()
        if existing is not None:
            detail = f"ya capturada y parseada como web_document {existing.id}"
            _log_item(session, run, code, url, e.CrawlItemStatus.ALREADY_PRESENT, detail, item=item)
            return UrlResult(url, UrlOutcome.ALREADY_PRESENT, detail, web_document_id=existing.id)
    else:
        item = m.PublicationItem(
            source_system_id=system.id,
            source_series=wc.WEB_SOURCE_SERIES,
            publication_code=code,
            canonical_url=wc.canonicalize_url(url),
        )
        session.add(item)
        session.flush()

    try:
        content, capture = fetcher.get(url)
    except (CaptureHttpError, CaptureNetworkError) as exc:
        detail = str(exc)
        _log_item(
            session,
            run,
            code,
            url,
            e.CrawlItemStatus.RETRY_PENDING,
            detail,
            item=item,
            error=detail,
        )
        return UrlResult(url, UrlOutcome.RETRY_PENDING, detail)

    outcome = store_capture(
        session,
        store,
        item=item,
        representation=e.RepresentationType.HTML,
        content=content,
        capture=capture,
    )
    try:
        page = parse_web_page(content)
    except WebParseError as exc:
        detail = f"captura guardada ({outcome.sha256[:12]}) pero no parseable: {exc}"
        _log_item(
            session, run, code, url, e.CrawlItemStatus.FAILED, detail, item=item, error=str(exc)
        )
        return UrlResult(url, UrlOutcome.FAILED, detail)

    # La página puede declarar su propia URL canónica; si lo hace, es mejor
    # puntero que la URL pedida — pero el código NO cambia: nació de la URL
    # con que se pidió y cambiarlo partiría la identidad de la publicación.
    if page.canonical_url_declared:
        item.canonical_url = wc.canonicalize_url(page.canonical_url_declared)

    version = session.get(m.ArtifactVersion, outcome.version_id)
    assert version is not None
    doc = m.WebDocument(
        publication_item_id=item.id,
        kind=page.kind,
        headline_raw=page.headline_raw,
        published_at_raw=page.published_at_raw,
        published_at=page.published_at,
        modified_at_raw=page.modified_at_raw,
        author_raw=page.author_raw,
        account_raw=None,
        section_raw=page.section_raw,
        language=page.language,
        body_scope=page.body_scope,
        parsed_from_artifact_version_id=version.id,
    )
    session.add(doc)
    session.flush()

    references, resolved = _extract_references(session, doc, page, version)
    mentions, linked, tasks = _extract_mentions(
        session,
        doc,
        page,
        version,
        person=person,
        aliases=aliases,
        signals=signals,
        resolved_references=resolved,
    )
    assertions = _assert_cited_acts(session, version, person, linked, resolved)

    detail = (
        f"menciones={len(mentions)} vinculadas={len(linked)} "
        f"referencias={len(references)} resueltas={len(resolved)} "
        f"afirmaciones={assertions} tareas={tasks} cuerpo={doc.body_scope}"
    )
    _log_item(session, run, code, url, e.CrawlItemStatus.INGESTED, detail, item=item)
    return UrlResult(
        url,
        UrlOutcome.INGESTED,
        detail,
        web_document_id=doc.id,
        mentions=len(mentions),
        linked_mentions=len(linked),
        references=len(references),
        resolved_references=len(resolved),
        assertions=assertions,
        review_tasks=tasks,
    )


def get_or_create_web_source(session: Session, spec: WebSourceSpec) -> m.SourceSystem:
    row = session.execute(
        select(m.SourceSystem).where(m.SourceSystem.name == spec.name)
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = m.SourceSystem(
        name=spec.name,
        base_url=spec.base_url,
        source_family=spec.source_family,
        authority=spec.authority,
        # La inspección fechada vive en domain/web_sources y en source-policy.md.
        policy_status=f"INSPECTED_{spec.inspected_on.isoformat()}",
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Referencias a normas
# ---------------------------------------------------------------------------


def _extract_references(
    session: Session,
    doc: m.WebDocument,
    page: ParsedWebPage,
    version: m.ArtifactVersion,
) -> tuple[list[m.WebReference], dict[str, m.LegalDocument]]:
    """Normas citadas por el documento, ancladas a su párrafo.

    Devuelve además las resueltas contra el corpus, indexadas por número
    normalizado: son la señal A del guard de homonimia y el objeto de la
    afirmación determinista.
    """
    references: list[m.WebReference] = []
    resolved: dict[str, m.LegalDocument] = {}
    seen: set[tuple[str, str]] = set()
    for index, paragraph in enumerate(page.paragraphs):
        for citation in wc.find_norm_citations(paragraph):
            number = normalize_document_number(citation.number_raw)
            kind_norm = collapse_whitespace(strip_accents(citation.kind_raw)).upper()
            if (kind_norm, number) in seen:
                continue
            seen.add((kind_norm, number))
            target = _resolve_citation(session, kind_norm, number)
            span = _paragraph_span(session, version, paragraph, index)
            references.append(
                m.WebReference(
                    web_document_id=doc.id,
                    reference_type=e.ReferenceType.NORMATIVE_CITATION,
                    target_document_id=target.id if target else None,
                    target_number_raw=f"{citation.kind_raw} {citation.number_raw}",
                    target_doc_kind_raw=citation.kind_raw,
                    evidence_span_id=span.id,
                )
            )
            if target is not None:
                resolved[number] = target
    session.add_all(references)
    session.flush()
    return references, resolved


def _resolve_citation(session: Session, kind_norm: str, number: str) -> m.LegalDocument | None:
    """El documento ingerido que responde a la cita, si hay exactamente uno.

    Con el tipo catalogado se exige que coincida; dos candidatos no eligen:
    un anclaje ambiguo es peor que ninguno.
    """
    query = select(m.LegalDocument).where(m.LegalDocument.number_normalized == number)
    doc_kind = _DOC_KIND_BY_CITATION.get(kind_norm)
    if doc_kind is not None:
        query = query.where(m.LegalDocument.document_type_code == doc_kind)
    rows = session.execute(query).scalars().all()
    return rows[0] if len(rows) == 1 else None


# ---------------------------------------------------------------------------
# Menciones y guard de homonimia
# ---------------------------------------------------------------------------


def _extract_mentions(
    session: Session,
    doc: m.WebDocument,
    page: ParsedWebPage,
    version: m.ArtifactVersion,
    *,
    person: m.Person,
    aliases: list[str],
    signals: list[tuple[m.RoleAssignment, list[str]]],
    resolved_references: dict[str, m.LegalDocument],
) -> tuple[list[m.WebPersonMention], list[m.WebPersonMention], int]:
    citation_basis = _citation_link_basis(session, person, resolved_references)
    mentions: list[m.WebPersonMention] = []
    linked: list[m.WebPersonMention] = []
    tasks = 0
    seen_spellings: set[str] = set()
    for index, paragraph in enumerate(page.paragraphs):
        for match, spelling in _name_forms_in(paragraph, aliases):
            if spelling in seen_spellings:
                continue
            seen_spellings.add(spelling)
            span = _paragraph_span(session, version, paragraph, index)
            matched_by = citation_basis or _paragraph_link_basis(paragraph, signals)
            mention = m.WebPersonMention(
                web_document_id=doc.id,
                text_raw=match,
                text_normalized=spelling,
                evidence_span_id=span.id,
                canonical_person_id=person.id if matched_by else None,
                resolution_status=(
                    e.ResolutionStatus.AUTO_LINKED if matched_by else e.ResolutionStatus.UNRESOLVED
                ),
                matched_by=matched_by,
            )
            session.add(mention)
            session.flush()
            mentions.append(mention)
            if matched_by:
                linked.append(mention)
            else:
                session.add(
                    m.ReviewTask(
                        task_type=e.ReviewTaskType.WEB_MENTION_RESOLUTION,
                        target_type="web_person_mention",
                        target_id=mention.id,
                        reason=(
                            f"«{match}» coincide con una grafía de {person.preferred_name} "
                            f"pero el documento web no aporta señal corroborante "
                            f"(ni norma ingerida citada, ni cargo/entidad de una "
                            f"asignación vigente en el mismo párrafo)"
                        ),
                    )
                )
                tasks += 1
    session.flush()
    return mentions, linked, tasks


def _citation_link_basis(
    session: Session, person: m.Person, resolved: dict[str, m.LegalDocument]
) -> str | None:
    """Señal A: el documento cita una norma ingerida que involucra a la persona."""
    for number, target in resolved.items():
        involves = session.execute(
            select(m.PersonMention.id)
            .where(
                m.PersonMention.legal_document_id == target.id,
                m.PersonMention.canonical_person_id == person.id,
            )
            .limit(1)
        ).first()
        if involves is not None:
            return (
                f"cita {target.document_type_raw} {target.number_raw} "
                f"({number}), acto ingerido que involucra a la persona"
            )
    return None


def _paragraph_link_basis(
    paragraph: str, signals: list[tuple[m.RoleAssignment, list[str]]]
) -> str | None:
    """Señal B: el párrafo de la mención nombra el cargo/entidad de una asignación vigente."""
    normalized = collapse_whitespace(strip_accents(paragraph)).upper()
    for assignment, corroborations in signals:
        for text in corroborations:
            if text in normalized:
                return (
                    f"el párrafo contiene «{text.title()}» de la asignación vigente {assignment.id}"
                )
    return None


def _assignment_signals(
    session: Session, person_id: str
) -> list[tuple[m.RoleAssignment, list[str]]]:
    """Cadenas corroborantes por asignación viva: entidad, ruta y etiqueta de cargo."""
    signals: list[tuple[m.RoleAssignment, list[str]]] = []
    assignments = (
        session.execute(
            select(m.RoleAssignment).where(
                m.RoleAssignment.person_id == person_id,
                m.RoleAssignment.superseded_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for assignment in assignments:
        candidates: list[str] = []
        org = (
            session.get(m.Organization, assignment.organization_id)
            if assignment.organization_id
            else None
        )
        if org is None and assignment.position_id:
            position = session.get(m.Position, assignment.position_id)
            if position is not None and position.organization_id:
                org = session.get(m.Organization, position.organization_id)
        for raw in (
            org.preferred_name if org else None,
            assignment.organization_path_raw,
            assignment.position_label_raw,
        ):
            if not raw:
                continue
            normalized = collapse_whitespace(strip_accents(raw)).upper()
            if len(normalized) >= _MIN_CORROBORATION_CHARS and normalized not in candidates:
                candidates.append(normalized)
        if candidates:
            signals.append((assignment, candidates))
    return signals


def _alias_spellings(session: Session, person: m.Person) -> list[str]:
    """Grafías conocidas de la persona, la preferida primero."""
    spellings = [normalize_person_name(person.preferred_name)]
    for spelling in session.execute(
        select(m.PersonMention.text_normalized)
        .where(m.PersonMention.canonical_person_id == person.id)
        .distinct()
    ).scalars():
        if spelling not in spellings:
            spellings.append(spelling)
    return spellings


def _name_forms_in(paragraph: str, aliases: list[str]) -> list[tuple[str, str]]:
    """Apariciones de la persona en el párrafo: (como se escribió, normalizada).

    Recorre las secuencias de palabras capitalizadas del texto original y
    acepta las que son forma corta legítima de alguna grafía conocida
    (subsecuencia ordenada con apellido; domain/web_context). Devuelve lo que
    la fuente escribió, tildes incluidas: la grafía no se fabrica.
    """
    found: list[tuple[str, str]] = []
    alias_tokens = [(alias, normalization.person_name_tokens(alias)) for alias in aliases]
    for run in wc.NAME_RUN_RE.finditer(paragraph):
        run_raw = run.group(0)
        # El guion de estilo editorial ("César Luna-Victoria", observado en RPP
        # el 2026-08-18) se compara como espacio: la forma escrita se conserva
        # en text_raw; solo la comparación lo separa.
        run_normalized = normalization.normalize_person_name(run_raw.replace("-", " "))
        run_toks = normalization.person_name_tokens(run_normalized)
        if any(wc.is_short_name_form(run_toks, full) for _, full in alias_tokens):
            found.append((run_raw, run_normalized))
    return found


# ---------------------------------------------------------------------------
# Evidencia y afirmaciones
# ---------------------------------------------------------------------------


def _paragraph_span(
    session: Session, version: m.ArtifactVersion, paragraph: str, index: int
) -> m.EvidenceSpan:
    """Cita textual anclada a la captura: el párrafo entero, con su localizador.

    El párrafo es la unidad citable de la prosa periodística (como la sección
    lo es del corpus oficial): la re-verificación re-extrae los párrafos de los
    bytes del CAS con el parser versionado y compara por índice y sha256.
    """
    span = m.EvidenceSpan(
        artifact_version_id=version.id,
        quoted_text=paragraph,
        quoted_text_sha256=hashlib.sha256(paragraph.encode("utf-8")).hexdigest(),
        locator_json={"paragraph_index": index, "parser_version": WEB_PARSER_VERSION},
    )
    session.add(span)
    session.flush()
    return span


def _assert_cited_acts(
    session: Session,
    version: m.ArtifactVersion,
    person: m.Person,
    linked: list[m.WebPersonMention],
    resolved: dict[str, m.LegalDocument],
) -> int:
    """Afirmación determinista `web:cites_official_act` por cada norma resuelta.

    Solo con mención vinculada: sin persona resuelta, la afirmación no tiene
    sujeto y espera. La corrida registra parser y extractor con sus versiones;
    la confianza no es 1.0 porque el anclaje número→documento, aunque exacto,
    depende de la normalización del número.
    """
    if not linked or not resolved:
        return 0
    run = m.ExtractionRun(
        artifact_version_id=version.id,
        parser_version=WEB_PARSER_VERSION,
        extractor_version=WEB_EXTRACTOR_VERSION,
        ontology_version=ONTOLOGY_VERSION,
        status=e.ExtractionStatus.COMPLETED,
        completed_at=m.utcnow(),
    )
    session.add(run)
    session.flush()
    count = 0
    reference_rows = session.execute(
        select(m.WebReference).where(
            m.WebReference.web_document_id == linked[0].web_document_id,
            m.WebReference.target_document_id.is_not(None),
        )
    ).scalars()
    for reference in reference_rows:
        session.add(
            m.Assertion(
                extraction_run_id=run.id,
                subject_type="person",
                subject_id=person.id,
                predicate=wc.CITES_OFFICIAL_ACT,
                object_type="legal_document",
                object_id=reference.target_document_id,
                object_value_json={"target_number_raw": reference.target_number_raw},
                confidence=0.9,
                evidence_span_id=reference.evidence_span_id,
                review_status=e.ReviewStatus.CANDIDATE,
            )
        )
        count += 1
    session.flush()
    return count


# ---------------------------------------------------------------------------
# Bitácora del recolector
# ---------------------------------------------------------------------------


def _log_item(
    session: Session,
    run: m.CrawlRun,
    code: str,
    url: str,
    status: e.CrawlItemStatus,
    detail: str,
    *,
    item: m.PublicationItem | None = None,
    error: str | None = None,
) -> None:
    """Cada URL pedida deja rastro con su desenlace, como en el índice diario."""
    relevance = (
        e.Relevance.NOT_RELEVANT
        if status == e.CrawlItemStatus.SKIPPED_NOT_RELEVANT
        else e.Relevance.RELEVANT
    )
    session.add(
        m.CrawlItem(
            crawl_run_id=run.id,
            source_series=wc.WEB_SOURCE_SERIES,
            publication_code=code,
            canonical_url=url,
            relevance=relevance,
            relevance_rule="enrich-person/1.0",
            relevance_rationale=detail,
            status=status,
            attempts=1,
            last_error=error,
            outcome_detail=detail,
            publication_item_id=item.id if item is not None else None,
        )
    )
    session.flush()
