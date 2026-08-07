"""Integración: fixture → ArtifactStore → parser → extractor → BD → API → revisión."""

from __future__ import annotations

from sqlalchemy import select

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.application.queries import find_position_by_code_or_label
from kipu_knowledge.domain import enums as e

CENEPRED_LABEL = (
    "Jefe del Centro Nacional de Estimación, Prevención y Reducción del Riesgo de "
    "Desastres - CENEPRED"
)


class TestDocumentEndpoints:
    def test_health_and_ready(self, api_client):
        assert api_client.get("/health").json()["status"] == "ok"
        assert api_client.get("/ready").json()["status"] == "ready"

    def test_document_by_source_full_payload(self, api_client):
        # Criterio de aceptación 5: documento, evento, persona, puesto, ruta y evidencia.
        response = api_client.get("/v1/documents/by-source/NL/2540861-1")
        assert response.status_code == 200
        doc = response.json()
        assert doc["number_normalized"] == "D000284-2026-MIDAGRI-DM"
        assert doc["source_url"].endswith("/dispositivo/NL/2540861-1")
        assert doc["published_on"] == "2026-08-06"
        assert doc["artifact_sha256"] and len(doc["artifact_sha256"]) == 64

        event = doc["events"][0]
        assert event["event_type"] == "DESIGNATION"
        # Criterio 6: effective_from null y NOT_STATED.
        assert event["effective_from"] is None
        assert event["effective_from_status"] == "NOT_STATED"
        assert event["participants"][0]["person_mention"] == "ERMELINDA GARCES PINTADO"

        assignment = event["assignments"][0]
        assert assignment["position_label_raw"].startswith("Jefa de Atención al Ciudadano")
        assert assignment["organization_path_raw"].endswith(
            "Ministerio de Desarrollo Agrario y Riego"
        )
        assert "Artículo Único" in event["evidence"]["article_label"]
        assert "ERMELINDA GARCES PINTADO" in event["evidence"]["quoted_text"]
        assert event["parser_version"].startswith("parser/")
        assert event["ontology_version"]

        # Incertidumbre explícita en la respuesta.
        kinds = [u["kind"] for u in doc["uncertainty"]]
        assert "effective_from_not_stated" in kinds

    def test_document_listing(self, api_client):
        data = api_client.get("/v1/documents").json()
        assert data["count"] == 9

    def test_unknown_document_404(self, api_client):
        assert api_client.get("/v1/documents/by-source/NL/0000000-0").status_code == 404


class TestEventAndPersonEndpoints:
    def test_events_filter(self, api_client):
        data = api_client.get("/v1/events", params={"event_type": "ACCEPT_RESIGNATION"}).json()
        assert data["count"] == 2  # CENEPRED y SUNAT

    def test_person_assignments(self, api_client, ingested_session):
        mention = ingested_session.execute(
            select(m.PersonMention).where(
                m.PersonMention.text_normalized == "ERMELINDA GARCES PINTADO"
            )
        ).scalar_one()
        person_id = mention.canonical_person_id
        data = api_client.get(f"/v1/persons/{person_id}/assignments").json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["valid_from"] is None
        assert item["valid_from_status"] == "NOT_STATED"


class TestCeneprexTimeline:
    def test_holder_on_gap_date_is_vacant_by_law(self, api_client, ingested_session):
        """Ejemplo obligatorio de la sección 13, ahora con la fecha que fija la ley.

        El jefe anterior cesó el 2026-08-04 con fecha expresa y la designación
        sucesora, publicada el 2026-08-06, surte efectos ese día por el artículo 6
        de la Ley N.º 27594. El 2026-08-05 el puesto estaba vacante: es una
        respuesta determinada, no una fecha inventada, y la respuesta dice con qué
        base se sostiene. Lo que sigue prohibido es rellenar `valid_from`.
        """
        position = find_position_by_code_or_label(ingested_session, CENEPRED_LABEL)
        assert position is not None
        response = api_client.get(
            f"/v1/positions/{position.id}/timeline", params={"on": "2026-08-05"}
        )
        holder_at = response.json()["holder_at"]
        assert holder_at["status"] == "vacant"
        assert holder_at["holder"] is None
        assert holder_at["basis"] == "legal_rule"
        supporting = holder_at["supporting_evidence"]
        by_person = {s["person_mention"]: s for s in supporting}
        # Asignación anterior: final explícito 2026-08-04.
        prev = by_person["CARLOS MANUEL YAÑEZ LAZO"]
        assert prev["valid_to"] == "2026-08-04"
        assert prev["valid_to_status"] == "EXPLICIT"
        assert prev["effective_end_basis"] == "source"
        # Designación sucesora: el documento sigue sin expresar el inicio; la
        # fecha con la que se responde viene de la norma y va marcada como tal.
        succ = by_person["MIGUEL YAMASAKI KOIZUMI"]
        assert succ["valid_from"] is None
        assert succ["valid_from_status"] == "NOT_STATED"
        assert succ["legal_effect_from"] == "2026-08-06"
        assert succ["effective_start"] == "2026-08-06"
        assert succ["effective_start_basis"] == "legal_rule"
        assert succ["document_id"] is not None

    def test_holder_on_the_day_the_designation_takes_effect(self, api_client, ingested_session):
        """El mismo día de la publicación ya hay titular: «a partir del día de su
        publicación» incluye ese día, no el siguiente."""
        position = find_position_by_code_or_label(ingested_session, CENEPRED_LABEL)
        holder_at = api_client.get(
            f"/v1/positions/{position.id}/timeline", params={"on": "2026-08-06"}
        ).json()["holder_at"]
        assert holder_at["status"] == "confirmed"
        assert holder_at["basis"] == "legal_rule"
        assert holder_at["holder"]["person_mention"] == "MIGUEL YAMASAKI KOIZUMI"

    def test_timeline_lists_both_assignments(self, api_client, ingested_session):
        position = find_position_by_code_or_label(ingested_session, CENEPRED_LABEL)
        data = api_client.get(f"/v1/positions/{position.id}/timeline").json()
        assert len(data["assignments"]) == 2


class TestSearchAndExports:
    def test_search_finds_document(self, api_client):
        data = api_client.get("/v1/search", params={"q": "GARCES PINTADO"}).json()
        codes = [item["publication_code"] for item in data["items"]]
        assert "2540861-1" in codes

    def test_export_ttl(self, api_client, ingested_session):
        doc = _doc(ingested_session, "2540861-1")
        response = api_client.get(f"/v1/exports/documents/{doc.id}.ttl")
        assert response.status_code == 200
        assert "text/turtle" in response.headers["content-type"]
        assert "PersonnelEvent" in response.text
        assert "D000284-2026-MIDAGRI-DM" in response.text

    def test_export_jsonld(self, api_client, ingested_session):
        doc = _doc(ingested_session, "2540861-1")
        response = api_client.get(f"/v1/exports/documents/{doc.id}.jsonld")
        assert response.status_code == 200
        assert "ld+json" in response.headers["content-type"]
        assert "ERMELINDA GARCES PINTADO" in response.text


class TestPersonAliases:
    """Leer no es vincular: las grafías sirven para encontrar a la persona.

    Buscar «Elmer Cuba Bustinza» y no hallarla porque la ficha quedó registrada
    como «Elmer Rafael Cuba Bustinza» es un problema de recall, no de identidad.
    """

    SHORT = "ELMER CUBA BUSTINZA"
    LONG = "ELMER RAFAEL CUBA BUSTINZA"

    def test_person_payload_lists_its_graphies(self, api_client, ingested_session):
        person_id = (
            ingested_session.execute(
                select(m.PersonMention).where(m.PersonMention.text_normalized == self.LONG)
            )
            .scalars()
            .first()
            .canonical_person_id
        )
        data = api_client.get(f"/v1/persons/{person_id}").json()
        aliases = {a["name_normalized"]: a for a in data["aliases"]}
        assert self.LONG in aliases
        assert aliases[self.LONG]["mentions"] == 2  # SUNAT y Tribunal Fiscal
        # Sin decisión humana todavía, ninguna grafía está confirmada.
        assert not any(a["confirmed_by_precedent"] for a in data["aliases"])

    def test_lookup_by_any_graphy_finds_the_person(self, api_client, ingested_session):
        for name in (self.SHORT, self.LONG, "elmer rafael cuba bustinza"):
            items = api_client.get("/v1/persons", params={"name": name}).json()["items"]
            assert items, f"la grafía {name!r} no encontró a nadie"

    def test_lookup_resolves_a_merged_person_to_the_survivor(self, api_client, ingested_session):
        """Tras fusionar, buscar por la grafía absorbida lleva a la ficha vigente."""
        short_mention = (
            ingested_session.execute(
                select(m.PersonMention).where(m.PersonMention.text_normalized == self.SHORT)
            )
            .scalars()
            .first()
        )
        survivor_id = (
            ingested_session.execute(
                select(m.PersonMention).where(m.PersonMention.text_normalized == self.LONG)
            )
            .scalars()
            .first()
            .canonical_person_id
        )
        task = next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["target_id"] == short_mention.id and t["task_type"] == "PERSON_VARIANT_CHECK"
        )
        api_client.post(
            f"/v1/review-tasks/{task['id']}/decisions",
            json={
                "action": "LINK_ENTITY",
                "reviewer": "prueba@example.org",
                "payload": {"entity_id": survivor_id, "scope": "global"},
            },
        )
        items = api_client.get("/v1/persons", params={"name": self.SHORT}).json()["items"]
        assert [x["id"] for x in items] == [survivor_id]
        # Y la ficha ya declara ambas grafías, con la fusionada confirmada.
        aliases = {
            a["name_normalized"]: a
            for a in api_client.get(f"/v1/persons/{survivor_id}").json()["aliases"]
        }
        assert set(aliases) == {self.SHORT, self.LONG}
        assert aliases[self.SHORT]["confirmed_by_precedent"] is True
        assert aliases[self.LONG]["confirmed_by_precedent"] is False

    def test_empty_name_is_rejected(self, api_client):
        assert api_client.get("/v1/persons", params={"name": "  "}).status_code == 422


class TestReviewFormByTaskType:
    """La mesa ofrece solo lo que esa tarea puede hacer, y las fichas se eligen
    por nombre. Antes el formulario era uno solo para los ocho tipos de tarea, así
    que mostraba acciones que fallaban con 422 y pedía identificadores hexadecimales
    que el propio revisor tenía que copiar desde otra zona de la misma página.
    """

    def _page(self, api_client, task_type: str) -> str:
        task = next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["task_type"] == task_type
        )
        return api_client.get(f"/review/tasks/{task['id']}").text

    def _task_id(self, api_client, task_type: str) -> str:
        return next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["task_type"] == task_type
        )["id"]

    def _event_task_page(self, api_client, ingested_session) -> str:
        """Página de una tarea sobre un evento de personal.

        El corpus ya no produce EFFECTIVE_DATE_UNSTATED por sí solo: en los nueve
        casos la fecha o está expresada o la determina la norma. La tarea se crea
        aquí para ejercitar el formulario, que es lo que estas pruebas miran.
        """
        event = ingested_session.execute(select(m.PersonnelEvent)).scalars().first()
        task = m.ReviewTask(
            task_type=e.ReviewTaskType.EFFECTIVE_DATE_UNSTATED,
            target_type="personnel_event",
            target_id=event.id,
            reason="prueba: formulario de una tarea sobre evento",
            priority=4,
        )
        ingested_session.add(task)
        ingested_session.flush()
        return api_client.get(f"/review/tasks/{task.id}").text

    def test_person_task_offers_only_its_applicable_actions(self, api_client):
        page = self._page(api_client, "PERSON_VARIANT_CHECK")
        for allowed in ("LINK_ENTITY", "CREATE_ENTITY", "REJECT", "DISMISS"):
            assert f'value="{allowed}"' in page
        # RESOLVE_POSITION y MARK_DATE_NOT_STATED darían 422 sobre una mención;
        # ACCEPT la cerraría sin dejarla en HUMAN_CONFIRMED; SPLIT_ENTITY no tiene
        # sentido sobre una mención que todavía no se fusionó con nadie.
        for forbidden in ("RESOLVE_POSITION", "MARK_DATE_NOT_STATED", "ACCEPT", "SPLIT_ENTITY"):
            assert f'value="{forbidden}"' not in page

    def test_event_task_offers_only_its_applicable_actions(self, api_client, ingested_session):
        page = self._event_task_page(api_client, ingested_session)
        for allowed in ("APPLY_LEGAL_EFFECT_DATE", "MARK_DATE_NOT_STATED", "ACCEPT", "DISMISS"):
            assert f'value="{allowed}"' in page
        for forbidden in ("LINK_ENTITY", "CREATE_ENTITY", "RESOLVE_POSITION"):
            assert f'value="{forbidden}"' not in page

    def test_position_task_offers_only_its_applicable_actions(self, api_client):
        page = self._page(api_client, "POSITION_ORG_UNRESOLVED")
        assert 'value="RESOLVE_POSITION"' in page
        assert 'value="DISMISS"' in page
        assert 'value="LINK_ENTITY"' not in page

    def test_the_default_action_comes_preselected(self, api_client):
        page = self._page(api_client, "PERSON_VARIANT_CHECK")
        assert 'value="LINK_ENTITY" data-label="Es la misma persona' in page
        assert "checked" in page

    def test_the_ficha_is_chosen_by_name_never_by_identifier(self, api_client):
        page = self._page(api_client, "PERSON_VARIANT_CHECK")
        # Radio con el nombre visible, no un campo de texto donde pegar el hexadecimal.
        assert 'type="radio" name="entity_id"' in page
        assert "ELMER RAFAEL CUBA BUSTINZA" in page
        assert 'type="text" id="entity_id"' not in page
        # El motivo por el que se propone viaja con la opción.
        assert "apellidos idénticos" in page

    def test_unrelated_fields_are_not_rendered(self, api_client):
        """El campo de organización solo existe donde alguna acción lo usa."""
        person_page = self._page(api_client, "PERSON_VARIANT_CHECK")
        assert 'name="organization_id"' not in person_page
        position_page = self._page(api_client, "POSITION_ORG_UNRESOLVED")
        assert 'type="radio" name="organization_id"' in position_page
        assert 'name="entity_id"' not in position_page

    def test_event_task_has_no_identity_controls(self, api_client, ingested_session):
        page = self._event_task_page(api_client, ingested_session)
        assert 'name="entity_id"' not in page
        assert 'name="precedent_scope"' not in page
        assert 'name="preferred_name"' not in page

    def test_inapplicable_action_is_refused_server_side(self, api_client):
        """El formulario ya no la ofrece; un envío a mano tampoco debe pasar."""
        task_id = self._task_id(api_client, "PERSON_VARIANT_CHECK")
        response = api_client.post(
            f"/review/tasks/{task_id}/decide",
            data={"action": "RESOLVE_POSITION", "organization_id": "loquesea"},
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert "no aplica a una tarea sobre person_mention" in response.json()["detail"]

    def test_manual_identifier_overrides_the_selected_ficha(self, api_client, ingested_session):
        """La lista cubre los casos normales; escribir un id sigue siendo posible."""
        task_id = self._task_id(api_client, "PERSON_VARIANT_CHECK")
        mention = ingested_session.get(
            m.PersonMention,
            next(
                t for t in api_client.get("/v1/review-tasks").json()["items"] if t["id"] == task_id
            )["target_id"],
        )
        proposed = (
            SimpleEntityResolver(ingested_session)
            .variant_person_candidates(mention.text_normalized)[0]
            .entity_id
        )
        other = m.Person(preferred_name="FICHA ELEGIDA A MANO")
        ingested_session.add(other)
        ingested_session.flush()
        response = api_client.post(
            f"/review/tasks/{task_id}/decide",
            data={
                "action": "LINK_ENTITY",
                "entity_id": proposed,
                "entity_id_other": other.id,
                "precedent_scope": "none",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        ingested_session.expire_all()
        assert mention.canonical_person_id == other.id

    def test_alias_option_is_disabled_when_the_grafia_is_ambiguous(
        self, api_client, ingested_session
    ):
        """Si la grafía ya designa a dos personas, el alias no debe ni ofrecerse."""
        task_id = self._task_id(api_client, "PERSON_VARIANT_CHECK")
        target_id = next(
            t for t in api_client.get("/v1/review-tasks").json()["items"] if t["id"] == task_id
        )["target_id"]
        mention = ingested_session.get(m.PersonMention, target_id)
        twin = m.Person(preferred_name="HOMONIMO INESPERADO")
        ingested_session.add(twin)
        ingested_session.flush()
        ingested_session.add(
            m.PersonMention(
                legal_document_id=mention.legal_document_id,
                evidence_span_id=mention.evidence_span_id,
                text_raw=mention.text_raw,
                text_normalized=mention.text_normalized,
                canonical_person_id=twin.id,
                resolution_status=e.ResolutionStatus.AUTO_LINKED,
            )
        )
        ingested_session.flush()

        page = api_client.get(f"/review/tasks/{task_id}").text
        assert 'value="global" disabled' in page
        assert "ya corresponde a 2 personas distintas" in page

    def test_the_form_summarises_what_will_happen(self, api_client):
        page = self._page(api_client, "PERSON_VARIANT_CHECK")
        assert "data-summary" in page
        # Sin JavaScript no se oculta nada: el formulario sigue siendo usable.
        assert "data-when" in page


class TestReviewFlow:
    def test_review_tasks_and_decision(self, api_client, ingested_session):
        tasks = api_client.get("/v1/review-tasks").json()["items"]
        assert tasks
        # Los firmantes recurrentes se resuelven por oficio unipersonal; la
        # resolución de identidad que llega a un humano es la de grafías compatibles.
        link_tasks = [t for t in tasks if t["task_type"] == "PERSON_VARIANT_CHECK"]
        assert link_tasks
        task = link_tasks[0]
        mention = ingested_session.get(m.PersonMention, task["target_id"])
        proposals = SimpleEntityResolver(ingested_session).variant_person_candidates(
            mention.text_normalized
        )
        assert proposals
        candidate = ingested_session.get(m.Person, proposals[0].entity_id)
        response = api_client.post(
            f"/v1/review-tasks/{task['id']}/decisions",
            json={
                "action": "LINK_ENTITY",
                "reviewer": "prueba@example.org",
                "payload": {"entity_id": candidate.id},
                "notes": "misma persona con el nombre de pila omitido",
            },
        )
        assert response.status_code == 200
        ingested_session.expire_all()
        assert mention.canonical_person_id == candidate.id
        assert mention.resolution_status == e.ResolutionStatus.HUMAN_CONFIRMED
        # La decisión queda auditada.
        decision = ingested_session.execute(
            select(m.ReviewDecision).where(m.ReviewDecision.review_task_id == task["id"])
        ).scalar_one()
        assert decision.reviewer == "prueba@example.org"

    def test_alias_scope_is_auditable_end_to_end(self, api_client, ingested_session):
        """Decidir con alcance global por API deja el alias visible en la auditoría."""
        tasks = api_client.get("/v1/review-tasks").json()["items"]
        task = next(t for t in tasks if t["task_type"] == "PERSON_VARIANT_CHECK")
        mention = ingested_session.get(m.PersonMention, task["target_id"])
        candidate_id = (
            SimpleEntityResolver(ingested_session)
            .variant_person_candidates(mention.text_normalized)[0]
            .entity_id
        )
        response = api_client.post(
            f"/v1/review-tasks/{task['id']}/decisions",
            json={
                "action": "LINK_ENTITY",
                "reviewer": "prueba@example.org",
                "payload": {"entity_id": candidate_id, "scope": "global"},
            },
        )
        assert response.status_code == 200

        precedents = api_client.get("/v1/identity-precedents").json()["items"]
        alias = next(p for p in precedents if p["name_normalized"] == mention.text_normalized)
        assert alias["scope"] == "global"
        assert alias["role_context"] is None
        assert alias["person_id"] == candidate_id

    def test_unknown_scope_is_rejected_with_422(self, api_client, ingested_session):
        tasks = api_client.get("/v1/review-tasks").json()["items"]
        task = next(t for t in tasks if t["task_type"] == "PERSON_VARIANT_CHECK")
        mention = ingested_session.get(m.PersonMention, task["target_id"])
        candidate_id = (
            SimpleEntityResolver(ingested_session)
            .variant_person_candidates(mention.text_normalized)[0]
            .entity_id
        )
        response = api_client.post(
            f"/v1/review-tasks/{task['id']}/decisions",
            json={
                "action": "LINK_ENTITY",
                "payload": {"entity_id": candidate_id, "scope": "cuando-sea"},
            },
        )
        assert response.status_code == 422
        assert "Alcance de precedente no soportado" in response.json()["detail"]
        # La tarea sigue pendiente: un alcance inválido no resuelve nada.
        still_pending = [
            t for t in api_client.get("/v1/review-tasks").json()["items"] if t["id"] == task["id"]
        ]
        assert still_pending and still_pending[0]["status"] == "PENDING"

    def test_review_ui_pages_render(self, api_client):
        assert api_client.get("/review").status_code == 200
        tasks = api_client.get("/v1/review-tasks").json()["items"]
        if tasks:
            assert api_client.get(f"/review/tasks/{tasks[0]['id']}").status_code == 200
        assert api_client.get("/review/history").status_code == 200

    def test_variant_task_page_offers_the_scope_control(self, api_client):
        """El alcance del precedente debe poder elegirse desde la mesa, no solo por API."""
        task = next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["task_type"] == "PERSON_VARIANT_CHECK"
        )
        page = api_client.get(f"/review/tasks/{task['id']}").text
        assert 'name="precedent_scope"' in page
        assert 'value="global"' in page and 'value="office"' in page and 'value="none"' in page
        # Grafía discriminante: el alias no debe aparecer deshabilitado.
        assert 'value="global" disabled' not in page

    def test_scope_chosen_in_the_ui_reaches_the_precedent(self, api_client, ingested_session):
        task = next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["task_type"] == "PERSON_VARIANT_CHECK"
        )
        mention = ingested_session.get(m.PersonMention, task["target_id"])
        candidate_id = (
            SimpleEntityResolver(ingested_session)
            .variant_person_candidates(mention.text_normalized)[0]
            .entity_id
        )
        response = api_client.post(
            f"/review/tasks/{task['id']}/decide",
            data={
                "action": "LINK_ENTITY",
                "reviewer": "mesa@example.org",
                "entity_id": candidate_id,
                "precedent_scope": "global",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        precedent = ingested_session.execute(select(m.IdentityPrecedent)).scalars().one()
        assert precedent.role_context is None
        assert precedent.reviewer == "mesa@example.org"

    def test_ui_can_decline_to_set_a_precedent(self, api_client, ingested_session):
        task = next(
            t
            for t in api_client.get("/v1/review-tasks").json()["items"]
            if t["task_type"] == "PERSON_VARIANT_CHECK"
        )
        mention = ingested_session.get(m.PersonMention, task["target_id"])
        candidate_id = (
            SimpleEntityResolver(ingested_session)
            .variant_person_candidates(mention.text_normalized)[0]
            .entity_id
        )
        api_client.post(
            f"/review/tasks/{task['id']}/decide",
            data={
                "action": "LINK_ENTITY",
                "entity_id": candidate_id,
                "precedent_scope": "none",
            },
            follow_redirects=False,
        )
        assert not ingested_session.execute(select(m.IdentityPrecedent)).scalars().all()


def _doc(session, code: str) -> m.LegalDocument:
    return session.execute(
        select(m.LegalDocument)
        .join(m.PublicationItem, m.PublicationItem.id == m.LegalDocument.publication_item_id)
        .where(m.PublicationItem.publication_code == code)
    ).scalar_one()
