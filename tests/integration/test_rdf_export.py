"""Integración RDF: proyección, named graphs y validación SHACL sin red."""

from __future__ import annotations

import pytest
from rdflib import URIRef

from kipu_knowledge.adapters.rdf.projection import (
    GRAPH_ACCEPTED,
    GRAPH_CANDIDATE,
    GRAPH_EXTRACTION,
    GRAPH_SOURCE,
    RdfProjection,
)
from kipu_knowledge.application.export import validate_publication

ALL_CODES = [
    "2540861-1",
    "2540903-1",
    "2540903-2",
    "2540905-3",
    "2540905-4",
    "2540779-1",
    "2540702-1",
    "2540905-2",
    "2540896-1",
]


@pytest.mark.parametrize("code", ALL_CODES)
def test_shacl_conforms(ingested_session, code):
    report = validate_publication(ingested_session, code)
    assert report.conforms, report.report_text


def test_named_graphs_present(ingested_session):
    dataset = RdfProjection(ingested_session).rebuild("2540861-1")
    identifiers = {g.identifier for g in dataset.graphs() if len(g)}
    assert GRAPH_SOURCE in identifiers
    assert GRAPH_EXTRACTION in identifiers
    assert GRAPH_ACCEPTED in identifiers
    assert URIRef("urn:kipu:graph:ontology") in identifiers
    # 2540905-2 genera candidatas (grafía compatible de un nombre ya registrado);
    # 2540861-1 puede no tenerlas.
    dataset2 = RdfProjection(ingested_session).rebuild("2540905-2")
    ids2 = {g.identifier for g in dataset2.graphs() if len(g)}
    assert GRAPH_CANDIDATE in ids2


def test_trig_export_contains_graphs(ingested_session):
    trig = RdfProjection(ingested_session).export_trig("2540861-1")
    assert "urn:kipu:graph:source" in trig
    assert "urn:kipu:graph:accepted" in trig


def test_jsonld_uses_context(ingested_session):
    from kipu_knowledge.application.export import export_document_jsonld

    text = export_document_jsonld(ingested_session, "2540779-1")
    assert "CAP_PROVISIONAL" in text
    assert "007" in text
