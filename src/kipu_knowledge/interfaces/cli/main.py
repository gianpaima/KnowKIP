"""CLI `kipu` (Typer). Comandos portables Windows/POSIX."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from kipu_knowledge.adapters.db.session import session_scope
from kipu_knowledge.adapters.storage.minio_store import build_store_from_settings

app = typer.Typer(
    name="kipu",
    help="Kipu Knowledge: captura, extracción y consulta verificable de normas publicadas.",
    no_args_is_help=True,
)


def _fixtures_dir() -> Path:
    for base in (Path.cwd(), Path(__file__).resolve().parents[4]):
        candidate = base / "fixtures"
        if candidate.exists():
            return candidate
    raise typer.BadParameter("No se encontró el directorio fixtures/")


def _print_outcome(outcome) -> None:  # noqa: ANN001
    typer.echo(
        f"publicación={outcome.publication_item_id} documento={outcome.legal_document_id} "
        f"nuevo={outcome.created}"
    )
    typer.echo(
        f"eventos={len(outcome.event_ids)} asignaciones={len(outcome.assignment_ids)} "
        f"tareas_revisión={len(outcome.review_task_ids)}"
    )
    for warning in outcome.warnings:
        typer.secho(f"aviso: {warning}", fg=typer.colors.YELLOW)


def _print_item(item) -> None:  # noqa: ANN001
    from kipu_knowledge.domain.enums import CrawlItemStatus

    color = {
        CrawlItemStatus.INGESTED: typer.colors.GREEN,
        CrawlItemStatus.ALREADY_PRESENT: typer.colors.BLUE,
        CrawlItemStatus.SKIPPED_NOT_RELEVANT: typer.colors.WHITE,
        CrawlItemStatus.DISCOVERED: typer.colors.CYAN,
        CrawlItemStatus.INGESTED_PDF_PENDING: typer.colors.YELLOW,
        CrawlItemStatus.RETRY_PENDING: typer.colors.YELLOW,
        CrawlItemStatus.FAILED: typer.colors.RED,
    }.get(item.status, typer.colors.WHITE)
    typer.secho(f"[{item.status}] {item.publication_code} — {item.detail}", fg=color)


def _print_daily(result) -> None:  # noqa: ANN001
    typer.echo(
        f"corrida={result.crawl_run_id or '(dry-run)'} fecha={result.publication_date} "
        f"edición={result.issue_code} páginas_índice={result.listing_pages} "
        f"declarados={result.total_declared}"
    )
    for item in result.items:
        _print_item(item)
    tally = " ".join(f"{k}={v}" for k, v in sorted(result.counts().items()))
    typer.echo(f"estado={result.status} {tally}")
    empty = result.relevant_but_empty
    if empty:
        # Ni error ni éxito: el documento está capturado y su texto íntegro, pero
        # el extractor no reconoció el acto. Es un hueco de cobertura y se dice.
        typer.secho(
            f"{len(empty)} dispositivo(s) que el filtro llamó de personal no produjeron "
            f"ningún evento (hueco del extractor, no fallo de captura): "
            + ", ".join(item.publication_code for item in empty),
            fg=typer.colors.YELLOW,
        )
    if result.error_summary:
        typer.secho(f"corrida fallida: {result.error_summary}", fg=typer.colors.RED)


@app.command("ingest-device")
def ingest_device(url: str) -> None:
    """Ingiere un dispositivo desde su URL (requiere LIVE_SOURCE_ENABLED=true)."""
    from kipu_knowledge.application.ingest import IngestService

    with session_scope() as session:
        service = IngestService(session, build_store_from_settings())
        _print_outcome(service.ingest_url(url))


@app.command("ingest-file")
def ingest_file(
    path: Path,
    publication_code: str = typer.Option(None, help="Código si el nombre del archivo no lo es"),
) -> None:
    """Ingiere un HTML local previamente capturado."""
    from kipu_knowledge.application.ingest import IngestService

    with session_scope() as session:
        service = IngestService(session, build_store_from_settings())
        _print_outcome(service.ingest_file(path, publication_code))


@app.command("ingest-fixture")
def ingest_fixture(fixture_name: str) -> None:
    """Ingiere un fixture del repositorio (p.ej. 2540861-1)."""
    from kipu_knowledge.application.ingest import IngestService

    with session_scope() as session:
        service = IngestService(session, build_store_from_settings())
        _print_outcome(service.ingest_fixture(fixture_name, _fixtures_dir()))


@app.command("reprocess")
def reprocess(publication_code: str = typer.Option(..., "--publication-code")) -> None:
    """Re-extrae una publicación ya capturada (supersede las afirmaciones previas)."""
    from kipu_knowledge.application.ingest import IngestService

    with session_scope() as session:
        service = IngestService(session, build_store_from_settings())
        _print_outcome(service.reprocess(publication_code))


@app.command("validate")
def validate(publication_code: str = typer.Option(..., "--publication-code")) -> None:
    """Valida la proyección RDF de una publicación con SHACL."""
    from kipu_knowledge.application.export import validate_publication

    with session_scope() as session:
        report = validate_publication(session, publication_code)
    if report.conforms:
        typer.secho(f"SHACL OK: {publication_code} conforma", fg=typer.colors.GREEN)
    else:
        typer.secho(f"SHACL FALLA: {publication_code}", fg=typer.colors.RED)
        typer.echo(report.report_text)
        raise typer.Exit(code=1)


@app.command("export-rdf")
def export_rdf(
    publication_code: str = typer.Option(..., "--publication-code"),
    out: Path = typer.Option(Path("var/exports"), help="Directorio de salida"),
) -> None:
    """Exporta TTL y JSON-LD de una publicación."""
    from kipu_knowledge.application.export import export_document_jsonld, export_document_rdf

    with session_scope() as session:
        ttl_path = out / f"{publication_code}.ttl"
        jsonld_path = out / f"{publication_code}.jsonld"
        export_document_rdf(session, publication_code, ttl_path)
        export_document_jsonld(session, publication_code, jsonld_path)
    typer.echo(f"escrito: {ttl_path}")
    typer.echo(f"escrito: {jsonld_path}")


@app.command("rebuild-projections")
def rebuild_projections(
    out: Path = typer.Option(Path("var/exports"), help="Directorio de salida"),
) -> None:
    """Regenera todas las exportaciones RDF/JSON-LD y el dataset TriG completo."""
    from kipu_knowledge.application.export import rebuild_projections as _rebuild

    with session_scope() as session:
        written = _rebuild(session, out)
    for path in written:
        typer.echo(f"escrito: {path}")


@app.command("discover")
def discover(
    for_date: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
    series: str = typer.Option("NL", "--series", help="Serie del diario (NL = Normas Legales)"),
) -> None:
    """Lista los dispositivos que la fuente publicó en una fecha (no ingiere nada)."""
    from kipu_knowledge.adapters.sources.elperuano import ElPeruanoSourceAdapter
    from kipu_knowledge.domain.enums import Relevance
    from kipu_knowledge.domain.relevance import classify_summary

    entries = ElPeruanoSourceAdapter().discover_entries(date.fromisoformat(for_date), series)
    for entry in entries:
        verdict = classify_summary(entry.summary_raw)
        color = {
            Relevance.RELEVANT: typer.colors.GREEN,
            Relevance.UNDECIDED: typer.colors.YELLOW,
            Relevance.NOT_RELEVANT: typer.colors.WHITE,
        }[verdict.relevance]
        typer.secho(
            f"[{verdict.relevance}] {entry.reference.publication_code} "
            f"{entry.issuer_raw or '?'} — {entry.summary_raw or '(sin sumilla)'}",
            fg=color,
        )
    typer.echo(f"total: {len(entries)} dispositivos en {for_date} ({series})")


@app.command("ingest-date")
def ingest_date(
    for_date: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
    series: str = typer.Option("NL", "--series"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Descubre y clasifica sin escribir nada"),
    limit: int = typer.Option(None, "--limit", help="Tope de dispositivos a ingerir en la corrida"),
    include_not_relevant: bool = typer.Option(
        False, "--include-not-relevant", help="Ingiere también lo que el filtro descarta"
    ),
    no_pdf: bool = typer.Option(False, "--no-pdf", help="No respalda el PDF de cada dispositivo"),
) -> None:
    """Descubre el índice de una fecha e ingiere los actos de personal que trae."""
    from kipu_knowledge.application.daily_ingest import DailyIngestService

    with session_scope() as session:
        service = DailyIngestService(session, build_store_from_settings())
        result = service.run(
            date.fromisoformat(for_date),
            series,
            dry_run=dry_run,
            limit=limit,
            include_not_relevant=include_not_relevant,
            capture_pdf=not no_pdf,
        )
    _print_daily(result)
    if result.error_summary:
        raise typer.Exit(code=1)


@app.command("retry-pending")
def retry_pending(
    limit: int = typer.Option(None, "--limit"),
    no_pdf: bool = typer.Option(False, "--no-pdf"),
) -> None:
    """Reintenta los dispositivos que quedaron con un fallo transitorio.

    Va aparte de la corrida diaria a propósito: un 404 pasajero se reintenta
    cuando alguien lo decide, no en el acto contra el mismo servidor.
    """
    from kipu_knowledge.application.daily_ingest import DailyIngestService

    with session_scope() as session:
        service = DailyIngestService(session, build_store_from_settings())
        results = service.retry_pending(limit=limit, capture_pdf=not no_pdf)
    if not results:
        typer.echo("No hay dispositivos pendientes de reintento.")
        return
    for item in results:
        _print_item(item)


@app.command("backfill-crawl-outcomes")
def backfill_crawl_outcomes(
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo evalúa; no modifica nada"),
) -> None:
    """Cuenta los eventos extraídos en las filas de bitácora anteriores a esa columna.

    Determinista y sin red: la cuenta sale de los `personnel_event` ya
    persistidos. Sirve para poder consultar qué dispositivos relevantes no
    produjeron ningún evento.
    """
    from kipu_knowledge.application.daily_ingest import backfill_event_counts

    with session_scope() as session:
        results = backfill_event_counts(session, dry_run=dry_run)
    if not results:
        typer.echo("No hay filas de bitácora sin cuenta de eventos.")
        return
    for row in results:
        color = typer.colors.YELLOW if row.events_extracted == 0 else typer.colors.GREEN
        typer.secho(f"{row.publication_code}: eventos={row.events_extracted}", fg=color)
    typer.echo(f"actualizadas: {len(results)}" + (" (dry-run)" if dry_run else ""))


@app.command("backfill-issue-links")
def backfill_issue_links_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo evalúa; no modifica nada"),
) -> None:
    """Ata a su edición las publicaciones ingeridas antes del recolector diario.

    Usa la fecha de publicación que declara cada captura; no crea ediciones ni
    toca la red.
    """
    from kipu_knowledge.application.daily_ingest import backfill_issue_links

    with session_scope() as session:
        results = backfill_issue_links(session, dry_run=dry_run)
    if not results:
        typer.echo("Todas las publicaciones del diario oficial ya tienen edición.")
        return
    for row in results:
        color = typer.colors.GREEN if row.issue_code else typer.colors.YELLOW
        typer.secho(
            f"{row.publication_code} -> {row.issue_code or 'sin edición'} — {row.detail}", fg=color
        )


@app.command("crawl-report")
def crawl_report(
    for_date: str = typer.Option(None, "--date", help="YYYY-MM-DD; por defecto, la última corrida"),
) -> None:
    """Qué se vio y qué se hizo en una corrida de descubrimiento."""
    from sqlalchemy import select

    from kipu_knowledge.adapters.db import models as m

    with session_scope() as session:
        query = select(m.CrawlRun).order_by(m.CrawlRun.started_at.desc())
        runs = session.execute(query).scalars().all()
        if for_date:
            runs = [r for r in runs if (r.parameters or {}).get("publication_date") == for_date]
        if not runs:
            typer.echo("No hay corridas registradas para ese criterio.")
            return
        run = runs[0]
        params = run.parameters or {}
        typer.echo(
            f"corrida {run.id} fecha={params.get('publication_date')} "
            f"serie={params.get('series')} estado={run.status}"
        )
        rows = (
            session.execute(
                select(m.CrawlItem)
                .where(m.CrawlItem.crawl_run_id == run.id)
                .order_by(m.CrawlItem.publication_code)
            )
            .scalars()
            .all()
        )
        from kipu_knowledge.domain.enums import Relevance

        tally: dict[str, int] = {}
        empty: list[str] = []
        for row in rows:
            tally[str(row.status)] = tally.get(str(row.status), 0) + 1
            typer.echo(
                f"  [{row.status}] [{row.relevance}] {row.publication_code} — "
                f"{(row.summary_raw or '')[:80]}"
            )
            if row.outcome_detail:
                typer.echo(f"      {row.outcome_detail}")
            if row.last_error:
                typer.secho(f"      error: {row.last_error}", fg=typer.colors.RED)
            if row.relevance is Relevance.RELEVANT and row.events_extracted == 0:
                empty.append(row.publication_code)
        typer.echo(f"total={len(rows)} " + " ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        if empty:
            # `events_extracted` es lo que sacó AQUELLA corrida y se queda como
            # está: la bitácora es un registro histórico, no un estado. Lo que
            # importa hoy es si el documento sigue sin eventos, y eso se cuenta
            # de los datos vivos. Repetir la cifra vieja como si fuera actual
            # haría parecer sin arreglar lo que ya se arregló re-extrayendo.
            still_empty = sorted(
                set(empty)
                & set(
                    session.execute(
                        select(m.PublicationItem.publication_code)
                        .join(
                            m.LegalDocument,
                            m.LegalDocument.publication_item_id == m.PublicationItem.id,
                        )
                        .where(
                            m.PublicationItem.publication_code.in_(empty),
                            ~select(m.PersonnelEvent.id)
                            .where(m.PersonnelEvent.legal_document_id == m.LegalDocument.id)
                            .exists(),
                        )
                    ).scalars()
                )
            )
            typer.secho(
                f"la corrida no extrajo eventos de {len(empty)} relevantes: " + ", ".join(empty),
                fg=typer.colors.YELLOW,
            )
            if still_empty:
                typer.secho(
                    f"y hoy siguen sin ningún evento ({len(still_empty)}): "
                    + ", ".join(still_empty),
                    fg=typer.colors.RED,
                )
            else:
                typer.secho(
                    "todos tienen eventos hoy (re-extraídos con un extractor posterior)",
                    fg=typer.colors.GREEN,
                )


@app.command("resolve-affected")
def resolve_affected(
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo evalúa; no modifica nada"),
) -> None:
    """Re-evalúa tareas LINK_AFFECTED_ASSIGNMENT pendientes con la señal de
    corroboración por recital. Solo resuelve las corroboradas; deja rastro
    completo (assertion supersedida + ReviewDecision del sistema)."""
    from kipu_knowledge.application.corroboration import RecitalOutcome, resolve_pending_affected

    with session_scope() as session:
        results = resolve_pending_affected(session, dry_run=dry_run)
    if not results:
        typer.echo("No hay tareas LINK_AFFECTED_ASSIGNMENT pendientes.")
        return
    for row in results:
        color = (
            typer.colors.GREEN
            if row.outcome == RecitalOutcome.CORROBORATED
            else typer.colors.YELLOW
        )
        typer.secho(
            f"[{row.outcome}] tarea={row.task_id} doc={row.document_number or '?'} — {row.detail}",
            fg=color,
        )


@app.command("backfill-source-links")
def backfill_source_links(
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo evalúa; no modifica nada"),
) -> None:
    """Rellena el PDF declarado de cada publicación releyendo las capturas del CAS.

    No toca la red: la captura es la fuente de verdad, así que el resultado es
    reproducible y no depende de que la fuente siga en pie."""
    from kipu_knowledge.application.source_links import LinkOutcome, backfill_pdf_urls

    store = build_store_from_settings()
    with session_scope() as session:
        results = backfill_pdf_urls(session, store, dry_run=dry_run)
    if not results:
        typer.echo("No hay publicaciones registradas.")
        return
    colors = {
        LinkOutcome.UPDATED: typer.colors.GREEN,
        LinkOutcome.UNCHANGED: typer.colors.WHITE,
        LinkOutcome.NOT_DECLARED: typer.colors.YELLOW,
        LinkOutcome.NO_CAPTURE: typer.colors.RED,
    }
    for row in results:
        typer.secho(
            f"[{row.outcome}] {row.publication_code} — {row.pdf_url or 'sin PDF'} ({row.detail})",
            fg=colors[row.outcome],
        )
    updated = sum(1 for r in results if r.outcome == LinkOutcome.UPDATED)
    typer.echo(f"{updated} de {len(results)} {'a actualizar' if dry_run else 'actualizadas'}")


@app.command("apply-legal-effect-dates")
def apply_legal_effect_dates(
    dry_run: bool = typer.Option(False, "--dry-run", help="Solo evalúa; no modifica nada"),
) -> None:
    """Determina la fecha de inicio de efectos que fija la norma.

    Para los actos que el catálogo cubre (designación, nombramiento y término),
    publicados en el diario oficial y sin cláusula que postergue la vigencia, la
    fecha es el día de la publicación (Ley N.º 27594 art. 6 y concordantes).
    Escribe `legal_effect_from` con su fundamento, cita la frase de la captura
    que declara la fecha —releyendo los bytes del CAS, sin red— y cierra las
    tareas EFFECTIVE_DATE_UNSTATED correspondientes. `effective_from` no se toca:
    sigue diciendo lo que el documento dice."""
    from kipu_knowledge.application.legal_effect import (
        BackfillOutcome,
        backfill_legal_effect_dates,
    )

    store = build_store_from_settings()
    with session_scope() as session:
        results = backfill_legal_effect_dates(session, store, dry_run=dry_run)
    if not results:
        typer.echo("No hay eventos con fecha de inicio sin expresar pendientes de determinar.")
        return
    colors = {
        BackfillOutcome.DETERMINED: typer.colors.GREEN,
        BackfillOutcome.VETOED: typer.colors.YELLOW,
        BackfillOutcome.NOT_APPLICABLE: typer.colors.WHITE,
        BackfillOutcome.NO_EVIDENCE: typer.colors.RED,
    }
    for row in results:
        typer.secho(
            f"[{row.outcome}] doc={row.document_number or '?'} "
            f"evento={row.event_id[:8]} {row.value or '—'} — {row.detail}",
            fg=colors[row.outcome],
        )
    determined = sum(1 for r in results if r.outcome == BackfillOutcome.DETERMINED)
    typer.echo(
        f"{determined} de {len(results)} {'a determinar' if dry_run else 'determinadas'} por norma"
    )


def _echo_capture(outcome) -> None:  # noqa: ANN001
    color = typer.colors.GREEN if outcome.created else typer.colors.WHITE
    typer.secho(
        f"[{outcome.representation}] {outcome.url}\n"
        f"  sha256={outcome.sha256[:16]} {outcome.byte_length} bytes · {outcome.detail}",
        fg=color,
    )


@app.command("capture-pdf")
def capture_pdf(
    publication_code: str = typer.Option(..., "--publication-code"),
) -> None:
    """Descarga el PDF del dispositivo y lo respalda en el CAS.

    Requiere LIVE_SOURCE_ENABLED=true: guardar los bytes es lo único que
    sobrevive a que la fuente reorganice su sitio."""
    from kipu_knowledge.application.capture import capture_device_pdf

    store = build_store_from_settings()
    with session_scope() as session:
        outcome = capture_device_pdf(session, store, publication_code)
    _echo_capture(outcome)


@app.command("recapture")
def recapture(
    publication_code: str = typer.Option(..., "--publication-code"),
) -> None:
    """Re-captura el HTML y abre versión nueva solo si los bytes cambiaron."""
    from kipu_knowledge.application.capture import recapture_html

    store = build_store_from_settings()
    with session_scope() as session:
        outcome = recapture_html(session, store, publication_code)
    _echo_capture(outcome)


@app.command("capture-issue")
def capture_issue_cmd(
    issue_code: str = typer.Option(..., "--issue-code", help="p. ej. NL20260806"),
) -> None:
    """Captura el cuadernillo de una edición y lo registra como PublicationIssue."""
    from kipu_knowledge.application.capture import capture_issue

    store = build_store_from_settings()
    with session_scope() as session:
        issue, outcome = capture_issue(session, store, issue_code)
        typer.echo(f"edición {issue.issue_code} · fecha {issue.publication_date or 'no expresada'}")
    _echo_capture(outcome)


@app.command("link-source")
def link_source(
    publication_code: str = typer.Option(..., "--publication-code", help="código en El Peruano"),
    url: str = typer.Option(..., "--url", help="página del acto en la otra fuente"),
    pdf_url: str = typer.Option("", "--pdf-url", help="PDF en la otra fuente"),
    name: str = typer.Option(..., "--name", help="nombre del sistema fuente"),
    external_code: str = typer.Option(..., "--external-code", help="identificador en esa fuente"),
    authority: str = typer.Option(
        "ISSUING_ENTITY", "--authority", help="OFFICIAL_GAZETTE | ISSUING_ENTITY | MIRROR"
    ),
    matched_by: str = typer.Option(
        ..., "--matched-by", help="en qué te basas para afirmar que es el mismo acto"
    ),
    no_capture: bool = typer.Option(False, "--no-capture", help="solo registrar el enlace"),
) -> None:
    """Vincula otra publicación del mismo acto y la respalda.

    Nunca se marca como autoritativa: esa es la publicación de la que se extrajo
    el documento, y solo puede haber una. Las demás sirven de respaldo y
    contraste, y no producen afirmaciones por sí solas."""
    from urllib.parse import urlsplit

    from kipu_knowledge.application.capture import link_external_source
    from kipu_knowledge.domain import enums as e

    parts = urlsplit(url)
    store = build_store_from_settings()
    with session_scope() as session:
        item, outcomes = link_external_source(
            session,
            store,
            publication_code=publication_code,
            system_name=name,
            system_base_url=f"{parts.scheme}://{parts.netloc}",
            authority=e.SourceAuthority(authority),
            external_code=external_code,
            landing_url=url,
            pdf_url=pdf_url or None,
            matched_by=matched_by,
            capture=not no_capture,
        )
        typer.echo(f"publicación {item.publication_code} vinculada a {publication_code}")
    for outcome in outcomes:
        _echo_capture(outcome)


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Arranca la API + UI de revisión."""
    import uvicorn

    uvicorn.run("kipu_knowledge.interfaces.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
