"""Recolección diaria: descubrir el índice de una fecha e ingerir lo que toca.

Es el paso que convierte la ingesta de un acto manual —una URL a la vez— en un
proceso desatendido. Lo desatendido cambia las exigencias: nadie está mirando
cuando algo falla, así que todo lo que se ve y todo lo que se decide tiene que
quedar escrito.

Cómo funciona una corrida:

1. Se recorre el índice de la fecha (`ElPeruanoSourceAdapter.iter_listing_pages`)
   y **se archivan sus bytes** como representación LISTING de la edición. El
   índice es la constancia de qué dijo la fuente que se publicó ese día; sin él,
   "se ingirieron 19 normas" no se puede contrastar con nada.
2. Cada dispositivo descubierto se anota en `crawl_item` con lo que el catálogo
   declaraba y con el veredicto del filtro de relevancia, se ingiera o no.
3. Se ingiere lo relevante y lo no catalogado; lo descartado queda registrado
   con su motivo y su regla, nunca omitido en silencio.
4. Los fallos se clasifican. Lo transitorio —404 pasajero, timeout, 5xx— queda
   en RETRY_PENDING para un `kipu retry-pending` explícito; nunca se reintenta
   dentro del mismo recorrido, porque insistir en el acto contra un servidor que
   acaba de fallar es exactamente lo que la política de fuente prohíbe.

Idempotencia: un dispositivo ya ingerido no se vuelve a ingerir (queda
ALREADY_PRESENT), y una captura de bytes idénticos no abre versión nueva. Correr
dos veces el mismo día no duplica nada.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from kipu_knowledge import CRAWLER_VERSION
from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.parsing.html_parser import ParseError
from kipu_knowledge.adapters.parsing.listing_parser import ListingParseError, parse_listing
from kipu_knowledge.adapters.sources.elperuano import BASE_URL, ElPeruanoSourceAdapter
from kipu_knowledge.adapters.sources.http_capture import (
    CaptureHttpError,
    CaptureNetworkError,
    LiveSourceDisabled,
)
from kipu_knowledge.application.capture import (
    CaptureError,
    capture_device_pdf,
    ensure_issue,
    store_capture,
)
from kipu_knowledge.application.ingest import IngestService
from kipu_knowledge.application.source_links import official_publication_item, verified_bytes
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import ArtifactStore, ListingEntry
from kipu_knowledge.domain.relevance import classify_summary

# Estados HTTP que no significan "este documento no existe". El 404 está en la
# lista por observación directa: el 2026-08-07 la URL del PDF de 2540861-1
# devolvió 404 y 200 minutos después respondió el archivo con la misma URL.
_TRANSIENT_STATUS = frozenset({404, 408, 409, 425, 429, 500, 502, 503, 504})

_LISTED_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@dataclass(frozen=True)
class ItemResult:
    publication_code: str
    status: e.CrawlItemStatus
    relevance: e.Relevance
    detail: str
    # Eventos de personal extraídos, o None si no se llegó a extraer nada.
    events_extracted: int | None = None

    @property
    def relevant_but_empty(self) -> bool:
        """Se dijo que era un acto de personal y no salió ninguno.

        No es un error: el documento está capturado y su texto íntegro. Es un
        hueco del extractor, y se cuenta aparte para que se vea.
        """
        return self.relevance is e.Relevance.RELEVANT and self.events_extracted == 0


@dataclass
class DailyRunResult:
    publication_date: date
    series: str
    dry_run: bool
    crawl_run_id: str | None = None
    issue_code: str | None = None
    total_declared: int = 0
    listing_pages: int = 0
    items: list[ItemResult] = field(default_factory=list)
    error_summary: str | None = None

    @property
    def relevant_but_empty(self) -> list[ItemResult]:
        """Dispositivos que el filtro llamó de personal y no dieron ningún evento."""
        return [item for item in self.items if item.relevant_but_empty]

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for item in self.items:
            tally[str(item.status)] = tally.get(str(item.status), 0) + 1
        return tally

    @property
    def status(self) -> str:
        if self.error_summary:
            return "FAILED"
        problems = {e.CrawlItemStatus.FAILED, e.CrawlItemStatus.RETRY_PENDING}
        if any(item.status in problems for item in self.items):
            return "PARTIAL"
        return "COMPLETED"


class DailyIngestService:
    def __init__(
        self,
        session: Session,
        store: ArtifactStore,
        *,
        adapter: ElPeruanoSourceAdapter | None = None,
        ingest_service: IngestService | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._adapter = adapter or ElPeruanoSourceAdapter()
        # El mismo adaptador para descubrir y para capturar: comparte el reloj
        # del rate limit, de modo que una corrida diaria no dispara dos series de
        # peticiones independientes contra el mismo host.
        self._ingest = ingest_service or IngestService(session, store, adapter=self._adapter)

    # ------------------------------------------------------------------
    # Corrida de una fecha
    # ------------------------------------------------------------------

    def run(
        self,
        publication_date: date,
        series: str = "NL",
        *,
        dry_run: bool = False,
        limit: int | None = None,
        include_not_relevant: bool = False,
        capture_pdf: bool = True,
    ) -> DailyRunResult:
        result = DailyRunResult(publication_date=publication_date, series=series, dry_run=dry_run)
        issue_code = f"{series}{publication_date.strftime('%Y%m%d')}"
        result.issue_code = issue_code

        run_row: m.CrawlRun | None = None
        issue_item: m.PublicationItem | None = None
        if not dry_run:
            run_row = m.CrawlRun(
                crawler_version=CRAWLER_VERSION,
                parameters={
                    "publication_date": publication_date.isoformat(),
                    "series": series,
                    "issue_code": issue_code,
                    "limit": limit,
                    "include_not_relevant": include_not_relevant,
                    "capture_pdf": capture_pdf,
                },
            )
            self._session.add(run_row)
            self._session.flush()
            result.crawl_run_id = run_row.id
            _issue, issue_item = ensure_issue(self._session, issue_code)

        try:
            discovered = self._discover(publication_date, series, issue_item, result, dry_run)
        except (
            ListingParseError,
            LiveSourceDisabled,
            CaptureHttpError,
            CaptureNetworkError,
        ) as exc:
            result.error_summary = f"{type(exc).__name__}: {exc}"
            if run_row is not None:
                run_row.status = "FAILED"
                run_row.error_summary = result.error_summary
                run_row.completed_at = datetime.now(UTC)
                self._session.flush()
            return result

        processed = 0
        for entry, listing_version_id in discovered:
            verdict = classify_summary(entry.summary_raw)
            row = (
                None if dry_run else self._record_item(run_row, entry, listing_version_id, verdict)
            )
            code = entry.reference.publication_code

            date_problem = self._listed_date_mismatch(entry, publication_date)
            if date_problem is not None:
                result.items.append(
                    self._settle(
                        row, code, e.CrawlItemStatus.FAILED, verdict.relevance, date_problem
                    )
                )
                continue

            if not verdict.should_ingest and not include_not_relevant:
                result.items.append(
                    self._settle(
                        row,
                        code,
                        e.CrawlItemStatus.SKIPPED_NOT_RELEVANT,
                        verdict.relevance,
                        verdict.rationale,
                    )
                )
                continue

            existing = official_publication_item(self._session, code, entry.reference.source_series)
            if existing is not None and self._has_document(existing.id):
                if row is not None:
                    row.publication_item_id = existing.id
                self._link_issue(existing, issue_item)
                result.items.append(
                    self._settle(
                        row,
                        code,
                        e.CrawlItemStatus.ALREADY_PRESENT,
                        verdict.relevance,
                        "ya estaba ingerido; no se vuelve a extraer (usa reprocess)",
                        # Se cuenta igual que si se hubiera ingerido ahora: la
                        # bitácora del día debe poder responder "¿de qué actos
                        # de personal no salió nada?" sin depender de en qué
                        # corrida entró cada uno.
                        self._event_count(existing.id),
                    )
                )
                continue

            if limit is not None and processed >= limit:
                result.items.append(
                    self._settle(
                        row,
                        code,
                        e.CrawlItemStatus.DISCOVERED,
                        verdict.relevance,
                        f"no procesado: se alcanzó el límite de {limit} por corrida",
                    )
                )
                continue

            if dry_run:
                result.items.append(
                    ItemResult(
                        code, e.CrawlItemStatus.DISCOVERED, verdict.relevance, "se ingeriría"
                    )
                )
                processed += 1
                continue

            processed += 1
            result.items.append(self._ingest_entry(row, entry, issue_item, capture_pdf=capture_pdf))

        if run_row is not None:
            run_row.status = result.status
            run_row.completed_at = datetime.now(UTC)
            self._session.flush()
        return result

    # ------------------------------------------------------------------
    # Reintentos
    # ------------------------------------------------------------------

    def retry_pending(
        self, *, limit: int | None = None, capture_pdf: bool = True, include_failed: bool = False
    ) -> list[ItemResult]:
        """Re-intenta lo que quedó transitorio, en un paso aparte y explícito.

        Deliberadamente no vive dentro de la corrida diaria: un fallo transitorio
        se reintenta cuando alguien lo decide, no en el acto contra un servidor
        que acaba de responder mal.

        Con `include_failed` reintenta también lo FAILED: es la vía de
        recuperación cuando el fallo era del parser y el parser ya se corrigió.
        Antes de re-pedir nada se reevalúa la relevancia con las reglas
        vigentes: entre el fallo y el reintento las reglas pueden haber
        cambiado (p. ej. las apelaciones electorales del JNE), y reintentar lo
        que hoy se descartaría gastaría peticiones en ingerir lo que no toca.
        """
        statuses = [e.CrawlItemStatus.RETRY_PENDING, e.CrawlItemStatus.INGESTED_PDF_PENDING]
        if include_failed:
            statuses.append(e.CrawlItemStatus.FAILED)
        rows = (
            self._session.execute(
                select(m.CrawlItem)
                .where(m.CrawlItem.status.in_(statuses))
                .order_by(m.CrawlItem.discovered_at)
            )
            .scalars()
            .all()
        )
        results: list[ItemResult] = []
        for row in rows[:limit] if limit is not None else rows:
            entry = ListingEntry(
                reference=self._adapter.parse_source_reference(
                    row.canonical_url or row.publication_code
                ),
                summary_raw=row.summary_raw,
            )
            issue_item = self._issue_item_of(row)
            if row.status == e.CrawlItemStatus.INGESTED_PDF_PENDING:
                results.append(self._retry_pdf_only(row))
                continue
            if row.status == e.CrawlItemStatus.FAILED:
                verdict = classify_summary(row.summary_raw)
                if not verdict.should_ingest:
                    # La decisión de hoy queda escrita con su regla: el veredicto
                    # original vive en el índice archivado, no en esta fila.
                    row.relevance = verdict.relevance
                    row.relevance_rule = verdict.rule
                    row.relevance_rationale = verdict.rationale
                    results.append(
                        self._settle(
                            row,
                            row.publication_code,
                            e.CrawlItemStatus.SKIPPED_NOT_RELEVANT,
                            verdict.relevance,
                            f"reevaluado al reintentar: {verdict.rationale}",
                        )
                    )
                    continue
            results.append(self._ingest_entry(row, entry, issue_item, capture_pdf=capture_pdf))
        return results

    # ------------------------------------------------------------------
    # Descubrimiento
    # ------------------------------------------------------------------

    def _discover(
        self,
        publication_date: date,
        series: str,
        issue_item: m.PublicationItem | None,
        result: DailyRunResult,
        dry_run: bool,
    ) -> list[tuple[ListingEntry, str | None]]:
        discovered: dict[str, tuple[ListingEntry, str | None]] = {}
        for url, content, capture, page in self._adapter.iter_listing_pages(
            publication_date, series
        ):
            result.listing_pages += 1
            result.total_declared = page.total_declared
            version_id: str | None = None
            if not dry_run and issue_item is not None:
                version_id = self._store_listing_page(
                    issue_item, url, content, capture, page, publication_date, series
                )
            for entry in page.entries:
                discovered.setdefault(entry.reference.publication_code, (entry, version_id))
        if result.total_declared and len(discovered) != result.total_declared:
            raise ListingParseError(
                f"El índice del {publication_date.isoformat()} declara "
                f"{result.total_declared} dispositivos y se recolectaron {len(discovered)}"
            )
        return list(discovered.values())

    def _store_listing_page(
        self,
        issue_item: m.PublicationItem,
        url: str,
        content: bytes,
        capture,  # noqa: ANN001 - CaptureRecord
        page,  # noqa: ANN001 - ListingPage
        publication_date: date,
        series: str,
    ) -> str | None:
        """Archiva la página del índice, salvo que ya haya una que diga lo mismo.

        La deduplicación normal del CAS —bytes idénticos, misma versión— no sirve
        aquí: el sitio inyecta en cada respuesta un script anti-bot con token
        distinto y una cookie con marca de tiempo al milisegundo, así que dos
        capturas del mismo índice **nunca** coinciden byte a byte (comprobado el
        2026-08-07: dos capturas de 136 925 caracteres que solo difieren en el
        token y en `x-bni-rncf`). Sin esta comprobación, cada corrida de una
        fecha ya recolectada abriría versiones nuevas para siempre.

        Lo que decide es lo que el índice declara: total y dispositivos. Si eso
        no cambió, la evidencia ya está archivada y se reutiliza. Si cambió —una
        norma añadida más tarde, una fe de erratas— se abre versión, que es
        justamente el hecho que interesa registrar.
        """
        previous = (
            self._session.execute(
                select(m.ArtifactVersion)
                .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
                .where(
                    m.Artifact.publication_item_id == issue_item.id,
                    m.Artifact.representation_type == e.RepresentationType.LISTING,
                    m.ArtifactVersion.requested_url == url,
                )
                .order_by(m.ArtifactVersion.captured_at.desc())
            )
            .scalars()
            .first()
        )
        if previous is not None and self._declares_the_same(
            previous, page, publication_date, series
        ):
            return previous.id
        return store_capture(
            self._session,
            self._store,
            item=issue_item,
            representation=e.RepresentationType.LISTING,
            content=content,
            capture=capture,
        ).version_id

    def _declares_the_same(
        self,
        version: m.ArtifactVersion,
        page,  # noqa: ANN001 - ListingPage
        publication_date: date,
        series: str,
    ) -> bool:
        """¿La versión archivada declara los mismos dispositivos que esta página?

        Ante la duda —bytes ausentes del CAS, sha256 que no cuadra, versión
        vieja que ya no parsea— responde que NO y se archiva otra vez: repetir
        una captura cuesta disco, darla por equivalente sin comprobarlo pierde
        evidencia.
        """
        content = verified_bytes(self._store, version)
        if content is None:
            return False
        try:
            stored = parse_listing(
                content,
                requested_date=publication_date,
                series=series,
                base_url=BASE_URL,
                source_family=self._adapter.source_family,
            )
        except ListingParseError:
            return False
        return stored.total_declared == page.total_declared and [
            (entry.reference.publication_code, entry.summary_raw) for entry in stored.entries
        ] == [(entry.reference.publication_code, entry.summary_raw) for entry in page.entries]

    # ------------------------------------------------------------------
    # Procesamiento de un dispositivo
    # ------------------------------------------------------------------

    def _ingest_entry(
        self,
        row: m.CrawlItem | None,
        entry: ListingEntry,
        issue_item: m.PublicationItem | None,
        *,
        capture_pdf: bool,
    ) -> ItemResult:
        code = entry.reference.publication_code
        relevance = row.relevance if row is not None else e.Relevance.UNDECIDED
        if row is not None:
            row.attempts += 1
        url = entry.reference.canonical_url
        if not url:
            return self._settle(
                row, code, e.CrawlItemStatus.FAILED, relevance, "el índice no declara URL canónica"
            )
        try:
            # Punto de guardado por dispositivo: un error de base de datos
            # aborta la transacción entera en PostgreSQL, y sin esto un solo
            # documento defectuoso tiraría la corrida completa —incluidas las
            # decenas de peticiones ya hechas a la fuente— sin dejar registro.
            with self._session.begin_nested():
                outcome = self._ingest.ingest_url(url)
        except Exception as exc:  # clasificado abajo; nada se traga en silencio
            status, detail = self._classify(exc)
            return self._settle(row, code, status, relevance, detail)

        item = self._session.get(m.PublicationItem, outcome.publication_item_id)
        if row is not None and item is not None:
            row.publication_item_id = item.id
        if item is not None:
            self._link_issue(item, issue_item)

        events = len(outcome.event_ids)
        detail = (
            f"eventos={events} asignaciones={len(outcome.assignment_ids)} "
            f"tareas={len(outcome.review_task_ids)}"
        )
        if not capture_pdf:
            return self._settle(row, code, e.CrawlItemStatus.INGESTED, relevance, detail, events)
        if item is None or not item.pdf_url:
            return self._settle(
                row,
                code,
                e.CrawlItemStatus.INGESTED,
                relevance,
                f"{detail}; la captura no declara PDF, no hay archivo que respaldar",
                events,
            )
        try:
            with self._session.begin_nested():
                pdf = capture_device_pdf(self._session, self._store, code)
        except Exception as exc:
            status, pdf_detail = self._classify(exc)
            # El texto ya está ingerido y extraído: lo único pendiente es el
            # respaldo del archivo, así que el estado lo dice y el reintento
            # sabrá que no debe volver a extraer nada.
            return self._settle(
                row,
                code,
                e.CrawlItemStatus.INGESTED_PDF_PENDING
                if status is e.CrawlItemStatus.RETRY_PENDING
                else e.CrawlItemStatus.INGESTED,
                relevance,
                f"{detail}; PDF no respaldado: {pdf_detail}",
                events,
            )
        return self._settle(
            row,
            code,
            e.CrawlItemStatus.INGESTED,
            relevance,
            f"{detail}; PDF {pdf.sha256[:12]} ({pdf.byte_length} bytes)",
            events,
        )

    def _retry_pdf_only(self, row: m.CrawlItem) -> ItemResult:
        row.attempts += 1
        try:
            with self._session.begin_nested():
                pdf = capture_device_pdf(self._session, self._store, row.publication_code)
        except Exception as exc:
            status, detail = self._classify(exc)
            return self._settle(
                row,
                row.publication_code,
                e.CrawlItemStatus.INGESTED_PDF_PENDING
                if status is e.CrawlItemStatus.RETRY_PENDING
                else e.CrawlItemStatus.INGESTED,
                row.relevance,
                f"PDF sigue sin respaldar: {detail}",
                row.events_extracted,
            )
        return self._settle(
            row,
            row.publication_code,
            e.CrawlItemStatus.INGESTED,
            row.relevance,
            f"PDF respaldado {pdf.sha256[:12]} ({pdf.byte_length} bytes)",
            row.events_extracted,
        )

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _classify(self, exc: Exception) -> tuple[e.CrawlItemStatus, str]:
        """Distingue lo que se arregla reintentando de lo que no.

        Un fallo de parseo o una captura mal etiquetada no mejoran por insistir:
        son FAILED y piden que alguien mire. Lo demás —red, estados HTTP
        transitorios— queda pendiente de reintento explícito.
        """
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        if isinstance(exc, LiveSourceDisabled):
            raise exc  # no es un fallo del dispositivo: la corrida entera no procede
        if isinstance(exc, CaptureHttpError):
            if exc.status_code in _TRANSIENT_STATUS:
                return e.CrawlItemStatus.RETRY_PENDING, detail
            return e.CrawlItemStatus.FAILED, detail
        if isinstance(exc, CaptureNetworkError):
            return e.CrawlItemStatus.RETRY_PENDING, detail
        # La base puede fallar por indisponibilidad (reintentable) o porque el
        # dato no cabe / viola una restricción, que no mejora por insistir.
        if isinstance(exc, OperationalError | InterfaceError):
            return e.CrawlItemStatus.RETRY_PENDING, detail
        if isinstance(exc, SQLAlchemyError):
            return e.CrawlItemStatus.FAILED, detail
        if isinstance(exc, ParseError | ListingParseError | CaptureError | ValueError):
            return e.CrawlItemStatus.FAILED, detail
        return e.CrawlItemStatus.RETRY_PENDING, detail

    def _settle(
        self,
        row: m.CrawlItem | None,
        code: str,
        status: e.CrawlItemStatus,
        relevance: e.Relevance,
        detail: str,
        events: int | None = None,
    ) -> ItemResult:
        """Cierra el estado de un dispositivo y devuelve lo que se dirá de él.

        `code` va explícito porque en dry-run no hay fila donde leerlo, y un
        resultado sin código no le sirve a nadie que esté mirando la corrida.
        """
        if row is not None:
            row.status = status
            row.updated_at = datetime.now(UTC)
            failed = status in (e.CrawlItemStatus.FAILED, e.CrawlItemStatus.RETRY_PENDING)
            row.last_error = detail if failed or "no respaldado" in detail else None
            row.outcome_detail = detail
            row.events_extracted = events
            self._session.flush()
        return ItemResult(code, status, relevance, detail, events)

    def _record_item(
        self,
        run_row: m.CrawlRun | None,
        entry: ListingEntry,
        listing_version_id: str | None,
        verdict,  # noqa: ANN001 - RelevanceVerdict
    ) -> m.CrawlItem:
        assert run_row is not None
        row = m.CrawlItem(
            crawl_run_id=run_row.id,
            source_series=entry.reference.source_series,
            publication_code=entry.reference.publication_code,
            canonical_url=entry.reference.canonical_url,
            listing_artifact_version_id=listing_version_id,
            issuer_raw=entry.issuer_raw,
            document_type_raw=entry.document_type_raw,
            number_raw=entry.number_raw,
            summary_raw=entry.summary_raw,
            listed_date_raw=entry.listed_date_raw,
            relevance=verdict.relevance,
            relevance_rule=verdict.rule,
            relevance_rationale=verdict.rationale,
            status=e.CrawlItemStatus.DISCOVERED,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _listed_date_mismatch(self, entry: ListingEntry, expected: date) -> str | None:
        """La tarjeta repite la fecha; si no es la pedida, algo no cuadra."""
        if not entry.listed_date_raw:
            return None
        match = _LISTED_DATE_RE.search(entry.listed_date_raw)
        if match is None:
            return None
        listed = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if listed == expected:
            return None
        return (
            f"el índice del {expected.isoformat()} lista este dispositivo con fecha "
            f"{listed.isoformat()}: no se ingiere bajo una edición que no es la suya"
        )

    def _event_count(self, publication_item_id: str) -> int:
        return self._session.execute(
            select(func.count(m.PersonnelEvent.id))
            .join(m.LegalDocument, m.LegalDocument.id == m.PersonnelEvent.legal_document_id)
            .where(m.LegalDocument.publication_item_id == publication_item_id)
        ).scalar_one()

    def _has_document(self, publication_item_id: str) -> bool:
        return (
            self._session.execute(
                select(m.LegalDocument.id).where(
                    m.LegalDocument.publication_item_id == publication_item_id
                )
            ).first()
            is not None
        )

    def _link_issue(self, item: m.PublicationItem, issue_item: m.PublicationItem | None) -> None:
        """Ata la publicación a la edición del día en que el índice la listó."""
        if issue_item is None or issue_item.issue_id is None:
            return
        if item.issue_id is None:
            item.issue_id = issue_item.issue_id
            self._session.flush()

    def _issue_item_of(self, row: m.CrawlItem) -> m.PublicationItem | None:
        run = self._session.get(m.CrawlRun, row.crawl_run_id)
        issue_code = (run.parameters or {}).get("issue_code") if run is not None else None
        if not issue_code:
            return None
        return (
            self._session.execute(
                select(m.PublicationItem).where(m.PublicationItem.publication_code == issue_code)
            )
            .scalars()
            .first()
        )


@dataclass(frozen=True)
class BackfillResult:
    publication_code: str
    events_extracted: int
    changed: bool


def backfill_event_counts(session: Session, *, dry_run: bool = False) -> list[BackfillResult]:
    """Rellena `crawl_item.events_extracted` en las filas anteriores a la columna.

    Determinista y sin red: cuenta los `personnel_event` que ya están
    persistidos para el documento de esa publicación. No inventa nada ni toca
    ninguna afirmación; solo pone en la bitácora una cuenta que ya se podía
    derivar de los datos, para que "relevante y sin eventos" sea consultable.

    `outcome_detail` se deja como estaba: de las corridas viejas no consta qué
    se dijo entonces, y escribirlo ahora sería inventar el registro.
    """
    rows = (
        session.execute(
            select(m.CrawlItem)
            .where(
                m.CrawlItem.events_extracted.is_(None),
                m.CrawlItem.publication_item_id.is_not(None),
            )
            .order_by(m.CrawlItem.publication_code)
        )
        .scalars()
        .all()
    )
    results: list[BackfillResult] = []
    for row in rows:
        count = session.execute(
            select(func.count(m.PersonnelEvent.id))
            .join(
                m.LegalDocument,
                m.LegalDocument.id == m.PersonnelEvent.legal_document_id,
            )
            .where(m.LegalDocument.publication_item_id == row.publication_item_id)
        ).scalar_one()
        if not dry_run:
            row.events_extracted = count
            row.updated_at = datetime.now(UTC)
        results.append(BackfillResult(row.publication_code, count, True))
    if not dry_run:
        session.flush()
    return results


@dataclass(frozen=True)
class IssueLinkResult:
    publication_code: str
    issue_code: str | None
    detail: str


def backfill_issue_links(session: Session, *, dry_run: bool = False) -> list[IssueLinkResult]:
    """Ata a su edición las publicaciones ingeridas antes del recolector diario.

    Determinista y sin red. La edición no se deduce de cuándo se capturó el
    documento sino de la fecha de publicación que la propia captura declara
    (`legal_document.published_on`, la misma frase que sostiene la fecha de
    inicio de efectos): si coincide con la fecha de una edición ya registrada
    del diario oficial y la serie es la suya, se enlaza; si no, se deja como
    está y se dice por qué.

    Nunca crea ediciones: enlazar con una edición inventada afirmaría que ese
    día hubo un cuadernillo que nadie ha capturado.
    """
    gazette = (
        session.execute(
            select(m.SourceSystem).where(
                m.SourceSystem.authority == e.SourceAuthority.OFFICIAL_GAZETTE
            )
        )
        .scalars()
        .first()
    )
    if gazette is None:
        return []
    issues_by_date = {
        (issue.family, issue.publication_date): issue
        for issue in session.execute(
            select(m.PublicationIssue).where(m.PublicationIssue.source_system_id == gazette.id)
        ).scalars()
        if issue.publication_date is not None
    }
    rows = (
        session.execute(
            select(m.PublicationItem)
            .where(
                m.PublicationItem.issue_id.is_(None),
                m.PublicationItem.source_system_id == gazette.id,
            )
            .order_by(m.PublicationItem.publication_code)
        )
        .scalars()
        .all()
    )
    results: list[IssueLinkResult] = []
    for item in rows:
        doc = (
            session.execute(
                select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
            )
            .scalars()
            .first()
        )
        if doc is None or doc.published_on is None:
            results.append(
                IssueLinkResult(
                    item.publication_code,
                    None,
                    "la captura no declara fecha de publicación; no se deduce la edición",
                )
            )
            continue
        issue = issues_by_date.get((item.source_series, doc.published_on))
        if issue is None:
            results.append(
                IssueLinkResult(
                    item.publication_code,
                    None,
                    f"no hay edición registrada de {item.source_series} para "
                    f"{doc.published_on.isoformat()}; captúrala antes (capture-issue)",
                )
            )
            continue
        if not dry_run:
            item.issue_id = issue.id
        results.append(
            IssueLinkResult(
                item.publication_code,
                issue.issue_code,
                f"fecha de publicación declarada por la captura: {doc.published_on.isoformat()}",
            )
        )
    if not dry_run:
        session.flush()
    return results


def pending_items(session: Session) -> Sequence[m.CrawlItem]:
    return (
        session.execute(
            select(m.CrawlItem)
            .where(
                m.CrawlItem.status.in_(
                    [e.CrawlItemStatus.RETRY_PENDING, e.CrawlItemStatus.INGESTED_PDF_PENDING]
                )
            )
            .order_by(m.CrawlItem.discovered_at)
        )
        .scalars()
        .all()
    )
