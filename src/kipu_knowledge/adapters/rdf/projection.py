"""Proyección RDF derivada (KnowledgeProjection).

PostgreSQL es la fuente operativa de verdad; el RDF es una proyección reconstruible.
Los named graphs separan fuente, extracción, candidatas, aceptadas y ontología.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import RDF, XSD, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS
from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.application.source_links import official_publication_item
from kipu_knowledge.domain import enums as e
from kipu_knowledge.ontology_version import ONTOLOGY_VERSION

KIPU = Namespace("https://kipu.example/ontology#")
ELI = Namespace("http://data.europa.eu/eli/ontology#")
PROV = Namespace("http://www.w3.org/ns/prov#")
ORG = Namespace("http://www.w3.org/ns/org#")
DATA = Namespace("https://kipu.example/id/")

GRAPH_SOURCE = URIRef("urn:kipu:graph:source")
GRAPH_EXTRACTION = URIRef("urn:kipu:graph:extraction")
GRAPH_CANDIDATE = URIRef("urn:kipu:graph:candidate")
GRAPH_ACCEPTED = URIRef("urn:kipu:graph:accepted")
GRAPH_ONTOLOGY = URIRef("urn:kipu:graph:ontology")

_ACCEPTED = {e.ReviewStatus.AUTO_ACCEPTED, e.ReviewStatus.HUMAN_ACCEPTED}

# Los subject_type relacionales se mapean al mismo "kind" de URI que usan los
# nodos proyectados, para que las afirmaciones referencien nodos existentes.
_SUBJECT_TYPE_TO_URI_KIND = {
    "legal_document": "document",
    "personnel_event": "event",
    "person_mention": "person-mention",
    "role_assignment": "assignment",
}


def _uri(kind: str, identifier: str) -> URIRef:
    return DATA[f"{kind}/{identifier}"]


class RdfProjection:
    """Implementa el contrato KnowledgeProjection sobre la base relacional."""

    def __init__(self, session: Session, context_path: Path | None = None) -> None:
        self._session = session
        self._context_path = context_path

    # -- API ------------------------------------------------------------

    def rebuild(self, publication_code: str | None = None) -> Dataset:
        dataset = Dataset()
        dataset.bind("kipu", KIPU)
        dataset.bind("eli", ELI)
        dataset.bind("prov", PROV)
        dataset.bind("org", ORG)
        dataset.bind("dcterms", DCTERMS)

        codes = [publication_code] if publication_code else self._all_codes()
        for code in codes:
            self._project_publication(dataset, code)

        ont = dataset.graph(GRAPH_ONTOLOGY)
        ont.add(
            (
                URIRef("https://kipu.example/ontology"),
                KIPU.ontologyVersion,
                Literal(ONTOLOGY_VERSION),
            )
        )
        return dataset

    def export_rdf(self, publication_code: str, destination: Path | None = None) -> str:
        dataset = self.rebuild(publication_code)
        merged = self._merge(dataset)
        text = merged.serialize(format="turtle")
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        return text

    def export_jsonld(self, publication_code: str, destination: Path | None = None) -> str:
        dataset = self.rebuild(publication_code)
        merged = self._merge(dataset)
        kwargs: dict = {"format": "json-ld", "auto_compact": True}
        if self._context_path and self._context_path.exists():
            import json

            context = json.loads(self._context_path.read_text(encoding="utf-8"))
            kwargs["context"] = context["@context"]
        text = merged.serialize(**kwargs)
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        return text

    def export_trig(self, publication_code: str | None = None) -> str:
        """Exportación completa con named graphs (fuente/extracción/candidata/aceptada)."""
        return self.rebuild(publication_code).serialize(format="trig")

    @staticmethod
    def _merge(dataset: Dataset) -> Graph:
        merged = Graph()
        for prefix, ns in dataset.namespaces():
            merged.bind(prefix, ns)
        for graph in dataset.graphs():
            if isinstance(graph.identifier, URIRef) and str(graph.identifier).startswith(
                "urn:kipu:graph:"
            ):
                for triple in graph:
                    merged.add(triple)
        return merged

    # -- proyección -----------------------------------------------------

    def _all_codes(self) -> list[str]:
        rows = self._session.execute(select(m.PublicationItem.publication_code)).scalars().all()
        return list(rows)

    def _project_publication(self, dataset: Dataset, publication_code: str) -> None:
        session = self._session
        item = official_publication_item(session, publication_code)
        if item is None:
            raise LookupError(f"Publicación no ingerida: {publication_code}")
        doc = session.execute(
            select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
        ).scalar_one_or_none()
        if doc is None:
            raise LookupError(f"Sin documento parseado para {publication_code}")

        g_source = dataset.graph(GRAPH_SOURCE)
        g_extraction = dataset.graph(GRAPH_EXTRACTION)
        g_candidate = dataset.graph(GRAPH_CANDIDATE)
        g_accepted = dataset.graph(GRAPH_ACCEPTED)

        doc_uri = _uri("document", doc.id)
        g_source.add((doc_uri, RDF.type, KIPU.LegalDocument))
        g_source.add((doc_uri, KIPU.publicationCode, Literal(item.publication_code)))
        g_source.add((doc_uri, KIPU.documentNumber, Literal(doc.number_normalized)))
        g_source.add((doc_uri, KIPU.documentNumberRaw, Literal(doc.number_raw)))
        g_source.add((doc_uri, DCTERMS.title, Literal(doc.title_raw, lang="es")))
        if item.canonical_url:
            g_source.add((doc_uri, DCTERMS.source, URIRef(item.canonical_url)))
        if doc.issued_on:
            g_source.add((doc_uri, KIPU.issuedOn, Literal(doc.issued_on, datatype=XSD.date)))
        if doc.published_on:
            g_source.add((doc_uri, KIPU.publishedOn, Literal(doc.published_on, datatype=XSD.date)))
        if doc.issue_place_raw:
            g_source.add((doc_uri, KIPU.issuePlaceRaw, Literal(doc.issue_place_raw)))

        versions = (
            session.execute(
                select(m.ArtifactVersion)
                .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
                .where(m.Artifact.publication_item_id == item.id)
            )
            .scalars()
            .all()
        )
        for version in versions:
            v_uri = _uri("artifact-version", version.id)
            g_source.add((v_uri, RDF.type, KIPU.ArtifactVersion))
            g_source.add((v_uri, KIPU.sha256, Literal(version.sha256)))
            g_source.add(
                (v_uri, KIPU.capturedAt, Literal(version.captured_at, datatype=XSD.dateTime))
            )
            g_source.add((doc_uri, PROV.wasDerivedFrom, v_uri))

        runs = (
            session.execute(
                select(m.ExtractionRun)
                .join(
                    m.ArtifactVersion, m.ArtifactVersion.id == m.ExtractionRun.artifact_version_id
                )
                .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
                .where(m.Artifact.publication_item_id == item.id)
            )
            .scalars()
            .all()
        )
        for run in runs:
            r_uri = _uri("extraction-run", run.id)
            g_extraction.add((r_uri, RDF.type, KIPU.ExtractionRun))
            g_extraction.add((r_uri, KIPU.parserVersion, Literal(run.parser_version)))
            g_extraction.add((r_uri, KIPU.extractorVersion, Literal(run.extractor_version)))
            g_extraction.add((r_uri, KIPU.ontologyVersion, Literal(run.ontology_version)))
            if run.model_provider:
                g_extraction.add((r_uri, KIPU.modelProvider, Literal(run.model_provider)))

        self._project_evidence_and_mentions(g_accepted, doc)
        self._project_events(g_accepted, doc)
        self._project_assertions(g_candidate, g_accepted, item)

    def _project_evidence_and_mentions(self, graph: Graph, doc: m.LegalDocument) -> None:
        session = self._session
        mentions = (
            session.execute(
                select(m.PersonMention).where(m.PersonMention.legal_document_id == doc.id)
            )
            .scalars()
            .all()
        )
        for mention in mentions:
            mu = _uri("person-mention", mention.id)
            graph.add((mu, RDF.type, KIPU.PersonMention))
            graph.add((mu, KIPU.mentionText, Literal(mention.text_raw)))
            graph.add((mu, KIPU.mentionTextNormalized, Literal(mention.text_normalized)))
            graph.add((mu, KIPU.resolutionStatus, Literal(str(mention.resolution_status))))
            self._project_span(graph, mention.evidence_span_id, mu)
            if mention.canonical_person_id:
                pu = _uri("person", mention.canonical_person_id)
                person = session.get(m.Person, mention.canonical_person_id)
                graph.add((mu, KIPU.resolvesTo, pu))
                graph.add((pu, RDF.type, KIPU.Person))
                if person is not None:
                    graph.add((pu, KIPU.preferredName, Literal(person.preferred_name)))

    def _project_span(self, graph: Graph, span_id: str, subject: URIRef) -> URIRef:
        span = self._session.get(m.EvidenceSpan, span_id)
        assert span is not None
        su = _uri("evidence", span.id)
        graph.add((subject, KIPU.evidence, su))
        graph.add((su, RDF.type, KIPU.EvidenceSpan))
        graph.add((su, KIPU.quotedText, Literal(span.quoted_text, lang="es")))
        graph.add((su, KIPU.quotedTextSha256, Literal(span.quoted_text_sha256)))
        graph.add((su, KIPU.inArtifactVersion, _uri("artifact-version", span.artifact_version_id)))
        if span.article_label:
            graph.add((su, KIPU.articleLabel, Literal(span.article_label)))
        return su

    def _project_events(self, graph: Graph, doc: m.LegalDocument) -> None:
        session = self._session
        doc_uri = _uri("document", doc.id)
        events = (
            session.execute(
                select(m.PersonnelEvent).where(m.PersonnelEvent.legal_document_id == doc.id)
            )
            .scalars()
            .all()
        )
        for event in events:
            eu = _uri("event", event.id)
            graph.add((eu, RDF.type, KIPU.PersonnelEvent))
            graph.add((eu, KIPU.declaredBy, doc_uri))
            graph.add((eu, KIPU.eventType, KIPU[f"evt-{event.event_type}"]))
            graph.add((eu, KIPU.assignmentEffect, Literal(str(event.assignment_effect))))
            graph.add((eu, KIPU.legalVerbRaw, Literal(event.legal_verb_raw)))
            graph.add((eu, KIPU.effectiveFromStatus, Literal(str(event.effective_from_status))))
            if event.effective_from:
                graph.add(
                    (eu, KIPU.effectiveFrom, Literal(event.effective_from, datatype=XSD.date))
                )
            graph.add((eu, KIPU.effectiveToStatus, Literal(str(event.effective_to_status))))
            if event.effective_to:
                graph.add((eu, KIPU.effectiveTo, Literal(event.effective_to, datatype=XSD.date)))
            if event.legal_effect_from:
                # La fecha determinada por norma va en su propia propiedad, con
                # la norma citada: quien consuma el grafo tiene que poder separar
                # lo que el documento dice de lo que la ley añade.
                graph.add(
                    (
                        eu,
                        KIPU.legalEffectFrom,
                        Literal(event.legal_effect_from, datatype=XSD.date),
                    )
                )
                graph.add((eu, KIPU.legalEffectFromStatus, Literal(str(e.DateStatus.DERIVED))))
                basis = (event.legal_effect_basis_json or {}).get("basis") or {}
                if basis.get("norm"):
                    graph.add(
                        (
                            eu,
                            KIPU.legalBasis,
                            Literal(f"{basis['norm']}, artículo {basis.get('article')}", lang="es"),
                        )
                    )
                if basis.get("source_url"):
                    graph.add((eu, KIPU.legalBasisSource, URIRef(basis["source_url"])))
                rule = (event.legal_effect_basis_json or {}).get("rule")
                if rule:
                    graph.add((eu, KIPU.determinationRule, Literal(rule)))
            if event.end_condition_text:
                graph.add((eu, KIPU.endConditionText, Literal(event.end_condition_text, lang="es")))
            self._project_span(graph, event.evidence_span_id, eu)

            for participant in session.execute(
                select(m.EventParticipant).where(m.EventParticipant.event_id == event.id)
            ).scalars():
                if participant.person_mention_id:
                    graph.add(
                        (
                            eu,
                            KIPU.hasParticipant,
                            _uri("person-mention", participant.person_mention_id),
                        )
                    )

            started = (
                session.execute(
                    select(m.RoleAssignment).where(m.RoleAssignment.start_event_id == event.id)
                )
                .scalars()
                .all()
            )
            ended = (
                session.execute(
                    select(m.RoleAssignment).where(m.RoleAssignment.end_event_id == event.id)
                )
                .scalars()
                .all()
            )
            for ra in started:
                graph.add((eu, KIPU.startsAssignment, _uri("assignment", ra.id)))
                self._project_assignment(graph, ra)
            for ra in ended:
                graph.add((eu, KIPU.endsAssignment, _uri("assignment", ra.id)))
                self._project_assignment(graph, ra)
            if event.assignment_effect == e.AssignmentEffect.END and not ended:
                graph.add((eu, KIPU.affectedAssignmentUnresolved, Literal(True)))

    def _project_assignment(self, graph: Graph, ra: m.RoleAssignment) -> None:
        au = _uri("assignment", ra.id)
        graph.add((au, RDF.type, KIPU.RoleAssignment))
        graph.add((au, KIPU.assignmentMention, _uri("person-mention", ra.person_mention_id)))
        if ra.person_id:
            graph.add((au, KIPU.assignmentOf, _uri("person", ra.person_id)))
        if ra.position_id:
            position = self._session.get(m.Position, ra.position_id)
            pu = _uri("position", ra.position_id)
            graph.add((au, KIPU.assignmentPosition, pu))
            if position is not None:
                graph.add((pu, RDF.type, KIPU.Position))
                graph.add((pu, KIPU.positionLabel, Literal(position.preferred_label)))
                if position.organization_id:
                    org = self._session.get(m.Organization, position.organization_id)
                    ou = _uri("organization", position.organization_id)
                    graph.add((pu, KIPU.positionIn, ou))
                    if org is not None:
                        graph.add((ou, RDF.type, KIPU.Organization))
                        graph.add((ou, KIPU.preferredName, Literal(org.preferred_name)))
                if position.organizational_unit_id:
                    unit = self._session.get(m.OrganizationalUnit, position.organizational_unit_id)
                    uu = _uri("unit", position.organizational_unit_id)
                    graph.add((pu, KIPU.positionUnit, uu))
                    if unit is not None:
                        graph.add((uu, RDF.type, KIPU.OrganizationalUnit))
                        graph.add((uu, KIPU.preferredName, Literal(unit.preferred_name)))
                        if unit.parent_unit_id:
                            graph.add((uu, KIPU.parentUnit, _uri("unit", unit.parent_unit_id)))
                        graph.add((uu, KIPU.unitOf, _uri("organization", unit.organization_id)))
                for slot in self._session.execute(
                    select(m.PositionSlot).where(m.PositionSlot.position_id == position.id)
                ).scalars():
                    slot_uri = _uri("position-slot", slot.id)
                    graph.add((pu, KIPU.hasSlot, slot_uri))
                    graph.add((slot_uri, RDF.type, KIPU.PositionSlot))
                    graph.add((slot_uri, KIPU.externalScheme, Literal(slot.external_scheme)))
                    graph.add((slot_uri, KIPU.externalCode, Literal(slot.external_code)))
        if ra.position_label_raw:
            graph.add((au, KIPU.positionLabelRaw, Literal(ra.position_label_raw, lang="es")))
        graph.add((au, KIPU.assignmentKind, Literal(str(ra.assignment_kind))))
        if ra.valid_from:
            graph.add((au, KIPU.effectiveFrom, Literal(ra.valid_from, datatype=XSD.date)))
        graph.add((au, KIPU.effectiveFromStatus, Literal(str(ra.valid_from_status))))
        if ra.valid_to:
            graph.add((au, KIPU.effectiveTo, Literal(ra.valid_to, datatype=XSD.date)))
        graph.add((au, KIPU.effectiveToStatus, Literal(str(ra.valid_to_status))))
        if ra.legal_effect_from:
            graph.add((au, KIPU.legalEffectFrom, Literal(ra.legal_effect_from, datatype=XSD.date)))
        if ra.legal_effect_to:
            graph.add((au, KIPU.legalEffectTo, Literal(ra.legal_effect_to, datatype=XSD.date)))
        if ra.end_condition_text:
            graph.add((au, KIPU.endConditionText, Literal(ra.end_condition_text, lang="es")))
        if ra.mandate_id:
            mandate = self._session.get(m.Mandate, ra.mandate_id)
            mu = _uri("mandate", ra.mandate_id)
            graph.add((au, KIPU.underMandate, mu))
            if mandate is not None:
                graph.add((mu, RDF.type, KIPU.Mandate))
                graph.add((mu, KIPU.mandateType, Literal(str(mandate.mandate_type))))
                graph.add((mu, DCTERMS.title, Literal(mandate.label, lang="es")))
                if mandate.end_condition_text:
                    graph.add(
                        (mu, KIPU.endConditionText, Literal(mandate.end_condition_text, lang="es"))
                    )

    def _project_assertions(
        self, g_candidate: Graph, g_accepted: Graph, item: m.PublicationItem
    ) -> None:
        session = self._session
        assertions = (
            session.execute(
                select(m.Assertion)
                .join(m.ExtractionRun, m.ExtractionRun.id == m.Assertion.extraction_run_id)
                .join(
                    m.ArtifactVersion, m.ArtifactVersion.id == m.ExtractionRun.artifact_version_id
                )
                .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
                .where(m.Artifact.publication_item_id == item.id)
            )
            .scalars()
            .all()
        )
        for assertion in assertions:
            graph = g_accepted if assertion.review_status in _ACCEPTED else g_candidate
            au = _uri("assertion", assertion.id)
            graph.add((au, RDF.type, KIPU.Assertion))
            graph.add((au, KIPU.reviewStatus, Literal(str(assertion.review_status))))
            graph.add(
                (au, KIPU.confidence, Literal(round(assertion.confidence, 3), datatype=XSD.decimal))
            )
            subject_kind = _SUBJECT_TYPE_TO_URI_KIND.get(
                assertion.subject_type, assertion.subject_type
            )
            graph.add(
                (
                    au,
                    DCTERMS.subject,
                    _uri(subject_kind, assertion.subject_id or "unknown"),
                )
            )
            graph.add(
                (au, KIPU.wasExtractedBy, _uri("extraction-run", assertion.extraction_run_id))
            )
            self._project_span(graph, assertion.evidence_span_id, au)
