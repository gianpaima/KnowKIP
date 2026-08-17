"""Servicio de revisión humana.

Cada decisión crea un ReviewDecision auditable; la extracción original nunca se
borra: aceptar/rechazar cambia estados y, si corrige, supersede (regla de
inmutabilidad de la cadena de afirmaciones).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.adapters.resolution.resolver import SimpleEntityResolver
from kipu_knowledge.domain import enums as e

# Alcances de precedente admitidos en el payload de una decisión.
PRECEDENT_SCOPE_OFFICE = "office"
PRECEDENT_SCOPE_GLOBAL = "global"
PRECEDENT_SCOPES = (PRECEDENT_SCOPE_OFFICE, PRECEDENT_SCOPE_GLOBAL)


class ReviewError(ValueError):
    pass


class ReviewService:
    def __init__(self, session: Session) -> None:
        self._s = session

    # -- API de repositorio de afirmaciones (contrato AssertionRepository) --

    def accept(self, assertion_id: str, reviewer: str | None = None) -> None:
        assertion = self._require_assertion(assertion_id)
        assertion.review_status = e.ReviewStatus.HUMAN_ACCEPTED

    def reject(self, assertion_id: str, reviewer: str | None = None) -> None:
        assertion = self._require_assertion(assertion_id)
        assertion.review_status = e.ReviewStatus.HUMAN_REJECTED

    def supersede(self, assertion_id: str, replacement_id: str) -> None:
        assertion = self._require_assertion(assertion_id)
        replacement = self._require_assertion(replacement_id)
        assertion.review_status = e.ReviewStatus.SUPERSEDED
        assertion.superseded_at = datetime.now(UTC)
        assertion.superseded_by_id = replacement.id

    def save_candidates(self, assertion_ids: list[str]) -> None:
        for assertion_id in assertion_ids:
            assertion = self._require_assertion(assertion_id)
            assertion.review_status = e.ReviewStatus.CANDIDATE

    def _require_assertion(self, assertion_id: str) -> m.Assertion:
        assertion = self._s.get(m.Assertion, assertion_id)
        if assertion is None:
            raise ReviewError(f"Assertion inexistente: {assertion_id}")
        return assertion

    # -- decisiones sobre tareas ------------------------------------------

    def decide(
        self,
        task_id: str,
        action: e.DecisionAction,
        reviewer: str | None = None,
        payload: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> m.ReviewDecision:
        task = self._s.get(m.ReviewTask, task_id)
        if task is None:
            raise ReviewError(f"Tarea inexistente: {task_id}")
        if task.status != e.ReviewTaskStatus.PENDING:
            raise ReviewError(f"La tarea {task_id} ya fue resuelta")

        payload = payload or {}
        handler = {
            e.DecisionAction.ACCEPT: self._handle_accept,
            e.DecisionAction.REJECT: self._handle_reject,
            e.DecisionAction.LINK_ENTITY: self._handle_link_entity,
            e.DecisionAction.CREATE_ENTITY: self._handle_create_entity,
            e.DecisionAction.SPLIT_ENTITY: self._handle_split_entity,
            e.DecisionAction.MARK_DATE_NOT_STATED: self._handle_mark_not_stated,
            e.DecisionAction.APPLY_LEGAL_EFFECT_DATE: self._handle_apply_legal_effect,
            e.DecisionAction.SET_LEGAL_EFFECT_DATE: self._handle_set_legal_effect,
            e.DecisionAction.RESOLVE_POSITION: self._handle_resolve_position,
            e.DecisionAction.DISMISS: self._handle_dismiss,
        }.get(action)
        if handler is None:
            raise ReviewError(f"Acción no soportada: {action}")

        # La decisión se registra antes de ejecutar el manejador para que un
        # IdentityPrecedent derivado pueda citarla como origen.
        decision = m.ReviewDecision(
            review_task_id=task.id,
            action=action,
            reviewer=reviewer,
            payload=payload or None,
            notes=notes,
        )
        self._s.add(decision)
        self._s.flush()
        handler(task, payload, decision)

        task.status = (
            e.ReviewTaskStatus.DISMISSED
            if action == e.DecisionAction.DISMISS
            else e.ReviewTaskStatus.RESOLVED
        )
        task.resolved_at = datetime.now(UTC)
        self._s.flush()
        return decision

    # -- manejadores -------------------------------------------------------

    def _handle_accept(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        if task.target_type == "assertion":
            self.accept(task.target_id)

    def _handle_reject(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        if task.target_type == "assertion":
            self.reject(task.target_id)
        elif task.target_type == "person_mention":
            mention = self._s.get(m.PersonMention, task.target_id)
            if mention is not None:
                mention.resolution_status = e.ResolutionStatus.HUMAN_REJECTED

    def _handle_link_entity(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        entity_id = payload.get("entity_id")
        if not entity_id:
            raise ReviewError("LINK_ENTITY requiere payload.entity_id")
        if task.target_type == "person_mention":
            mention = self._s.get(m.PersonMention, task.target_id)
            person = self._s.get(m.Person, entity_id)
            if mention is None or person is None:
                raise ReviewError("Mención o persona inexistente")
            previous_person_id = mention.canonical_person_id
            mention.canonical_person_id = person.id
            mention.resolution_status = e.ResolutionStatus.HUMAN_CONFIRMED
            for ra in self._s.execute(
                select(m.RoleAssignment).where(m.RoleAssignment.person_mention_id == mention.id)
            ).scalars():
                ra.person_id = person.id
            if previous_person_id and previous_person_id != person.id:
                self._absorb_person(previous_person_id, person.id)
            precedent = self._record_precedent(mention, person.id, decision, payload)
            if precedent is not None:
                self._apply_precedent_to_pending(precedent, decision, exclude_mention_id=mention.id)
        elif task.target_type == "organization_mention":
            mention_org = self._s.get(m.OrganizationMention, task.target_id)
            org = self._s.get(m.Organization, entity_id)
            if mention_org is None or org is None:
                raise ReviewError("Mención u organización inexistente")
            mention_org.canonical_organization_id = org.id
            mention_org.resolution_status = e.ResolutionStatus.HUMAN_CONFIRMED
        else:
            raise ReviewError(f"LINK_ENTITY no aplica a {task.target_type}")

    def _handle_create_entity(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        if task.target_type != "person_mention":
            raise ReviewError("CREATE_ENTITY solo soporta person_mention en el MVP")
        mention = self._s.get(m.PersonMention, task.target_id)
        if mention is None:
            raise ReviewError("Mención inexistente")
        person = m.Person(preferred_name=payload.get("preferred_name") or mention.text_raw)
        self._s.add(person)
        self._s.flush()
        mention.canonical_person_id = person.id
        mention.resolution_status = e.ResolutionStatus.HUMAN_CONFIRMED
        for ra in self._s.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.person_mention_id == mention.id)
        ).scalars():
            ra.person_id = person.id
        precedent = self._record_precedent(mention, person.id, decision, payload)
        if precedent is not None:
            self._apply_precedent_to_pending(precedent, decision, exclude_mention_id=mention.id)

    def _handle_split_entity(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        """Deshace una vinculación incorrecta: la mención vuelve a UNRESOLVED con
        una persona nueva propia; la persona original conserva sus otras menciones."""
        if task.target_type != "person_mention":
            raise ReviewError("SPLIT_ENTITY solo soporta person_mention en el MVP")
        mention = self._s.get(m.PersonMention, task.target_id)
        if mention is None:
            raise ReviewError("Mención inexistente")
        # Si la vinculación vino de un precedente, ese precedente estaba equivocado:
        # revocarlo evita que la próxima ingesta repita el mismo error en silencio.
        self._revoke_precedents_for(mention, payload.get("notes") or "separación humana")
        new_person = m.Person(preferred_name=mention.text_raw)
        self._s.add(new_person)
        self._s.flush()
        mention.canonical_person_id = new_person.id
        mention.resolution_status = e.ResolutionStatus.SPLIT
        mention.identity_precedent_id = None
        for ra in self._s.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.person_mention_id == mention.id)
        ).scalars():
            ra.person_id = new_person.id

    def _handle_mark_not_stated(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        if task.target_type != "personnel_event":
            raise ReviewError("MARK_DATE_NOT_STATED aplica a personnel_event")
        event = self._s.get(m.PersonnelEvent, task.target_id)
        if event is None:
            raise ReviewError("Evento inexistente")
        event.effective_from = None
        event.effective_from_status = e.DateStatus.NOT_STATED

    def _handle_apply_legal_effect(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        """Aplica la fecha de inicio de efectos que la norma determina.

        El revisor no dicta la fecha: confirma que la regla aplica, y el sistema
        la vuelve a derivar de los datos capturados. Si la regla no la determina
        —porque el acto no está cubierto o porque el propio documento posterga su
        vigencia— la acción falla en lugar de escribir una fecha que nadie podría
        justificar después.
        """
        from kipu_knowledge.application import legal_effect as le

        if task.target_type != "personnel_event":
            raise ReviewError("APPLY_LEGAL_EFFECT_DATE aplica a personnel_event")
        event = self._s.get(m.PersonnelEvent, task.target_id)
        if event is None:
            raise ReviewError("Evento inexistente")
        verdict = le.verdict_for_event(self._s, event)
        if not verdict.determined:
            raise ReviewError(f"La norma no determina la fecha de este acto: {verdict.rationale}")

        le.apply_verdict(self._s, event, verdict)
        already = (
            self._s.execute(
                select(m.Assertion).where(
                    m.Assertion.subject_type == "personnel_event",
                    m.Assertion.subject_id == event.id,
                    m.Assertion.predicate == le.PREDICATE,
                    m.Assertion.review_status != e.ReviewStatus.SUPERSEDED,
                )
            )
            .scalars()
            .first()
        )
        if already is not None:
            return
        doc = self._s.get(m.LegalDocument, event.legal_document_id)
        evidence = le.publication_date_span(self._s, doc) if doc is not None else None
        run = le.latest_extraction_run(self._s, doc) if doc is not None else None
        if evidence is None or run is None:
            raise ReviewError(
                "No hay cita registrada de la fecha de publicación para este documento: "
                "ejecuta `kipu apply-legal-effect-dates`, que la reconstruye desde los "
                "bytes del CAS, antes de aplicar la regla desde el panel"
            )
        le.record_assertion(self._s, event, verdict, evidence, run.id)

    def _handle_set_legal_effect(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        """Fija a mano el inicio de efectos que la norma no determina (ADR-0009).

        Existe para los actos que difieren su vigencia a un momento que estos
        datos no permiten fechar: sin esta salida la tarea quedaba abierta y las
        únicas acciones disponibles o fallaban o afirmaban algo falso.

        Se escribe en ``legal_effect_from``, no en ``effective_from``: el
        documento sigue sin expresar una fecha, y borrar esa distinción haría
        indistinguible lo que la fuente dijo de lo que un revisor concluyó. El
        fundamento se marca como decisión humana y cita el ``ReviewDecision``,
        que es el registro auditable — no se crea ``Assertion`` porque no hay
        ``EvidenceSpan`` que citar para un juicio humano.
        """
        from kipu_knowledge.application import legal_effect as le

        if task.target_type != "personnel_event":
            raise ReviewError("SET_LEGAL_EFFECT_DATE aplica a personnel_event")
        event = self._s.get(m.PersonnelEvent, task.target_id)
        if event is None:
            raise ReviewError("Evento inexistente")

        raw = (payload.get("legal_effect_from") or "").strip()
        if not raw:
            raise ReviewError("SET_LEGAL_EFFECT_DATE requiere payload.legal_effect_from")
        try:
            value = date.fromisoformat(raw)
        except ValueError as exc:
            raise ReviewError(f"Fecha no válida: {raw!r} (se espera AAAA-MM-DD)") from exc
        # Una fecha fijada a mano sin motivo escrito no se puede auditar después:
        # el que la lea no sabrá de dónde salió ni podrá contradecirla.
        if not (decision.notes or "").strip():
            raise ReviewError(
                "SET_LEGAL_EFFECT_DATE exige una nota que justifique la fecha: es un "
                "juicio humano y sin el motivo no queda auditable"
            )

        verdict = le.verdict_for_event(self._s, event)
        if verdict.determined:
            raise ReviewError(
                "La norma sí determina la fecha de este acto "
                f"({verdict.value}): usa APPLY_LEGAL_EFFECT_DATE, que la vuelve a "
                "derivar de los datos capturados en lugar de fijarla a mano"
            )

        event.legal_effect_from = value
        event.legal_effect_basis_json = {
            "rule": "human-decision/1.0",
            "outcome": str(le.LegalEffectOutcome.DETERMINED),
            "method": "decisión humana",
            "status": str(e.DateStatus.DERIVED),
            "legal_effect_from": value.isoformat(),
            "rationale": decision.notes,
            "review_decision_id": decision.id,
            "reviewer": decision.reviewer,
            # Por qué la regla no pudo decidirlo: sin esto, una fecha humana es
            # indistinguible de un capricho seis meses después.
            "rule_declined_because": verdict.rationale,
            "deferral_clause": verdict.deferral.text if verdict.deferral else None,
        }
        le.project_to_assignments(self._s, event, value)

    def _handle_resolve_position(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        organization_id = payload.get("organization_id")
        if not organization_id:
            raise ReviewError("RESOLVE_POSITION requiere payload.organization_id")
        position = self._s.get(m.Position, task.target_id)
        org = self._s.get(m.Organization, organization_id)
        if position is None or org is None:
            raise ReviewError("Puesto u organización inexistente")
        position.organization_id = org.id

    def _handle_dismiss(
        self, task: m.ReviewTask, payload: dict[str, Any], decision: m.ReviewDecision
    ) -> None:
        pass

    # -- precedentes de identidad -----------------------------------------

    def _record_precedent(
        self,
        mention: m.PersonMention,
        person_id: str,
        decision: m.ReviewDecision,
        payload: dict[str, Any],
    ) -> m.IdentityPrecedent | None:
        """Persiste la decisión como precedente reutilizable.

        Dos alcances, elegidos con ``scope`` en el payload:

        - ``"office"`` (por defecto): ata el precedente al cargo declarado. Sin
          cargo no se crea nada, porque el nombre por sí solo nunca autoriza
          vincular menciones futuras (regla 13) y esos casos deben seguir yendo a
          revisión una por una.
        - ``"global"``: declara un alias sobre la grafía, aplicable con cualquier
          cargo. Es una afirmación humana, no una inferencia del sistema, así que
          no contradice la regla 13; pero renuncia a la segunda señal, y por eso
          se exige que el nombre sea discriminante. Si ya corresponde a más de una
          persona, el alias vincularía homónimos futuros en silencio: se rechaza.

        El revisor puede desactivarlo del todo con ``create_precedent: false``.
        """
        if payload.get("create_precedent") is False:
            return None
        scope = payload.get("scope") or PRECEDENT_SCOPE_OFFICE
        if scope not in PRECEDENT_SCOPES:
            raise ReviewError(
                f"Alcance de precedente no soportado: {scope!r} "
                f"(admitidos: {', '.join(PRECEDENT_SCOPES)})"
            )

        if scope == PRECEDENT_SCOPE_GLOBAL:
            homonyms = SimpleEntityResolver(self._s).distinct_persons_for_name(
                mention.text_normalized
            )
            if homonyms > 1:
                raise ReviewError(
                    f"'{mention.text_normalized}' ya corresponde a {homonyms} personas "
                    "distintas: un alias sin cargo vincularía homónimos futuros sin "
                    "abrir tarea. Usa alcance por cargo."
                )
            role_context = None
        else:
            role_context = mention.role_context_normalized
            if not role_context:
                return None

        existing = self._s.execute(
            select(m.IdentityPrecedent).where(
                m.IdentityPrecedent.subject_type == "person",
                m.IdentityPrecedent.name_normalized == mention.text_normalized,
                m.IdentityPrecedent.role_context.is_(None)
                if role_context is None
                else m.IdentityPrecedent.role_context == role_context,
                m.IdentityPrecedent.person_id == person_id,
                m.IdentityPrecedent.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        precedent = m.IdentityPrecedent(
            name_normalized=mention.text_normalized,
            role_context=role_context,
            person_id=person_id,
            source_person_mention_id=mention.id,
            review_decision_id=decision.id,
            reviewer=decision.reviewer,
        )
        self._s.add(precedent)
        self._s.flush()
        return precedent

    def _apply_precedent_to_pending(
        self,
        precedent: m.IdentityPrecedent,
        decision: m.ReviewDecision,
        *,
        exclude_mention_id: str,
    ) -> None:
        """Resuelve con el precedente recién sentado las tareas hermanas pendientes.

        Sin esto, el revisor decide el mismo conflicto una vez por documento: el
        precedente solo alcanzaba a las ingestas posteriores, y cada mención
        CANDIDATE_MATCH anterior conservaba su tarea abierta pidiendo exactamente
        la decisión que se acaba de tomar.

        La cobertura la decide `person_precedent`, el mismo camino que usa la
        ingesta: así hereda sus salvaguardas (clave literal, precedentes
        contradictorios → None) y el resultado es idéntico a reingerir el
        documento. Las menciones con un EXTRACTION_CONFLICT pendiente se saltan:
        ahí las señales se contradijeron y el precedente es solo una de ellas,
        así que cerrarlas en silencio elegiría por el humano.

        Cada cierre registra su propio ReviewDecision sin revisor —la acción es
        del sistema— citando el precedente y la decisión humana que lo originó.
        No se crean Assertions: eso exige una corrida de extracción, y la cadena
        mención → precedente → decisión ya deja el vínculo auditable.
        """
        resolver = SimpleEntityResolver(self._s)
        siblings = (
            self._s.execute(
                select(m.PersonMention).where(
                    m.PersonMention.text_normalized == precedent.name_normalized,
                    m.PersonMention.resolution_status == e.ResolutionStatus.CANDIDATE_MATCH,
                    m.PersonMention.id != exclude_mention_id,
                )
            )
            .scalars()
            .all()
        )
        for sibling in siblings:
            tasks = (
                self._s.execute(
                    select(m.ReviewTask).where(
                        m.ReviewTask.target_type == "person_mention",
                        m.ReviewTask.target_id == sibling.id,
                        m.ReviewTask.status == e.ReviewTaskStatus.PENDING,
                    )
                )
                .scalars()
                .all()
            )
            if any(t.task_type == e.ReviewTaskType.EXTRACTION_CONFLICT for t in tasks):
                continue
            applicable = resolver.person_precedent(
                sibling.text_normalized, sibling.role_context_normalized
            )
            if applicable is None or applicable.person_id != precedent.person_id:
                continue
            sibling.canonical_person_id = applicable.person_id
            sibling.resolution_status = e.ResolutionStatus.PRECEDENT_LINKED
            sibling.identity_precedent_id = applicable.id
            for ra in self._s.execute(
                select(m.RoleAssignment).where(m.RoleAssignment.person_mention_id == sibling.id)
            ).scalars():
                ra.person_id = applicable.person_id
            now = datetime.now(UTC)
            for task in tasks:
                self._s.add(
                    m.ReviewDecision(
                        review_task_id=task.id,
                        action=e.DecisionAction.LINK_ENTITY,
                        reviewer=None,
                        payload={
                            "entity_id": applicable.person_id,
                            "identity_precedent_id": applicable.id,
                            "origin_review_decision_id": decision.id,
                        },
                        notes=(
                            f"Resuelta por el precedente {applicable.id} sentado por "
                            f"{decision.reviewer or 'revisor no identificado'} en la "
                            f"decisión {decision.id}"
                        ),
                    )
                )
                task.status = e.ReviewTaskStatus.RESOLVED
                task.resolved_at = now
        self._s.flush()

    def _revoke_precedents_for(self, mention: m.PersonMention, reason: str) -> None:
        """Revoca los precedentes vigentes que aplican a esta mención.

        Incluye el alias sobre la grafía (``role_context`` NULL), no solo el de su
        cargo: si la vinculación era incorrecta, dejar vivo el alias repetiría el
        mismo error en la próxima ingesta, que es justo lo que SPLIT_ENTITY viene
        a impedir. Por eso tampoco se sale temprano cuando la mención no declara
        cargo: un alias puede aplicarle igualmente.
        """
        scopes: list[ColumnElement[bool]] = [m.IdentityPrecedent.role_context.is_(None)]
        if mention.role_context_normalized:
            scopes.append(m.IdentityPrecedent.role_context == mention.role_context_normalized)
        now = datetime.now(UTC)
        for precedent in self._s.execute(
            select(m.IdentityPrecedent).where(
                m.IdentityPrecedent.subject_type == "person",
                m.IdentityPrecedent.name_normalized == mention.text_normalized,
                or_(*scopes),
                m.IdentityPrecedent.revoked_at.is_(None),
            )
        ).scalars():
            precedent.revoked_at = now
            precedent.revoked_reason = reason

    def _absorb_person(self, duplicate_id: str, survivor_id: str) -> None:
        """Marca como fusionada una persona que se quedó sin menciones propias.

        La fila no se borra (regla 3): queda apuntando a la superviviente para que
        cualquier identificador ya publicado siga resolviendo.
        """
        duplicate = self._s.get(m.Person, duplicate_id)
        if duplicate is None or duplicate_id == survivor_id:
            return
        remaining = (
            self._s.execute(
                select(m.PersonMention).where(m.PersonMention.canonical_person_id == duplicate_id)
            )
            .scalars()
            .first()
        )
        if remaining is not None:
            return
        duplicate.status = "MERGED"
        duplicate.merged_into_person_id = survivor_id
        for ra in self._s.execute(
            select(m.RoleAssignment).where(m.RoleAssignment.person_id == duplicate_id)
        ).scalars():
            ra.person_id = survivor_id
