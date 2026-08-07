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
def discover(for_date: str = typer.Option(..., "--date", help="YYYY-MM-DD")) -> None:
    """Descubrimiento por fecha (interfaz preparada; sin implementación live en el MVP)."""
    from kipu_knowledge.adapters.sources.elperuano import ElPeruanoSourceAdapter

    refs = list(ElPeruanoSourceAdapter().discover(date.fromisoformat(for_date)))
    if not refs:
        typer.echo(
            "El descubrimiento por fecha aún no está habilitado (ver docs/source-policy.md); "
            "usa ingest-device o ingest-fixture."
        )
    for ref in refs:
        typer.echo(f"{ref.publication_code} {ref.canonical_url}")


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
