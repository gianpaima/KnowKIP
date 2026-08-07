"""Validación SHACL de la proyección RDF (pySHACL)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph


@dataclass(frozen=True)
class ShaclReport:
    conforms: bool
    report_text: str


def default_shapes_dir() -> Path:
    for base in (Path(__file__).resolve().parents[4], Path.cwd()):
        candidate = base / "ontology" / "shapes"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No se encontró ontology/shapes")


def load_shapes(shapes_dir: Path | None = None) -> Graph:
    shapes_dir = shapes_dir or default_shapes_dir()
    graph = Graph()
    for path in sorted(shapes_dir.glob("*.ttl")):
        graph.parse(path, format="turtle")
    return graph


def validate_graph(data_graph: Graph, shapes_dir: Path | None = None) -> ShaclReport:
    from pyshacl import validate

    shapes = load_shapes(shapes_dir)
    conforms, _report_graph, report_text = validate(
        data_graph,
        shacl_graph=shapes,
        advanced=True,
        allow_infos=True,
        allow_warnings=False,
    )
    return ShaclReport(conforms=bool(conforms), report_text=str(report_text))
