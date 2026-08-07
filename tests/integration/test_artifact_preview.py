"""Previsualizador de la UI de revisión: endpoint de bytes crudos y salvaguardas.

Garantías congeladas aquí:
- Los bytes servidos son exactamente los capturados (CAS inmutable), con ETag
  estable y caché inmutable.
- Toda respuesta HTML de captura viaja con CSP `sandbox` (contenido de terceros
  jamás ejecuta dentro del origin de la app) y todo iframe de captura HTML en la
  UI lleva el atributo `sandbox`.
- Las tareas sobre assertions derivan su documento desde el artefacto.
- El resaltado de evidencia solo aparece cuando el span sigue anclado; si el
  texto derivó, la UI advierte en lugar de señalar un fragmento equivocado.
"""

from __future__ import annotations

from pathlib import Path

from lxml import html as lxml_html
from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "elperuano"

CODE = "2540905-4"  # Caso D: Tribunal Fiscal, origen de la solicitud de preview


def _version_for(session, code: str) -> m.ArtifactVersion:
    return session.execute(
        select(m.ArtifactVersion)
        .join(m.Artifact, m.Artifact.id == m.ArtifactVersion.artifact_id)
        .join(m.PublicationItem, m.PublicationItem.id == m.Artifact.publication_item_id)
        .where(m.PublicationItem.publication_code == code)
    ).scalar_one()


def test_raw_bytes_are_the_exact_capture(api_client, ingested_session):
    version = _version_for(ingested_session, CODE)
    response = api_client.get(f"/review/artifacts/{version.id}/raw")
    assert response.status_code == 200
    assert response.content == (FIXTURES / f"{CODE}.html").read_bytes()
    assert response.headers["etag"] == f'"{version.sha256}"'
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers.get("content-security-policy", "")
    assert "sandbox" in csp, "la captura HTML debe servirse con CSP sandbox"
    assert "default-src 'none'" in csp, "la captura no debe cargar recursos remotos"


def test_raw_replies_304_when_etag_matches(api_client, ingested_session):
    version = _version_for(ingested_session, CODE)
    etag = f'"{version.sha256}"'
    response = api_client.get(
        f"/review/artifacts/{version.id}/raw", headers={"If-None-Match": etag}
    )
    assert response.status_code == 304
    assert response.headers["etag"] == etag


def test_raw_404_for_unknown_version(api_client):
    response = api_client.get("/review/artifacts/no-existe/raw")
    assert response.status_code == 404


def test_every_pending_task_page_sandboxes_its_html_preview(api_client, ingested_session):
    tasks = (
        ingested_session.execute(
            select(m.ReviewTask).where(m.ReviewTask.status == e.ReviewTaskStatus.PENDING)
        )
        .scalars()
        .all()
    )
    assert tasks, "el corpus debe generar tareas de revisión"
    pages_with_preview = 0
    for task in tasks:
        response = api_client.get(f"/review/tasks/{task.id}")
        assert response.status_code == 200
        tree = lxml_html.fromstring(response.text)
        for iframe in tree.xpath("//iframe"):
            src = iframe.get("src", "")
            if "/review/artifacts/" not in src:
                continue
            pages_with_preview += 1
            if iframe.get("title") == "PDF original":
                # Único caso sin sandbox: el atributo bloquearía el visor PDF
                # nativo. El PDF no es documento HTML activo dentro del origin.
                continue
            assert iframe.get("sandbox") == "", (
                f"iframe de captura HTML sin sandbox en la tarea {task.id}"
            )
    assert pages_with_preview, "ninguna tarea mostró previsualización de captura"


def test_preview_anchors_to_the_device_container(api_client, ingested_session):
    """La captura puede contener otros dispositivos: el iframe ancla a div#x<código>.

    El caso D ya no abre tareas propias —la identidad la corrobora el recital y
    la fecha la determina la norma—, así que se crea una sintética sobre uno de
    sus eventos para ejercitar la página, igual que en la prueba del resaltado.
    """
    version = _version_for(ingested_session, CODE)
    event = (
        ingested_session.execute(
            select(m.PersonnelEvent)
            .join(m.EvidenceSpan, m.EvidenceSpan.id == m.PersonnelEvent.evidence_span_id)
            .where(m.EvidenceSpan.artifact_version_id == version.id)
        )
        .scalars()
        .first()
    )
    assert event is not None, "el caso D debe producir eventos"
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
        target_type="personnel_event",
        target_id=event.id,
        reason="prueba: anclaje del previsualizador al dispositivo",
        priority=3,
    )
    ingested_session.add(task)
    ingested_session.flush()
    response = api_client.get(f"/review/tasks/{task.id}")
    assert f"/review/artifacts/{version.id}/raw#x{CODE}" in response.text


def test_assertion_task_derives_its_document(api_client, ingested_session):
    assertion = (
        ingested_session.execute(
            select(m.Assertion).where(m.Assertion.evidence_span_id.isnot(None))
        )
        .scalars()
        .first()
    )
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.EXTRACTION_CONFLICT,
        target_type="assertion",
        target_id=assertion.id,
        reason="prueba: documento derivado del artefacto de la evidencia",
        priority=3,
    )
    ingested_session.add(task)
    ingested_session.flush()

    response = api_client.get(f"/review/tasks/{task.id}")
    assert response.status_code == 200
    assert "<h2>Documento</h2>" in response.text, (
        "la tarea sobre assertion debe mostrar el documento derivado"
    )
    assert "/review/artifacts/" in response.text, "y ofrecer la previsualización"


def test_event_task_page_highlights_the_recital_participant(api_client, ingested_session):
    """El caso que motivó el previsualizador: la participante viene de los
    considerandos y su cita debe aparecer resaltada en la página de la tarea.

    Desde la señal de corroboración por recital el caso D ya no abre tarea
    LINK_AFFECTED_ASSIGNMENT (se corrobora solo), así que se crea una tarea
    sintética sobre el mismo evento para ejercitar la página.
    """
    event = ingested_session.execute(
        select(m.PersonnelEvent).where(
            m.PersonnelEvent.event_type == e.EventType.END_ACTING_ASSIGNMENT
        )
    ).scalar_one()
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
        target_type="personnel_event",
        target_id=event.id,
        reason="prueba: página de tarea sobre evento con participante corroborada",
        priority=3,
    )
    ingested_session.add(task)
    ingested_session.flush()

    response = api_client.get(f"/review/tasks/{task.id}")
    assert "<mark>Luisa Ysila Castillo Soto</mark>" in response.text
    assert "AFFECTED_PERSON_RECITAL_CORROBORATED" in response.text


def test_every_pending_task_shows_its_source_capture(api_client, ingested_session):
    """Ninguna tarea puede quedarse sin documento fuente que mirar.

    Las tareas sobre entidades canónicas (un puesto, una organización) no tienen
    EvidenceSpan propio y mostraban «Esta tarea no tiene un artefacto capturado
    que previsualizar»: se decidía a ciegas. La captura se toma prestada de la
    mención que originó la ficha.
    """
    tasks = (
        ingested_session.execute(
            select(m.ReviewTask).where(m.ReviewTask.status == e.ReviewTaskStatus.PENDING)
        )
        .scalars()
        .all()
    )
    sin_captura = []
    sin_procedencia = []
    for task in tasks:
        response = api_client.get(f"/review/tasks/{task.id}")
        assert response.status_code == 200
        etiqueta = f"{task.task_type.value} sobre {task.target_type} ({task.id})"
        if "/review/artifacts/" not in response.text:
            sin_captura.append(etiqueta)
        # La captura es la evidencia, pero sin el enlace al original el revisor no
        # puede contrastarla ni citar de dónde salió.
        if "busquedas.elperuano.pe/dispositivo/" not in response.text:
            sin_procedencia.append(etiqueta)
    assert not sin_captura, "tareas sin previsualización de la captura: " + "; ".join(sin_captura)
    assert not sin_procedencia, "tareas sin enlace a la fuente: " + "; ".join(sin_procedencia)


def test_every_task_page_offers_the_pdf_as_file_and_as_viewer(api_client, ingested_session):
    """Dos enlaces distintos, porque son dos cosas distintas.

    `…/<código>/pdf` es la página del visor y devuelve HTML; el archivo vive en
    la URL que declara el payload. Confundirlas hizo que se archivara una página
    web creyendo que era el PDF de respaldo, así que la UI las nombra por lo que
    cada una hace.
    """
    tasks = (
        ingested_session.execute(
            select(m.ReviewTask).where(m.ReviewTask.status == e.ReviewTaskStatus.PENDING)
        )
        .scalars()
        .all()
    )
    incompletas = []
    for task in tasks:
        text = api_client.get(f"/review/tasks/{task.id}").text
        if "Ver el PDF en la fuente" not in text or "Descargar el PDF" not in text:
            incompletas.append(f"{task.task_type.value} ({task.id})")
        if ".PDF" not in text:
            incompletas.append(f"{task.task_type.value} ({task.id}): sin URL de archivo")
    assert not incompletas, "tareas sin los dos enlaces al PDF: " + "; ".join(incompletas)


def test_position_task_borrows_evidence_from_its_origin_mention(api_client, ingested_session):
    """Caso C: el puesto de la SUNAT no tiene organización determinable.

    Para resolverlo hay que leer cómo nombró el documento a la entidad, así que
    la página debe traer la mención de origen, su cita y la captura.
    """
    task = ingested_session.execute(
        select(m.ReviewTask).where(
            m.ReviewTask.task_type == e.ReviewTaskType.POSITION_ORG_UNRESOLVED
        )
    ).scalar_one()
    position = ingested_session.get(m.Position, task.target_id)
    assert position.organization_id is None

    response = api_client.get(f"/review/tasks/{task.id}")
    assert response.status_code == 200
    assert "De dónde procede este puesto" in response.text
    assert "<h2>Documento</h2>" in response.text, "debe derivar el documento de la mención"
    assert "/review/artifacts/" in response.text, "y previsualizar su captura"
    assert "<mark>" in response.text, "con la cita de la mención resaltada"


def test_organization_task_lists_the_mentions_that_name_it(api_client, ingested_session):
    org = ingested_session.execute(select(m.Organization)).scalars().first()
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.ORG_VARIANT_CHECK,
        target_type="organization",
        target_id=org.id,
        reason="prueba: tarea sobre organización canónica",
        priority=3,
    )
    ingested_session.add(task)
    ingested_session.flush()

    response = api_client.get(f"/review/tasks/{task.id}")
    assert response.status_code == 200
    assert org.preferred_name in response.text
    assert "De dónde procede esta organización" in response.text
    assert "/review/artifacts/" in response.text


def test_organization_mention_task_shows_its_evidence(api_client, ingested_session):
    mention = (
        ingested_session.execute(
            select(m.OrganizationMention).where(m.OrganizationMention.evidence_span_id.isnot(None))
        )
        .scalars()
        .first()
    )
    task = m.ReviewTask(
        task_type=e.ReviewTaskType.ENTITY_RESOLUTION,
        target_type="organization_mention",
        target_id=mention.id,
        reason="prueba: tarea sobre mención de organización",
        priority=3,
    )
    ingested_session.add(task)
    ingested_session.flush()

    response = api_client.get(f"/review/tasks/{task.id}")
    assert response.status_code == 200
    assert "<h2>Mención de organización</h2>" in response.text
    assert "/review/artifacts/" in response.text


def test_broken_span_warns_instead_of_highlighting(api_client, ingested_session):
    task = (
        ingested_session.execute(
            select(m.ReviewTask).where(
                m.ReviewTask.target_type == "person_mention",
                m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
            )
        )
        .scalars()
        .first()
    )
    mention = ingested_session.get(m.PersonMention, task.target_id)
    span = ingested_session.get(m.EvidenceSpan, mention.evidence_span_id)
    span.char_start = (span.char_start or 0) + 3  # desplaza el rango: deja de anclar
    ingested_session.flush()

    response = api_client.get(f"/review/tasks/{task.id}")
    assert "<mark>" not in response.text
    assert "Integridad" in response.text
