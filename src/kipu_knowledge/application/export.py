"""Casos de uso de exportación y validación semántica."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from kipu_knowledge.adapters.rdf.projection import RdfProjection
from kipu_knowledge.adapters.rdf.validation import ShaclReport, validate_graph


def _projection(session: Session) -> RdfProjection:
    context = None
    for base in (Path(__file__).resolve().parents[3], Path.cwd()):
        candidate = base / "ontology" / "context.jsonld"
        if candidate.exists():
            context = candidate
            break
    return RdfProjection(session, context_path=context)


def export_document_rdf(
    session: Session, publication_code: str, destination: Path | None = None
) -> str:
    return _projection(session).export_rdf(publication_code, destination)


def export_document_jsonld(
    session: Session, publication_code: str, destination: Path | None = None
) -> str:
    return _projection(session).export_jsonld(publication_code, destination)


def validate_publication(session: Session, publication_code: str) -> ShaclReport:
    projection = _projection(session)
    dataset = projection.rebuild(publication_code)
    merged = projection._merge(dataset)
    return validate_graph(merged)


def rebuild_projections(session: Session, out_dir: Path) -> list[Path]:
    """Regenera exportaciones TTL/JSON-LD de todas las publicaciones ingeridas."""
    projection = _projection(session)
    written: list[Path] = []
    for code in projection._all_codes():
        ttl = out_dir / f"{code}.ttl"
        jsonld = out_dir / f"{code}.jsonld"
        projection.export_rdf(code, ttl)
        projection.export_jsonld(code, jsonld)
        written.extend([ttl, jsonld])
    trig = out_dir / "dataset.trig"
    trig.parent.mkdir(parents=True, exist_ok=True)
    trig.write_text(projection.export_trig(), encoding="utf-8")
    written.append(trig)
    return written
