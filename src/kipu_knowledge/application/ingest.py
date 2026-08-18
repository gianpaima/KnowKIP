"""Caso de uso de ingesta: URL/archivo/fixture → captura → CAS → parse → extracción → BD.

La captura y el respaldo inmutable ocurren SIEMPRE antes del parsing. Los datos
derivados nunca sobrescriben la evidencia (los bytes viven en el ArtifactStore y
las secciones conservan el texto original).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from kipu_knowledge import CRAWLER_VERSION, PARSER_VERSION
from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.extraction.deterministic import DeterministicExtractor
from kipu_knowledge.adapters.parsing.html_parser import ElPeruanoHtmlParser
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.adapters.sources.elperuano import BASE_URL, ElPeruanoSourceAdapter
from kipu_knowledge.application.corroboration import (
    RULE_VERSION,
    RecitalCandidate,
    RecitalOutcome,
    bare_document_number,
    corroborate_recital,
)
from kipu_knowledge.application.issuer import IssuerOutcome, ensure_document_issuer
from kipu_knowledge.application.legal_effect import (
    apply_verdict,
    build_publication_date_span,
    publication_date_span,
    record_assertion,
    verdict_for_event,
)
from kipu_knowledge.application.source_links import (
    get_or_create_official_gazette,
    latest_html_version,
    official_publication_item,
)
from kipu_knowledge.domain import enums as e
from kipu_knowledge.domain.contracts import (
    ArtifactStore,
    CaptureRecord,
    DocumentParser,
    SourceAdapter,
    SourceReference,
    StructuredExtractor,
)
from kipu_knowledge.domain.extraction_models import (
    EvidenceRef,
    ExtractedEvent,
    ExtractedIdentifier,
    ExtractionResult,
)
from kipu_knowledge.domain.legal_effect import (
    LegalEffectOutcome,
    LegalEffectVerdict,
)
from kipu_knowledge.domain.normalization import (
    normalize_identifier,
    normalize_org_name,
    normalize_person_name,
    normalize_position_label,
    org_name_contamination,
)
from kipu_knowledge.domain.parsed import ParsedDocument
from kipu_knowledge.domain.state_entities import (
    catalog_entity,
    looks_like_uncatalogued_ministry,
    parent_entity,
)
from kipu_knowledge.ontology_version import ONTOLOGY_VERSION


@dataclass
class IngestOutcome:
    publication_item_id: str
    artifact_version_id: str
    legal_document_id: str | None
    extraction_run_id: str | None
    created: bool
    event_ids: list[str] = field(default_factory=list)
    assignment_ids: list[str] = field(default_factory=list)
    review_task_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class IngestService:
    def __init__(
        self,
        session: Session,
        store: ArtifactStore,
        parser: DocumentParser | None = None,
        extractor: StructuredExtractor | None = None,
        adapter: SourceAdapter | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._parser = parser or ElPeruanoHtmlParser()
        self._extractor = extractor or DeterministicExtractor()
        # Inyectable para que las pruebas del recolector diario puedan servir
        # fixtures en lugar de red; en producción es siempre el adaptador real.
        self._adapter = adapter or ElPeruanoSourceAdapter()

    # ------------------------------------------------------------------
    # Puntos de entrada
    # ------------------------------------------------------------------

    def ingest_url(self, url: str) -> IngestOutcome:
        reference = self._adapter.parse_source_reference(url)
        fetch = self._adapter.fetch(reference)  # falla si LIVE_SOURCE_ENABLED=false
        return self.ingest_bytes(reference, fetch.content, fetch.capture)

    def ingest_file(self, path: Path, publication_code: str | None = None) -> IngestOutcome:
        path = Path(path)
        code = publication_code or path.stem
        reference = self._adapter.parse_source_reference(code)
        content = path.read_bytes()
        capture = CaptureRecord(
            requested_url=None,
            final_url=None,
            http_status=None,
            content_type="text/html",
            byte_length=len(content),
            captured_at=datetime.now(UTC),
            crawler_version=f"{CRAWLER_VERSION}+local-file",
        )
        return self.ingest_bytes(reference, content, capture)

    def ingest_fixture(self, fixture_name: str, fixtures_dir: Path) -> IngestOutcome:
        fixtures_dir = Path(fixtures_dir)
        path = fixtures_dir / "elperuano" / f"{fixture_name}.html"
        if not path.exists():
            raise FileNotFoundError(f"Fixture no encontrado: {path}")
        manifest_path = fixtures_dir / "manifest.json"
        manifest: dict = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = manifest.get("items", {}).get(fixture_name, {})
        reference = SourceReference(
            source_family=manifest.get("source_family", "EL_PERUANO_NL"),
            source_series=item.get("series", "NL"),
            publication_code=fixture_name,
            canonical_url=item.get("url", f"{BASE_URL}/dispositivo/NL/{fixture_name}"),
        )
        content = path.read_bytes()
        capture = CaptureRecord(
            requested_url=item.get("url"),
            final_url=item.get("url"),
            http_status=manifest.get("http_status"),
            content_type=manifest.get("content_type", "text/html"),
            byte_length=len(content),
            captured_at=datetime.now(UTC),
            crawler_version=manifest.get("crawler_version", "fixture"),
        )
        return self.ingest_bytes(reference, content, capture)

    def reprocess(self, publication_code: str) -> IngestOutcome:
        item = official_publication_item(self._session, publication_code)
        if item is None:
            raise LookupError(f"Publicación no ingerida: {publication_code}")
        version = latest_html_version(self._session, item.id)
        if version is None:
            raise LookupError(f"No hay artefacto HTML para {publication_code}")
        reference = SourceReference(
            source_family="EL_PERUANO_NL",
            source_series=item.source_series,
            publication_code=item.publication_code,
            canonical_url=item.canonical_url,
        )
        content = self._store.get(version.object_key)
        return self._process_document(item, version, reference, content, reprocess=True)

    # ------------------------------------------------------------------
    # Núcleo
    # ------------------------------------------------------------------

    def ingest_bytes(
        self,
        reference: SourceReference,
        content: bytes,
        capture: CaptureRecord,
        representation: e.RepresentationType = e.RepresentationType.HTML,
    ) -> IngestOutcome:
        session = self._session
        item = self._get_or_create_publication_item(reference)
        artifact = self._get_or_create_artifact(item, representation, capture.content_type)

        stored = self._store.put_immutable(content)
        version = session.execute(
            select(m.ArtifactVersion).where(
                m.ArtifactVersion.artifact_id == artifact.id,
                m.ArtifactVersion.sha256 == stored.sha256,
            )
        ).scalar_one_or_none()
        if version is None:
            previous = self._latest_version_of(artifact.id)
            version = m.ArtifactVersion(
                artifact_id=artifact.id,
                sha256=stored.sha256,
                byte_length=stored.byte_length,
                captured_at=capture.captured_at,
                http_status=capture.http_status,
                etag=capture.etag,
                last_modified=capture.last_modified,
                response_headers=capture.response_headers or None,
                requested_url=capture.requested_url,
                final_url=capture.final_url,
                object_key=stored.object_key,
                crawler_version=capture.crawler_version,
                previous_version_id=previous.id if previous else None,
            )
            session.add(version)
            session.flush()

        existing_doc = session.execute(
            select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
        ).scalar_one_or_none()
        if existing_doc is not None:
            return IngestOutcome(
                publication_item_id=item.id,
                artifact_version_id=version.id,
                legal_document_id=existing_doc.id,
                extraction_run_id=None,
                created=False,
                warnings=["documento ya ingerido; usa reprocess para re-extraer"],
            )
        return self._process_document(item, version, reference, content, reprocess=False)

    def _process_document(
        self,
        item: m.PublicationItem,
        version: m.ArtifactVersion,
        reference: SourceReference,
        content: bytes,
        *,
        reprocess: bool,
    ) -> IngestOutcome:
        session = self._session
        self._carried_sources: list[tuple[str, e.DocumentSourceRole, str]] = []
        self._precedents_to_repoint: list[tuple[str, str, str | None]] = []
        self._persons_touched: set[str] = set()
        self._organizations_touched: set[str] = set()
        self._positions_touched: set[str] = set()
        if reprocess:
            self._supersede_previous(item)

        parsed = self._parser.parse(content, reference)
        # La forma derivable del código manda; la que declara el payload solo
        # entra si aquella no pudo construirse, porque su token opaco caduca.
        # El PDF que declara la captura es el archivo de verdad: la ruta /pdf
        # derivable del código devuelve el visor HTML, no el documento.
        if parsed.pdf_url:
            item.pdf_url = parsed.pdf_url
        doc = m.LegalDocument(
            publication_item_id=item.id,
            document_type_raw=parsed.document_type_raw,
            document_type_code=parsed.document_type_code,
            number_raw=parsed.number_raw,
            number_normalized=parsed.number_normalized,
            title_raw=parsed.title_raw,
            issue_place_raw=parsed.issue_place_raw,
            issued_on=parsed.issued_on,
            published_on=parsed.published_on,
            parsed_from_artifact_version_id=version.id,
        )
        session.add(doc)
        session.flush()
        # La publicación de la que se extrae es, por definición, la autoritativa.
        # Queda registrada como una fila más de `document_source` para que la
        # consulta "¿dónde está publicado este acto?" tenga una sola respuesta,
        # no dos caminos (el FK directo y la tabla) que puedan divergir.
        session.add(
            m.DocumentSource(
                legal_document_id=doc.id,
                publication_item_id=item.id,
                role=e.DocumentSourceRole.AUTHORITATIVE,
                matched_by="publicación de la que se parseó el documento",
            )
        )
        # Las publicaciones corroborantes que un operador enlazó siguen siendo
        # del mismo acto después de re-extraerlo: el acto no cambió, cambió cómo
        # lo leemos. Volverlas a exigir a mano sería perder trabajo humano.
        for publication_item_id, role, matched_by in self._carried_sources:
            session.add(
                m.DocumentSource(
                    legal_document_id=doc.id,
                    publication_item_id=publication_item_id,
                    role=role,
                    matched_by=matched_by,
                )
            )
        session.flush()

        section_rows: dict[int, m.DocumentSection] = {}
        for s in parsed.sections:
            row = m.DocumentSection(
                legal_document_id=doc.id,
                section_type=s.section_type,
                label_raw=s.label_raw,
                order_index=s.order_index,
                text_raw=s.text_raw,
                text_normalized=s.text_normalized,
            )
            session.add(row)
            section_rows[s.order_index] = row
        session.flush()

        run = m.ExtractionRun(
            artifact_version_id=version.id,
            parser_version=PARSER_VERSION,
            extractor_version=self._extractor.extractor_version,
            ontology_version=ONTOLOGY_VERSION,
        )
        session.add(run)
        session.flush()

        try:
            result = self._extractor.extract(parsed)
            persister = _ResultPersister(session, doc, run, version, section_rows, parsed)
            outcome_ids = persister.persist(result)
        except Exception:
            run.status = e.ExtractionStatus.FAILED
            run.completed_at = datetime.now(UTC)
            session.flush()
            raise

        run.status = e.ExtractionStatus.COMPLETED
        run.completed_at = datetime.now(UTC)
        session.flush()

        # Las menciones nuevas ya existen: los precedentes humanos que citaban a
        # las retiradas vuelven a tener origen, y las fichas que la extracción
        # anterior había creado y esta ya no sostiene se retiran.
        orphaned_precedents = self._repoint_precedents(doc)
        self._drop_persons_without_evidence()

        # El emisor lo declara el índice diario, no el dispositivo; si la
        # bitácora del recolector lo señala, se registra aquí mismo con su cita
        # para que ni el reproceso ni el reintento lo dejen vacío.
        issuer = ensure_document_issuer(session, self._store, doc)
        # Los huérfanos se evalúan después de reasegurar el emisor: su mención
        # recién re-creada es un sostén legítimo que el borrado debe ver.
        self._drop_positions_without_holders()
        self._drop_organizations_without_evidence()
        if reprocess:
            self._drop_variant_tasks_without_premise()
        issuer_warnings = (
            [f"emisor declarado pero no registrado ({issuer.outcome}): {issuer.detail}"]
            if issuer.outcome in (IssuerOutcome.NO_CAPTURE, IssuerOutcome.NO_EVIDENCE)
            else []
        )

        return IngestOutcome(
            publication_item_id=item.id,
            artifact_version_id=version.id,
            legal_document_id=doc.id,
            extraction_run_id=run.id,
            created=True,
            event_ids=outcome_ids["events"],
            assignment_ids=outcome_ids["assignments"],
            review_task_ids=outcome_ids["tasks"] + orphaned_precedents,
            warnings=result.warnings + issuer_warnings,
        )

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _get_or_create_source_system(self) -> m.SourceSystem:
        return get_or_create_official_gazette(self._session)

    def _get_or_create_publication_item(self, reference: SourceReference) -> m.PublicationItem:
        source = self._get_or_create_source_system()
        row = self._session.execute(
            select(m.PublicationItem).where(
                m.PublicationItem.source_system_id == source.id,
                m.PublicationItem.source_series == reference.source_series,
                m.PublicationItem.publication_code == reference.publication_code,
            )
        ).scalar_one_or_none()
        if row is None:
            row = m.PublicationItem(
                source_system_id=source.id,
                source_series=reference.source_series,
                publication_code=reference.publication_code,
                canonical_url=reference.canonical_url,
            )
            self._session.add(row)
            self._session.flush()
        elif row.source_system_id is None:
            row.source_system_id = source.id
        return row

    def _get_or_create_artifact(
        self,
        item: m.PublicationItem,
        representation: e.RepresentationType,
        media_type: str | None,
    ) -> m.Artifact:
        row = self._session.execute(
            select(m.Artifact).where(
                m.Artifact.publication_item_id == item.id,
                m.Artifact.representation_type == representation,
            )
        ).scalar_one_or_none()
        if row is None:
            row = m.Artifact(
                publication_item_id=item.id,
                representation_type=representation,
                media_type=media_type,
            )
            self._session.add(row)
            self._session.flush()
        return row

    def _latest_version_of(self, artifact_id: str) -> m.ArtifactVersion | None:
        """Última versión capturada DEL MISMO artefacto.

        La cadena `previous_version_id` es el histórico de una representación
        concreta: encadenar la primera captura de un PDF con la última del HTML
        mezcla dos series distintas y vuelve ilegible el "¿qué cambió respecto
        de la vez anterior?". El artefacto ya es único por (publicación,
        representación), así que basta con él.
        """
        return (
            self._session.execute(
                select(m.ArtifactVersion)
                .where(m.ArtifactVersion.artifact_id == artifact_id)
                .order_by(m.ArtifactVersion.captured_at.desc())
            )
            .scalars()
            .first()
        )

    def _supersede_previous(self, item: m.PublicationItem) -> None:
        """Reproceso: las afirmaciones anteriores se marcan SUPERSEDED (nunca se
        borran) y las filas derivadas deterministas se retiran para reconstruirse.
        La cadena de auditoría completa queda en `assertion` + `extraction_run`."""
        session = self._session
        now = datetime.now(UTC)
        doc = session.execute(
            select(m.LegalDocument).where(m.LegalDocument.publication_item_id == item.id)
        ).scalar_one_or_none()
        if doc is None:
            return
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
            for assertion in session.execute(
                select(m.Assertion).where(m.Assertion.extraction_run_id == run.id)
            ).scalars():
                if assertion.review_status not in (
                    e.ReviewStatus.SUPERSEDED,
                    e.ReviewStatus.HUMAN_REJECTED,
                ):
                    assertion.review_status = e.ReviewStatus.SUPERSEDED
                    assertion.superseded_at = now

        # Filas derivadas (proyección determinista del documento): se reconstruyen.
        # El borrado va por fases con flush entre ellas. Los modelos no declaran
        # `relationship()`, así que la unit of work no deduce que role_assignment y
        # event_participant dependen de personnel_event —las FKs del metadata no
        # ordenan por sí solas— y emitía el DELETE del evento primero, violando la
        # FK. Las fases imponen ese orden explícitamente.
        event_ids = list(
            session.execute(
                select(m.PersonnelEvent.id).where(m.PersonnelEvent.legal_document_id == doc.id)
            ).scalars()
        )
        if event_ids:
            for row in session.execute(
                select(m.EventParticipant).where(m.EventParticipant.event_id.in_(event_ids))
            ).scalars():
                session.delete(row)
            for ra in session.execute(
                select(m.RoleAssignment).where(
                    m.RoleAssignment.start_event_id.in_(event_ids)
                    | m.RoleAssignment.end_event_id.in_(event_ids)
                )
            ).scalars():
                # Los puestos y organizaciones que estas asignaciones sostenían
                # pueden quedar sin nada tras re-extraer; ver
                # `_drop_positions_without_holders` y
                # `_drop_organizations_without_evidence`.
                if ra.position_id:
                    self._positions_touched.add(ra.position_id)
                if ra.organization_id:
                    self._organizations_touched.add(ra.organization_id)
                session.delete(ra)
            session.flush()
            for event in session.execute(
                select(m.PersonnelEvent).where(m.PersonnelEvent.id.in_(event_ids))
            ).scalars():
                session.delete(event)
            session.flush()
        self._detach_evidence_from_superseded_sections(doc)
        self._release_precedents_from_superseded_mentions(doc)
        # Personas a las que apuntaban las menciones que van a retirarse: si tras
        # re-extraer se quedan sin nada que las sostenga, son residuo de la
        # extracción anterior y no una ficha (ver `_drop_persons_without_evidence`).
        self._persons_touched = {
            person_id
            for person_id in session.execute(
                select(m.PersonMention.canonical_person_id).where(
                    m.PersonMention.legal_document_id == doc.id
                )
            ).scalars()
            if person_id
        }

        table: type[m.Signatory] | type[m.PersonMention] | type[m.DocumentSection]
        for table in (m.Signatory, m.PersonMention, m.DocumentSection):
            for derived in session.execute(
                select(table).where(table.legal_document_id == doc.id)
            ).scalars():
                # Las tareas abiertas sobre la mención que se retira son residuo
                # de la extracción reemplazada: la re-extracción abre las suyas
                # sobre las menciones nuevas. Sin esto quedaban tareas PENDING
                # apuntando a filas inexistentes (igual que puestos y orgs).
                if table is m.PersonMention:
                    self._drop_pending_tasks_for("person_mention", derived.id)
                session.delete(derived)
        # Las menciones de organización se rehacen como las de persona. El emisor
        # apunta a una de ellas desde el propio documento, así que ese puntero se
        # suelta antes: la fila que lo satisface está a punto de irse.
        doc.issuer_mention_id = None
        session.flush()
        for org_mention in session.execute(
            select(m.OrganizationMention).where(m.OrganizationMention.legal_document_id == doc.id)
        ).scalars():
            if org_mention.canonical_organization_id:
                self._organizations_touched.add(org_mention.canonical_organization_id)
            session.delete(org_mention)
        session.flush()
        for reference in session.execute(
            select(m.DocumentReference).where(m.DocumentReference.source_document_id == doc.id)
        ).scalars():
            session.delete(reference)

        # Dónde más está publicado el acto lo estableció un operador con su
        # motivo (`kipu link-source --matched-by`); no lo deduce nadie. Se
        # arrastra al documento nuevo en vez de perderse con el viejo. La fila
        # AUTHORITATIVE no viaja: la vuelve a escribir el propio reproceso.
        carried: list[tuple[str, e.DocumentSourceRole, str]] = []
        for source in session.execute(
            select(m.DocumentSource).where(m.DocumentSource.legal_document_id == doc.id)
        ).scalars():
            if source.role != e.DocumentSourceRole.AUTHORITATIVE:
                carried.append((source.publication_item_id, source.role, source.matched_by))
            session.delete(source)
        self._carried_sources = carried

        session.delete(doc)
        session.flush()

    def _detach_evidence_from_superseded_sections(self, doc: m.LegalDocument) -> None:
        """Desancla de sus secciones los spans de evidencia antes de retirarlas.

        Una afirmación supersedida conserva su evidencia: eso es lo que la hace
        auditable (regla 3). Pero la sección es solo el puntero cómodo a dónde
        estaba el texto según la versión del parser que la produjo, y esa
        versión es precisamente lo que el reproceso reemplaza. Lo verificable
        —la cita literal, su sha256, el artefacto y la etiqueta del artículo—
        vive en el propio span y no se toca; lo que se retira es el puntero.

        Sin esto, el DELETE de `document_section` violaba la FK de
        `evidence_span` y `kipu reprocess` fallaba entero contra PostgreSQL. En
        SQLite las FKs no se comprueban por defecto y las pruebas no lo veían.
        """
        session = self._session
        sections = {
            row.id: row
            for row in session.execute(
                select(m.DocumentSection).where(m.DocumentSection.legal_document_id == doc.id)
            ).scalars()
        }
        if not sections:
            return
        for span in session.execute(
            select(m.EvidenceSpan).where(m.EvidenceSpan.document_section_id.in_(list(sections)))
        ).scalars():
            section = sections[str(span.document_section_id)]
            # Dónde estuvo la sección se conserva como dato: quien lea una
            # afirmación supersedida tiene que poder situar la cita, aunque la
            # fila que la contenía ya no exista.
            locator = dict(span.locator_json or {})
            locator["superseded_section"] = {
                "order_index": section.order_index,
                "section_type": str(section.section_type),
                "label_raw": section.label_raw,
                "parser_version": PARSER_VERSION,
            }
            span.locator_json = locator
            span.document_section_id = None
        session.flush()

    def _release_precedents_from_superseded_mentions(self, doc: m.LegalDocument) -> None:
        """Suelta los precedentes que citan menciones a punto de retirarse.

        Un precedente de identidad cita la mención que motivó la decisión
        humana. Esa mención desaparece al re-extraer, pero la decisión sigue en
        pie: lo que el revisor afirmó —"esta grafía, con este cargo, es esta
        persona"— vive en las columnas del precedente y en su ReviewDecision,
        no en el puntero. Aquí se anota qué buscar para volver a apuntarlo
        (`_precedents_to_repoint`) y se suelta el puntero, que es lo único que
        impedía el DELETE. Si tras la extracción no reaparece una mención
        equivalente, el precedente queda sin origen y eso lo decide un humano.
        """
        session = self._session
        rows = session.execute(
            select(m.IdentityPrecedent, m.PersonMention)
            .join(
                m.PersonMention, m.PersonMention.id == m.IdentityPrecedent.source_person_mention_id
            )
            .where(m.PersonMention.legal_document_id == doc.id)
        ).all()
        for precedent, mention in rows:
            self._precedents_to_repoint.append(
                (precedent.id, mention.text_normalized, mention.role_context_normalized)
            )
            precedent.source_person_mention_id = None
        session.flush()

    def _drop_persons_without_evidence(self) -> None:
        """Retira las fichas que la extracción reemplazada había creado y esta no.

        Una `Person` nace de una mención. Si al re-extraer el documento esa
        mención ya no aparece —cambió el parser, cambió el patrón— la ficha se
        queda sin nada que afirmar y, peor, la mención equivalente crea una
        ficha nueva: quedan dos donde hay una persona, una de ellas vacía. La
        ficha vacía no es un registro, es residuo.

        Solo se retira lo que no sostiene nada: sin menciones, sin asignaciones,
        sin precedentes que la citen, sin fusión de la que sea origen o destino.
        Cualquiera de esas cosas es una afirmación o una decisión humana, y
        entonces la ficha se queda aunque hoy esté callada.
        """
        session = self._session
        for person_id in self._persons_touched:
            person = session.get(m.Person, person_id)
            if person is None or person.merged_into_person_id is not None:
                continue
            holds = (
                select(m.PersonMention.id).where(m.PersonMention.canonical_person_id == person_id),
                select(m.RoleAssignment.id).where(m.RoleAssignment.person_id == person_id),
                select(m.IdentityPrecedent.id).where(m.IdentityPrecedent.person_id == person_id),
                select(m.Person.id).where(m.Person.merged_into_person_id == person_id),
            )
            if any(session.execute(stmt.limit(1)).first() for stmt in holds):
                continue
            session.delete(person)
        self._persons_touched.clear()
        session.flush()

    def _drop_pending_tasks_for(self, target_type: str, target_id: str) -> None:
        """Retira las tareas abiertas sobre una fila que va a borrarse.

        Solo las PENDING sin decisión: son residuo mecánico de la extracción
        reemplazada, igual que la fila a la que apuntan. Una tarea con decisión
        —resuelta o descartada— es trabajo humano y se conserva aunque su
        objetivo desaparezca; la UI ya tolera objetivos ausentes.
        """
        session = self._session
        for task in session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.target_type == target_type,
                m.ReviewTask.target_id == target_id,
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
        ).scalars():
            has_decision = session.execute(
                select(m.ReviewDecision.id)
                .where(m.ReviewDecision.review_task_id == task.id)
                .limit(1)
            ).first()
            if has_decision:
                continue
            session.delete(task)

    def _drop_positions_without_holders(self) -> None:
        """Retira los puestos que la extracción reemplazada creó y esta ya no usa.

        Espejo de `_drop_persons_without_evidence`: un Position nace de una
        asignación. Si tras re-extraer ninguna asignación lo referencia y ningún
        CAP lo declaró (PositionSlot), es residuo del patrón anterior —el caso
        típico: el puesto quedó colgado de una organización fabricada por una
        coletilla mal recortada— y conservarlo duplicaría el puesto real.
        """
        session = self._session
        for position_id in self._positions_touched:
            position = session.get(m.Position, position_id)
            if position is None:
                continue
            holds = (
                select(m.RoleAssignment.id).where(m.RoleAssignment.position_id == position_id),
                select(m.PositionSlot.id).where(m.PositionSlot.position_id == position_id),
            )
            if any(session.execute(stmt.limit(1)).first() for stmt in holds):
                continue
            self._drop_pending_tasks_for("position", position_id)
            session.delete(position)
        self._positions_touched.clear()
        session.flush()

    def _drop_organizations_without_evidence(self) -> None:
        """Retira las organizaciones que ya nada menciona ni usa.

        Una Organization nace de una mención. Si al re-extraer el documento la
        mención que la creó desaparece —el caso real: "Ministerio de Vivienda,
        Construcción y Saneamiento, bajo el régimen de la Ley N° 30057" tras
        corregir el recorte de coletillas— la ficha queda sin nada que afirmar,
        y peor: su nombre normalizado seguiría enlazando en silencio cualquier
        documento futuro con el mismo defecto.

        Solo se retira lo que no sostiene nada: sin menciones, sin puestos, sin
        asignaciones, sin unidades con puestos, y sin ser destino de una fusión
        humana. Las unidades vacías son filas derivadas y se van con ella.
        """
        session = self._session
        for org_id in self._organizations_touched:
            org = session.get(m.Organization, org_id)
            if org is None or org.merged_into_organization_id is not None:
                continue
            holds = (
                select(m.OrganizationMention.id).where(
                    m.OrganizationMention.canonical_organization_id == org_id
                ),
                select(m.Position.id).where(m.Position.organization_id == org_id),
                select(m.RoleAssignment.id).where(m.RoleAssignment.organization_id == org_id),
                select(m.Organization.id).where(
                    m.Organization.merged_into_organization_id == org_id
                ),
            )
            if any(session.execute(stmt.limit(1)).first() for stmt in holds):
                continue
            units = (
                session.execute(
                    select(m.OrganizationalUnit).where(
                        m.OrganizationalUnit.organization_id == org_id
                    )
                )
                .scalars()
                .all()
            )
            unit_ids = [unit.id for unit in units]
            if (
                unit_ids
                and session.execute(
                    select(m.Position.id)
                    .where(m.Position.organizational_unit_id.in_(unit_ids))
                    .limit(1)
                ).first()
            ):
                continue
            # Hijas antes que padres, o la FK autorreferente rechaza el borrado.
            remaining = {unit.id: unit for unit in units}
            while remaining:
                leaves = [
                    unit
                    for unit in remaining.values()
                    if not any(u.parent_unit_id == unit.id for u in remaining.values())
                ]
                for unit in leaves:
                    session.delete(remaining.pop(unit.id))
                session.flush()
            self._drop_pending_tasks_for("organization", org_id)
            session.delete(org)
        self._organizations_touched.clear()
        session.flush()

    def _drop_variant_tasks_without_premise(self) -> None:
        """Cierra las ORG_VARIANT_CHECK abiertas cuya variante similar ya no existe.

        La tarea pregunta "¿es esta organización la misma que aquella parecida?".
        Cuando el reproceso retira la parecida —era una fabricación del extractor
        anterior—, la pregunta se queda sin segundo término: se re-evalúa con la
        misma heurística que la abrió (`similar_org_exists`) y, si ya no hay
        ninguna candidata, la tarea es residuo. Solo se retiran tareas PENDING y
        sin decisión: lo que un humano tocó se conserva.
        """
        session = self._session
        for task in session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.task_type == e.ReviewTaskType.ORG_VARIANT_CHECK,
                m.ReviewTask.target_type == "organization",
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
        ).scalars():
            target = session.get(m.Organization, task.target_id)
            if (
                target is not None
                and target.merged_into_organization_id is None
                and SimpleEntityResolver.similar_org_exists(session, target.name_normalized)
                is not None
            ):
                continue
            has_decision = session.execute(
                select(m.ReviewDecision.id)
                .where(m.ReviewDecision.review_task_id == task.id)
                .limit(1)
            ).first()
            if has_decision:
                continue
            session.delete(task)
        session.flush()

    def _repoint_precedents(self, doc: m.LegalDocument) -> list[str]:
        """Vuelve a apuntar cada precedente a su mención en el documento nuevo.

        La equivalencia es exacta —mismo documento, misma grafía normalizada,
        mismo cargo declarado— porque cualquier cosa más laxa sería decidir una
        identidad, que es justo lo que un precedente no puede hacerse a sí
        mismo. Devuelve las tareas abiertas para los que se quedaron sin origen.
        """
        session = self._session
        task_ids: list[str] = []
        for precedent_id, name_normalized, role_context in self._precedents_to_repoint:
            precedent = session.get(m.IdentityPrecedent, precedent_id)
            if precedent is None:
                continue
            replacement = (
                session.execute(
                    select(m.PersonMention).where(
                        m.PersonMention.legal_document_id == doc.id,
                        m.PersonMention.text_normalized == name_normalized,
                        m.PersonMention.role_context_normalized.is_(None)
                        if role_context is None
                        else m.PersonMention.role_context_normalized == role_context,
                    )
                )
                .scalars()
                .first()
            )
            if replacement is not None:
                precedent.source_person_mention_id = replacement.id
                continue
            task = m.ReviewTask(
                task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
                target_type="identity_precedent",
                target_id=precedent.id,
                reason=(
                    f"El precedente sobre '{name_normalized}' quedó sin la mención que lo "
                    f"originó: al re-extraer el documento esa mención ya no aparece. La "
                    f"decisión sigue vigente y se seguirá aplicando; confirmarla o revocarla "
                    f"es decisión humana."
                ),
                priority=2,
            )
            session.add(task)
            session.flush()
            task_ids.append(task.id)
        self._precedents_to_repoint.clear()
        return task_ids


class _ResultPersister:
    """Convierte un ExtractionResult validado en filas canónicas con evidencia."""

    def __init__(
        self,
        session: Session,
        doc: m.LegalDocument,
        run: m.ExtractionRun,
        version: m.ArtifactVersion,
        section_rows: dict[int, m.DocumentSection],
        parsed: ParsedDocument,
    ) -> None:
        self._s = session
        self._doc = doc
        self._run = run
        self._version = version
        self._sections = section_rows
        self._parsed = parsed
        self._resolver = SimpleEntityResolver(session)
        self._task_ids: list[str] = []
        self._mention_cache: dict[str, m.PersonMention] = {}
        self._publication_date_span: m.EvidenceSpan | None = None

    # -- evidencia ------------------------------------------------------

    def _publication_date_evidence(self) -> m.EvidenceSpan | None:
        """Cita de la fecha de publicación, compartida por todos los eventos.

        La declara la página del buscador, fuera del contenedor del dispositivo,
        así que el span no cuelga de ninguna sección: su rango es sobre el texto
        del artefacto. Es la evidencia que sostiene la fecha determinada por
        norma, y sin ella esa fecha no se registra.
        """
        if self._publication_date_span is not None:
            return self._publication_date_span
        existing = publication_date_span(self._s, self._doc)
        if existing is not None:
            self._publication_date_span = existing
            return existing
        phrase = self._parsed.published_on_phrase
        start = self._parsed.published_on_char_start
        end = self._parsed.published_on_char_end
        if phrase is None or start is None or end is None:
            return None
        self._publication_date_span = build_publication_date_span(
            self._s, self._version.id, phrase, start, end
        )
        return self._publication_date_span

    def _evidence(self, ref: EvidenceRef) -> m.EvidenceSpan:
        section = self._sections[ref.section_index]
        span = m.EvidenceSpan(
            document_section_id=section.id,
            artifact_version_id=self._version.id,
            article_label=ref.article_label,
            char_start=ref.char_start,
            char_end=ref.char_end,
            quoted_text=ref.quoted_text,
            quoted_text_sha256=hashlib.sha256(ref.quoted_text.encode("utf-8")).hexdigest(),
            locator_json={"section_order_index": ref.section_index},
        )
        self._s.add(span)
        self._s.flush()
        return span

    def _assert_(
        self,
        subject_type: str,
        subject_id: str | None,
        predicate: str,
        evidence: m.EvidenceSpan,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        object_value: dict | None = None,
        confidence: float = 0.95,
        status: e.ReviewStatus = e.ReviewStatus.AUTO_ACCEPTED,
    ) -> m.Assertion:
        row = m.Assertion(
            extraction_run_id=self._run.id,
            subject_type=subject_type,
            subject_id=subject_id,
            predicate=predicate,
            object_type=object_type,
            object_id=object_id,
            object_value_json=object_value,
            confidence=confidence,
            evidence_span_id=evidence.id,
            review_status=status,
        )
        self._s.add(row)
        self._s.flush()
        return row

    def _task(
        self,
        task_type: e.ReviewTaskType,
        target_type: str,
        target_id: str,
        reason: str,
        priority: int = 3,
    ) -> None:
        task = m.ReviewTask(
            task_type=task_type,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            priority=priority,
        )
        self._s.add(task)
        self._s.flush()
        self._task_ids.append(task.id)

    # -- personas -------------------------------------------------------

    def _person_mention(
        self,
        text_raw: str,
        evidence: m.EvidenceSpan,
        role_context_raw: str | None = None,
        identifiers: Sequence[ExtractedIdentifier] | None = None,
    ) -> m.PersonMention:
        normalized = normalize_person_name(text_raw)
        cached = self._mention_cache.get(normalized)
        if cached is not None:
            return cached
        role_context = normalize_position_label(role_context_raw) if role_context_raw else None
        mention = m.PersonMention(
            legal_document_id=self._doc.id,
            text_raw=text_raw,
            text_normalized=normalized,
            role_context_raw=role_context_raw,
            role_context_normalized=role_context,
            evidence_span_id=evidence.id,
        )
        self._s.add(mention)
        self._s.flush()
        identifier_keys = self._persist_identifiers(mention, identifiers or ())

        if self._resolve_without_review(mention, evidence, identifier_keys):
            self._s.flush()
            self._mention_cache[normalized] = mention
            return mention

        proposals = self._resolver.propose_matches(normalized, {"kind": "person"})
        if not proposals:
            person = m.Person(preferred_name=text_raw)
            self._s.add(person)
            self._s.flush()
            mention.canonical_person_id = person.id
            mention.resolution_status = e.ResolutionStatus.AUTO_LINKED
            # Crear una persona nueva no fusiona nada, pero puede duplicar a alguien
            # ya registrado con otra grafía del mismo nombre. El duplicado se hace
            # visible; resolverlo sigue siendo decisión humana.
            variants = self._resolver.variant_person_candidates(normalized)
            if variants:
                self._task(
                    e.ReviewTaskType.PERSON_VARIANT_CHECK,
                    "person_mention",
                    mention.id,
                    f"Mención '{text_raw}' no coincide de forma exacta con ninguna persona, "
                    f"pero es grafía compatible de {len(variants)} existente(s): "
                    + "; ".join(f"'{prop.entity_label}'" for prop in variants)
                    + ". Se registró como persona nueva; confirmar o fusionar (regla 13)",
                    priority=2,
                )
                self._assert_(
                    "person_mention",
                    mention.id,
                    "mention_possibly_duplicates",
                    evidence,
                    object_type="person",
                    object_id=variants[0].entity_id,
                    object_value={
                        "candidates": [
                            {"person_id": prop.entity_id, "score": prop.score} for prop in variants
                        ]
                    },
                    confidence=max(prop.score for prop in variants),
                    status=e.ReviewStatus.CANDIDATE,
                )
        else:
            mention.resolution_status = e.ResolutionStatus.CANDIDATE_MATCH
            self._task(
                e.ReviewTaskType.ENTITY_RESOLUTION,
                "person_mention",
                mention.id,
                f"Mención '{text_raw}' coincide por nombre con {len(proposals)} persona(s) "
                f"existentes; vincular o crear nueva requiere revisión humana (regla 13)",
                priority=2,
            )
            self._assert_(
                "person_mention",
                mention.id,
                "mention_resolves_to",
                self._s.get(m.EvidenceSpan, mention.evidence_span_id),  # type: ignore[arg-type]
                object_type="person",
                object_id=proposals[0].entity_id,
                object_value={
                    "candidates": [
                        {"person_id": prop.entity_id, "score": prop.score} for prop in proposals
                    ]
                },
                confidence=max(prop.score for prop in proposals),
                status=e.ReviewStatus.CANDIDATE,
            )
        self._s.flush()
        self._mention_cache[normalized] = mention
        return mention

    def _persist_identifiers(
        self, mention: m.PersonMention, identifiers: Sequence[ExtractedIdentifier]
    ) -> list[tuple[str, str]]:
        """Guarda los documentos de identidad declarados y devuelve sus claves."""
        keys: list[tuple[str, str]] = []
        for extracted in identifiers:
            value_normalized = normalize_identifier(extracted.value_raw)
            if not value_normalized:
                continue
            self._s.add(
                m.PersonIdentifier(
                    person_mention_id=mention.id,
                    scheme=extracted.scheme,
                    value_raw=extracted.value_raw,
                    value_normalized=value_normalized,
                    evidence_span_id=self._evidence(extracted.evidence).id,
                )
            )
            keys.append((str(extracted.scheme), value_normalized))
        if keys:
            self._s.flush()
        return keys

    def _resolve_without_review(
        self,
        mention: m.PersonMention,
        evidence: m.EvidenceSpan,
        identifier_keys: list[tuple[str, str]],
    ) -> bool:
        """Vincula la mención cuando la evidencia descarta la homonimia.

        Tres señales, de más a menos fuerte: un identificador que la fuente
        declara, una decisión humana previa (precedente), y la coincidencia de
        nombre normalizado con un oficio del que hay un solo titular a la vez.
        Ninguna se apoya solo en el nombre, así que ninguna viola la regla 13.

        Si dos señales apuntan a personas distintas no se elige la más fuerte: se
        abre un conflicto y decide un humano. Devuelve True si la mención quedó
        resuelta (vinculada o marcada en conflicto).
        """
        normalized = mention.text_normalized
        role_context = mention.role_context_normalized
        signals: list[tuple[e.ResolutionStatus, str, str]] = []  # estado, persona, motivo
        conflicts: list[str] = []

        by_identifier = self._resolver.persons_by_identifier(identifier_keys)
        if len(by_identifier) > 1:
            conflicts.append(
                f"el identificador declarado coincide con {len(by_identifier)} personas distintas"
            )
        elif by_identifier:
            schemes = ", ".join(sorted({scheme for scheme, _ in identifier_keys}))
            signals.append(
                (
                    e.ResolutionStatus.IDENTIFIER_LINKED,
                    by_identifier[0].id,
                    f"identificador declarado por la fuente ({schemes})",
                )
            )

        precedent = self._resolver.person_precedent(normalized, role_context)
        if precedent is not None:
            signals.append(
                (
                    e.ResolutionStatus.PRECEDENT_LINKED,
                    precedent.person_id,
                    f"precedente humano sobre el cargo '{precedent.role_context}'",
                )
            )

        by_office = self._resolver.office_corroborated_persons(normalized, role_context)
        if len(by_office) > 1:
            conflicts.append(
                f"el oficio unipersonal '{role_context}' aparece con {len(by_office)} personas "
                "distintas bajo el mismo nombre"
            )
        elif by_office:
            signals.append(
                (
                    e.ResolutionStatus.OFFICE_CORROBORATED,
                    by_office[0].id,
                    f"nombre idéntico y oficio unipersonal '{role_context}'",
                )
            )

        targets = {person_id for _, person_id, _ in signals}
        if conflicts or len(targets) > 1:
            if len(targets) > 1:
                conflicts.append(
                    "las señales de identidad apuntan a personas distintas: "
                    + "; ".join(f"{reason} → {pid}" for _, pid, reason in signals)
                )
            mention.resolution_status = e.ResolutionStatus.CANDIDATE_MATCH
            self._task(
                e.ReviewTaskType.EXTRACTION_CONFLICT,
                "person_mention",
                mention.id,
                f"Mención '{mention.text_raw}': " + "; ".join(conflicts) + ". "
                "No se vincula automáticamente ante señales contradictorias (regla 13)",
                priority=1,
            )
            return True

        if not signals:
            return False

        # `signals` se construyó en orden de fuerza decreciente: identificador
        # declarado, decisión humana, corroboración por oficio.
        status, person_id, reason = signals[0]
        if status == e.ResolutionStatus.PRECEDENT_LINKED and precedent is not None:
            self._link_by_precedent(mention, precedent)
            return True
        mention.canonical_person_id = person_id
        mention.resolution_status = status
        self._assert_(
            "person_mention",
            mention.id,
            "mention_resolves_to",
            evidence,
            object_type="person",
            object_id=person_id,
            object_value={
                "resolution": str(status),
                "rationale": reason,
                "corroborating_signals": [
                    {"status": str(s), "person_id": pid, "rationale": r} for s, pid, r in signals
                ],
            },
            confidence=1.0 if status == e.ResolutionStatus.IDENTIFIER_LINKED else 0.95,
        )
        return True

    def _link_by_precedent(self, mention: m.PersonMention, precedent: m.IdentityPrecedent) -> None:
        """Aplica una decisión humana previa en lugar de reabrir la misma tarea.

        No se crea ReviewTask: el humano ya resolvió esta clave. La afirmación cita
        el precedente y la decisión que lo originó, de modo que la vinculación es
        rastreable hasta su autor y revocable.
        """
        mention.canonical_person_id = precedent.person_id
        mention.resolution_status = e.ResolutionStatus.PRECEDENT_LINKED
        mention.identity_precedent_id = precedent.id
        self._assert_(
            "person_mention",
            mention.id,
            "mention_resolves_to",
            self._s.get(m.EvidenceSpan, mention.evidence_span_id),  # type: ignore[arg-type]
            object_type="person",
            object_id=precedent.person_id,
            object_value={
                "identity_precedent_id": precedent.id,
                "review_decision_id": precedent.review_decision_id,
                "reviewer": precedent.reviewer,
                "role_context": precedent.role_context,
            },
            confidence=1.0,
            status=e.ReviewStatus.HUMAN_ACCEPTED,
        )

    # -- organizaciones / unidades / puestos ---------------------------

    def _organization(self, name: str, evidence: m.EvidenceSpan | None) -> m.Organization:
        normalized = normalize_org_name(name)
        # El catálogo curado enriquece la ficha nueva cuando la grafía coincide
        # exactamente con el nombre vigente de una entidad conocida: la sigla y
        # el tipo son datos declarados, no inferencias.
        entry = catalog_entity(normalized)
        if entry is not None:
            # Dos grafías vigentes de la misma entidad del catálogo ("Instituto
            # Peruano de Energía Nuclear" con o sin "– IPEN") son la misma
            # organización por dato declarado, no por inferencia de nombres:
            # reutilizar la ficha evita fabricar un duplicado que después solo
            # una fusión humana podría deshacer. Se resuelve antes que la
            # coincidencia exacta de grafía, y con preferencia determinista por
            # la ficha del nombre canónico, para que dos fichas de la misma
            # entidad converjan en una al reprocesar en vez de sostenerse
            # mutuamente.
            same_entity = [
                existing
                for existing in self._s.execute(
                    select(m.Organization)
                    .where(m.Organization.merged_into_organization_id.is_(None))
                    .order_by(m.Organization.id)
                ).scalars()
                if catalog_entity(existing.name_normalized) is entry
            ]
            if same_entity:
                canonical_normalized = normalize_org_name(entry.canonical_name)
                org = next(
                    (o for o in same_entity if o.name_normalized == canonical_normalized),
                    same_entity[0],
                )
                if org.name_normalized != normalized:
                    # La grafía distinta queda documentada como mención de la
                    # misma ficha; la idéntica ya lo está desde su creación.
                    self._s.add(
                        m.OrganizationMention(
                            legal_document_id=self._doc.id,
                            text_raw=name,
                            text_normalized=normalized,
                            canonical_organization_id=org.id,
                            resolution_status=e.ResolutionStatus.AUTO_LINKED,
                            evidence_span_id=evidence.id if evidence else None,
                        )
                    )
                    self._s.flush()
                return org
        proposals = self._resolver.propose_matches(normalized, {"kind": "organization"})
        if proposals:
            proposed = self._s.get(m.Organization, proposals[0].entity_id)
            assert proposed is not None
            return proposed
        org = m.Organization(preferred_name=name, name_normalized=normalized)
        if entry is not None:
            org.acronym = entry.acronym
            org.organization_type = entry.entity_type
            # La adscripción es dato curado del catálogo, no inferencia: si la
            # entidad madre ya tiene ficha, el programa cuelga de ella. Si aún
            # no la tiene, no se crea (el catálogo no crea filas): el vínculo
            # se sentará cuando una mención con evidencia la haga nacer.
            parent = parent_entity(entry)
            if parent is not None:
                parent_row = self._s.execute(
                    select(m.Organization).where(
                        m.Organization.name_normalized.in_(
                            [
                                normalize_org_name(parent.canonical_name),
                                normalize_org_name(f"{parent.canonical_name} - {parent.acronym}"),
                            ]
                            if parent.acronym
                            else [normalize_org_name(parent.canonical_name)]
                        ),
                        m.Organization.merged_into_organization_id.is_(None),
                    )
                ).scalar_one_or_none()
                if parent_row is not None:
                    org.parent_organization_id = parent_row.id
        self._s.add(org)
        self._s.flush()
        mention = m.OrganizationMention(
            legal_document_id=self._doc.id,
            text_raw=name,
            text_normalized=normalized,
            canonical_organization_id=org.id,
            resolution_status=e.ResolutionStatus.AUTO_LINKED,
            evidence_span_id=evidence.id if evidence else None,
        )
        self._s.add(mention)
        # Una sola tarea por ficha nueva, la más específica primero: la
        # contaminación explica la variante y la ausencia del catálogo, así que
        # abrir las tres sería repetir la misma pregunta con menos precisión.
        contaminant = org_name_contamination(normalized)
        similar = self._resolver.similar_org_exists(self._s, normalized)
        if contaminant is not None:
            self._task(
                e.ReviewTaskType.EXTRACTION_CONFLICT,
                "organization",
                org.id,
                f"'{name}' contiene la coletilla administrativa «{contaminant}»: la "
                f"extracción arrastró la cláusula de contratación dentro del nombre del "
                f"órgano. Corregir el extractor y reprocesar, o fusionar la organización "
                f"con la ficha limpia",
                priority=2,
            )
        elif similar is not None:
            self._task(
                e.ReviewTaskType.ORG_VARIANT_CHECK,
                "organization",
                org.id,
                f"'{name}' es similar a la organización existente '{similar.preferred_name}'; "
                f"confirmar si es la misma entidad, una variante o una sucesión",
            )
        elif looks_like_uncatalogued_ministry(normalized):
            self._task(
                e.ReviewTaskType.ONTOLOGY_CANDIDATE,
                "organization",
                org.id,
                f"'{name}' se llama ministerio pero no figura en el catálogo de carteras "
                f"(domain/state_entities.py): puede ser una entidad nueva o renombrada "
                f"—que merece entrar al catálogo con su norma— o una extracción "
                f"defectuosa que hay que corregir",
                priority=2,
            )
        self._s.flush()
        return org

    def _unit_chain(self, org: m.Organization, chain: list[str]) -> m.OrganizationalUnit | None:
        """Crea/reutiliza la cadena de unidades (de específica a general)."""
        parent: m.OrganizationalUnit | None = None
        # Se procesa de la más general a la más específica.
        for name in reversed(chain):
            normalized = normalize_org_name(name)
            row = self._s.execute(
                select(m.OrganizationalUnit).where(
                    m.OrganizationalUnit.organization_id == org.id,
                    m.OrganizationalUnit.parent_unit_id == (parent.id if parent else None),
                    m.OrganizationalUnit.name_normalized == normalized,
                )
            ).scalar_one_or_none()
            if row is None:
                row = m.OrganizationalUnit(
                    organization_id=org.id,
                    parent_unit_id=parent.id if parent else None,
                    preferred_name=name,
                    name_normalized=normalized,
                )
                self._s.add(row)
                self._s.flush()
            parent = row
        return parent

    def _position(
        self,
        label_raw: str,
        org: m.Organization | None,
        unit: m.OrganizationalUnit | None,
        slot: tuple[str, str] | None,
    ) -> m.Position:
        normalized = normalize_position_label(label_raw)
        query = select(m.Position).where(m.Position.label_normalized == normalized)
        query = query.where(m.Position.organization_id == (org.id if org else None))
        query = query.where(m.Position.organizational_unit_id == (unit.id if unit else None))
        row = self._s.execute(query).scalar_one_or_none()
        if row is None:
            row = m.Position(
                organization_id=org.id if org else None,
                organizational_unit_id=unit.id if unit else None,
                preferred_label=label_raw,
                label_normalized=normalized,
            )
            self._s.add(row)
            self._s.flush()
            if org is None:
                self._task(
                    e.ReviewTaskType.POSITION_ORG_UNRESOLVED,
                    "position",
                    row.id,
                    f"El puesto '{label_raw}' no tiene organización determinable desde el "
                    f"texto; resolver manualmente (no se infiere, regla 21)",
                )
        if slot is not None:
            existing_slot = self._s.execute(
                select(m.PositionSlot).where(
                    m.PositionSlot.position_id == row.id,
                    m.PositionSlot.external_scheme == slot[0],
                    m.PositionSlot.external_code == slot[1],
                )
            ).scalar_one_or_none()
            if existing_slot is None:
                self._s.add(
                    m.PositionSlot(
                        position_id=row.id,
                        external_scheme=slot[0],
                        external_code=slot[1],
                        source_document_id=self._doc.id,
                    )
                )
                self._s.flush()
        return row

    # -- persistencia principal ----------------------------------------

    def persist(self, result: ExtractionResult) -> dict[str, list[str]]:
        event_ids: list[str] = []
        assignment_ids: list[str] = []

        mandates: dict[str, m.Mandate] = {}

        for extracted in result.events:
            event, ras = self._persist_event(extracted, mandates)
            event_ids.append(event.id)
            assignment_ids.extend(ra.id for ra in ras)

        for cls in result.article_classifications:
            evidence = self._evidence(cls.evidence)
            self._assert_(
                "legal_document",
                self._doc.id,
                "article_classification",
                evidence,
                object_value={
                    "article_label": cls.article_label,
                    "article_class": str(cls.article_class),
                },
                confidence=cls.confidence,
            )

        for ref in result.references:
            evidence = self._evidence(ref.evidence)
            self._s.add(
                m.DocumentReference(
                    source_document_id=self._doc.id,
                    reference_type=ref.reference_type,
                    target_number_raw=ref.target_number_raw,
                    target_doc_kind_raw=ref.target_doc_kind_raw,
                    evidence_span_id=evidence.id,
                )
            )

        for sig in result.signatories:
            evidence = self._evidence(sig.person.evidence)
            # La capacidad con que se firma ("Presidenta de la República") es la
            # señal corroborante que permite reutilizar precedentes de identidad.
            mention = self._person_mention(
                sig.person.text_raw, evidence, sig.capacity_raw, sig.person.identifiers
            )
            self._s.add(
                m.Signatory(
                    legal_document_id=self._doc.id,
                    person_mention_id=mention.id,
                    capacity_raw=sig.capacity_raw,
                    signature_order=sig.signature_order,
                )
            )
        self._s.flush()
        return {"events": event_ids, "assignments": assignment_ids, "tasks": self._task_ids}

    def _persist_event(
        self, extracted: ExtractedEvent, mandates: dict[str, m.Mandate]
    ) -> tuple[m.PersonnelEvent, list[m.RoleAssignment]]:
        evidence = self._evidence(extracted.evidence)
        event = m.PersonnelEvent(
            legal_document_id=self._doc.id,
            event_type=extracted.event_type,
            assignment_effect=extracted.assignment_effect,
            legal_verb_raw=extracted.legal_verb_raw,
            effective_from=extracted.effective_from.value,
            effective_from_status=extracted.effective_from.status,
            effective_to=extracted.effective_to.value,
            effective_to_status=extracted.effective_to.status,
            end_condition_text=extracted.end_condition_text,
            evidence_span_id=evidence.id,
        )
        self._s.add(event)
        self._s.flush()

        self._assert_(
            "legal_document",
            self._doc.id,
            "declares_personnel_event",
            evidence,
            object_type="personnel_event",
            object_id=event.id,
            object_value={"event_type": str(extracted.event_type)},
            confidence=extracted.confidence,
        )

        # Quinta señal: la fecha que el documento no expresa puede estar fijada
        # por norma (Ley 27594 art. 6 y concordantes). Si la regla la determina,
        # queda registrada con su fundamento y no hay nada que preguntar; si la
        # veta —o no cubre este tipo de acto— sigue el camino de siempre.
        legal_verdict = verdict_for_event(self._s, event)
        if legal_verdict.determined:
            evidence_span = self._publication_date_evidence()
            if evidence_span is None:
                legal_verdict = LegalEffectVerdict(
                    LegalEffectOutcome.VETOED,
                    "la captura no declara la fecha de publicación en un fragmento citable; "
                    "sin evidencia no se registra la fecha determinada (regla 2)",
                    basis=legal_verdict.basis,
                )
            else:
                record_assertion(self._s, event, legal_verdict, evidence_span, self._run.id)

        if (
            extracted.effective_from.status == e.DateStatus.NOT_STATED
            and extracted.assignment_effect == e.AssignmentEffect.START
            and not legal_verdict.determined
        ):
            # Un documento que difiere su vigencia sí dice cuándo empieza a
            # producir efectos; lo que no hace es dar una fecha que estos datos
            # permitan calcular. Decirle al revisor que "no expresa la fecha"
            # cuando el artículo la expresa lo manda a buscar lo que ya tiene.
            if legal_verdict.deferral is not None:
                reason = (
                    "El documento difiere el inicio de efectos a un momento que estos "
                    f"datos no permiten fechar: {legal_verdict.rationale}. "
                    "`effective_from` queda NOT_STATED (no se infiere de la fecha de "
                    "publicación, regla 12)."
                )
            else:
                reason = (
                    "El documento no expresa fecha efectiva de inicio; queda NOT_STATED "
                    "(no se infiere de la fecha de publicación, regla 12). "
                    f"La fecha tampoco quedó determinada por norma: {legal_verdict.rationale}"
                )
            self._task(
                e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
                "personnel_event",
                event.id,
                reason,
                priority=4,
            )

        if extracted.prior_document_number_raw:
            existing = (
                self._s.execute(
                    select(m.DocumentReference).where(
                        m.DocumentReference.source_document_id == self._doc.id,
                        m.DocumentReference.reference_type == e.ReferenceType.PRIOR_APPOINTMENT,
                    )
                )
                .scalars()
                .all()
            )
            already = any(
                r.target_number_raw in extracted.prior_document_number_raw for r in existing
            )
            if not already:
                self._s.add(
                    m.DocumentReference(
                        source_document_id=self._doc.id,
                        reference_type=e.ReferenceType.PRIOR_APPOINTMENT,
                        target_number_raw=extracted.prior_document_number_raw,
                        evidence_span_id=evidence.id,
                    )
                )

        mandate: m.Mandate | None = None
        if extracted.mandate_hint:
            mandate = mandates.get(extracted.mandate_hint)
            if mandate is None:
                if "constitucional" in extracted.mandate_hint:
                    mandate = m.Mandate(
                        mandate_type=e.MandateType.CONSTITUTIONAL_PERIOD,
                        label="Período constitucional del Presidente de la República",
                        end_condition_text=extracted.mandate_hint,
                    )
                else:
                    mandate = m.Mandate(
                        mandate_type=e.MandateType.INSTITUTIONAL_PERIOD,
                        label="Período institucional del cargo (continúa el del antecesor)",
                        end_condition_text=extracted.mandate_hint,
                    )
                self._s.add(mandate)
                self._s.flush()
                mandates[extracted.mandate_hint] = mandate

        # Corroboración por recital (cuarta señal): si el considerando declara
        # quién ejercía exactamente el puesto que este artículo concluye, la
        # atribución se verifica mecánicamente; si hay contradicción, conflicto.
        recital_verdict = None
        recital_participants = [
            pt
            for pt in extracted.participants
            if pt.role == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE
            and pt.person is not None
        ]
        if recital_participants and extracted.event_type == e.EventType.END_ACTING_ASSIGNMENT:
            ended_position = (
                extracted.assignments[0].position_label_raw if extracted.assignments else None
            )
            article_numbers = {
                number
                for number in [bare_document_number(extracted.prior_document_number_raw)]
                if number
            }
            recital_verdict = corroborate_recital(
                [
                    RecitalCandidate(
                        name=pt.person.text_raw,
                        encargo_position_raw=pt.encargo_position_raw,
                        cited_document_number=pt.cited_document_number_raw,
                        substantive_role_raw=pt.substantive_role_raw,
                    )
                    for pt in recital_participants
                    if pt.person is not None
                ],
                ended_position,
                article_numbers,
            )

        conflict_open = False
        mention_by_name: dict[str, m.PersonMention] = {}
        for participant in extracted.participants:
            if participant.person is None:
                continue
            p_evidence = self._evidence(participant.person.evidence)
            is_candidate = participant.role == e.ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE
            corroborating_verdict = (
                recital_verdict
                if (
                    is_candidate
                    and recital_verdict is not None
                    and recital_verdict.outcome == RecitalOutcome.CORROBORATED
                )
                else None
            )
            corroborated = corroborating_verdict is not None
            mention = self._person_mention(
                participant.person.text_raw,
                p_evidence,
                role_context_raw=participant.substantive_role_raw if corroborated else None,
                identifiers=participant.person.identifiers,
            )
            mention_by_name[participant.person.text_raw] = mention
            self._s.add(
                m.EventParticipant(
                    event_id=event.id,
                    participant_type="PERSON",
                    person_mention_id=mention.id,
                    role_in_event=(
                        e.ParticipantRole.AFFECTED_PERSON_RECITAL_CORROBORATED
                        if corroborated
                        else participant.role
                    ),
                    confidence=0.9 if corroborated else participant.confidence,
                )
            )
            if not is_candidate:
                continue
            if corroborating_verdict is not None:
                self._assert_(
                    "personnel_event",
                    event.id,
                    "event_affects_person",
                    p_evidence,
                    object_type="person_mention",
                    object_id=mention.id,
                    object_value={
                        "basis": "recital_corroborated",
                        "rule": RULE_VERSION,
                        "rationale": corroborating_verdict.rationale,
                    },
                    confidence=0.9,
                    status=e.ReviewStatus.AUTO_ACCEPTED,
                )
            elif recital_verdict is not None and recital_verdict.outcome == RecitalOutcome.CONFLICT:
                self._assert_(
                    "personnel_event",
                    event.id,
                    "event_affects_person",
                    p_evidence,
                    object_type="person_mention",
                    object_id=mention.id,
                    object_value={"basis": "recital"},
                    confidence=participant.confidence,
                    status=e.ReviewStatus.CANDIDATE,
                )
                if not conflict_open:
                    conflict_open = True
                    self._task(
                        e.ReviewTaskType.EXTRACTION_CONFLICT,
                        "personnel_event",
                        event.id,
                        recital_verdict.rationale,
                        priority=1,
                    )
            else:
                self._assert_(
                    "personnel_event",
                    event.id,
                    "event_affects_person",
                    p_evidence,
                    object_type="person_mention",
                    object_id=mention.id,
                    object_value={"basis": "recital"},
                    confidence=participant.confidence,
                    status=e.ReviewStatus.CANDIDATE,
                )
                self._task(
                    e.ReviewTaskType.LINK_AFFECTED_ASSIGNMENT,
                    "personnel_event",
                    event.id,
                    "El artículo resolutivo no nombra a la persona afectada; el candidato "
                    f"'{participant.person.text_raw}' proviene de los considerandos y "
                    "requiere confirmación humana"
                    + (f" ({recital_verdict.rationale})" if recital_verdict else ""),
                    priority=2,
                )

        assignments: list[m.RoleAssignment] = []
        for extracted_assignment in extracted.assignments:
            org: m.Organization | None = None
            unit: m.OrganizationalUnit | None = None
            if extracted_assignment.org_path and extracted_assignment.org_path.organization_name:
                org = self._organization(extracted_assignment.org_path.organization_name, evidence)
                unit = self._unit_chain(org, extracted_assignment.org_path.unit_chain)
            slot = None
            if extracted_assignment.position_slot:
                slot = (
                    extracted_assignment.position_slot.external_scheme,
                    extracted_assignment.position_slot.external_code,
                )
            position = None
            if extracted_assignment.position_label_raw:
                # Con el órgano resuelto, la etiqueta del puesto es el cargo sin la
                # ruta: la ruta ya vive en organización + unidades, y dejarla dentro
                # de la etiqueta creaba un Position distinto por cada grafía de la
                # misma ruta. Sin órgano se conserva la etiqueta completa: recortar
                # perdería la única seña de a quién pertenece el puesto.
                position_label = extracted_assignment.position_label_raw
                if (
                    org is not None
                    and extracted_assignment.org_path
                    and extracted_assignment.org_path.role_label
                ):
                    position_label = extracted_assignment.org_path.role_label
                position = self._position(position_label, org, unit, slot)

            ra_mention: m.PersonMention | None = None
            if extracted_assignment.person is not None:
                ra_mention = mention_by_name.get(extracted_assignment.person.text_raw)
                if ra_mention is None:
                    a_evidence = self._evidence(extracted_assignment.person.evidence)
                    ra_mention = self._person_mention(
                        extracted_assignment.person.text_raw,
                        a_evidence,
                        identifiers=extracted_assignment.person.identifiers,
                    )
            elif len(mention_by_name) == 1:
                # Evento END sin persona en el artículo: usa el candidato único
                # del recital (corroborado o ya marcado con tarea de revisión).
                ra_mention = next(iter(mention_by_name.values()))
            if ra_mention is None:
                # Sin mención inequívoca no hay asignación que registrar; queda
                # el evento con su tarea (o el conflicto ya abierto si los
                # considerandos declaran más de un encargo).
                if not conflict_open:
                    self._task(
                        e.ReviewTaskType.LINK_AFFECTED_ASSIGNMENT,
                        "personnel_event",
                        event.id,
                        "Evento sin persona identificable en artículo ni considerandos; "
                        "vincular la asignación afectada manualmente",
                        priority=1,
                    )
                continue

            is_end = extracted.assignment_effect == e.AssignmentEffect.END
            ra = m.RoleAssignment(
                person_id=ra_mention.canonical_person_id,
                person_mention_id=ra_mention.id,
                position_id=position.id if position else None,
                position_label_raw=extracted_assignment.position_label_raw,
                organization_id=org.id if org else None,
                organization_path_raw=(
                    extracted_assignment.org_path.path_raw
                    if extracted_assignment.org_path
                    else None
                ),
                assignment_kind=extracted_assignment.assignment_kind,
                valid_from=extracted_assignment.valid_from.value,
                valid_from_status=extracted_assignment.valid_from.status,
                valid_to=extracted_assignment.valid_to.value,
                valid_to_status=extracted_assignment.valid_to.status,
                end_condition_text=extracted_assignment.end_condition_text,
                start_event_id=None if is_end else event.id,
                end_event_id=event.id if is_end else None,
                mandate_id=mandate.id if mandate else None,
            )
            self._s.add(ra)
            self._s.flush()
            assignments.append(ra)

            self._assert_(
                "person_mention",
                ra_mention.id,
                "role_assignment",
                evidence,
                object_type="role_assignment",
                object_id=ra.id,
                object_value={
                    "assignment_kind": str(extracted_assignment.assignment_kind),
                    "valid_from": (
                        extracted_assignment.valid_from.value.isoformat()
                        if extracted_assignment.valid_from.value
                        else None
                    ),
                    "valid_from_status": str(extracted_assignment.valid_from.status),
                    "valid_to": (
                        extracted_assignment.valid_to.value.isoformat()
                        if extracted_assignment.valid_to.value
                        else None
                    ),
                    "valid_to_status": str(extracted_assignment.valid_to.status),
                    "completes_predecessor_period": (
                        extracted_assignment.completes_predecessor_period
                    ),
                },
                confidence=extracted.confidence,
            )
        # La proyección a la asignación va al final, cuando ya existen las filas
        # que la fecha determinada tiene que alcanzar.
        apply_verdict(self._s, event, legal_verdict)
        return event, assignments
