"""Extractor determinista de eventos de personal.

Produce ExtractionResult (Pydantic estricto). Principios:
- Cada hecho lleva evidencia exacta (sección + offsets + cita literal).
- Las fechas solo se emiten si el texto las declara (EXPLICIT); nunca se derivan
  de la fecha de publicación (regla 12).
- Artículos que no calzan con un patrón de personal se clasifican como obligación,
  notificación, refrendo u OTHER, y se emite un warning si quedan sin clasificar.
"""

from __future__ import annotations

from kipu_knowledge import EXTRACTOR_VERSION
from kipu_knowledge.domain.enums import (
    ArticleClass,
    AssignmentEffect,
    AssignmentKind,
    DateStatus,
    EventType,
    IdentifierScheme,
    ParticipantRole,
    ReferenceType,
    SectionType,
)
from kipu_knowledge.domain.extraction_models import (
    ArticleClassification,
    EvidenceRef,
    ExtractedAssignment,
    ExtractedDate,
    ExtractedEvent,
    ExtractedIdentifier,
    ExtractedOrgPath,
    ExtractedParticipant,
    ExtractedPersonMention,
    ExtractedPositionSlot,
    ExtractedReference,
    ExtractedSignatory,
    ExtractionResult,
)
from kipu_knowledge.domain.normalization import parse_spanish_date
from kipu_knowledge.domain.parsed import ParsedDocument, ParsedSection

from . import patterns as p


def _full_span(section: ParsedSection) -> EvidenceRef:
    return EvidenceRef(
        section_index=section.order_index,
        article_label=section.label_raw,
        char_start=0,
        char_end=len(section.text_raw),
        quoted_text=section.text_raw,
    )


def _explicit_date(raw_phrase: str | None) -> ExtractedDate:
    if not raw_phrase:
        return ExtractedDate()
    value = parse_spanish_date(raw_phrase)
    if value is None:
        return ExtractedDate()
    return ExtractedDate(value=value, status=DateStatus.EXPLICIT, source_phrase=raw_phrase)


def _name_span(section: ParsedSection, name: str) -> EvidenceRef:
    """Span exacto del nombre dentro de la sección.

    Permite a la UI de revisión resaltar a la persona, no el artículo entero.
    Si el texto no contiene el nombre de forma literal (espaciado distinto),
    degrada a la sección completa: un span amplio pero correcto vale más que
    uno preciso pero equivocado.
    """
    start = section.text_raw.find(name)
    if start < 0:
        return _full_span(section)
    return EvidenceRef(
        section_index=section.order_index,
        article_label=section.label_raw,
        char_start=start,
        char_end=start + len(name),
        quoted_text=name,
    )


def _mention(section: ParsedSection, name: str) -> ExtractedPersonMention:
    cleaned = name.strip()
    return ExtractedPersonMention(
        text_raw=cleaned,
        evidence=_name_span(section, cleaned),
        identifiers=_identifiers(section, cleaned),
    )


def _identifiers(section: ParsedSection, name: str) -> list[ExtractedIdentifier]:
    """Documentos de identidad que la sección atribuye inequívocamente a `name`."""
    text = section.text_raw
    return [
        ExtractedIdentifier(
            scheme=IdentifierScheme(scheme),
            value_raw=value,
            evidence=EvidenceRef(
                section_index=section.order_index,
                article_label=section.label_raw,
                char_start=start,
                char_end=end,
                quoted_text=text[start:end],
            ),
        )
        for scheme, value, start, end in p.identifiers_for_name(text, name)
    ]


def _org_path(role_text: str) -> ExtractedOrgPath:
    split = p.split_org_path(role_text)
    return ExtractedOrgPath(
        path_raw=role_text,
        organization_name=split.organization,
        unit_chain=split.unit_chain,
    )


def _article_body(section: ParsedSection) -> str:
    text = section.text_raw
    if section.label_raw and text.startswith(section.label_raw):
        text = text[len(section.label_raw) :]
    return text.strip()


class DeterministicExtractor:
    extractor_version = EXTRACTOR_VERSION

    def extract(self, document: ParsedDocument) -> ExtractionResult:
        result = ExtractionResult()
        considerandos = document.sections_of(SectionType.CONSIDERANDO)
        recital_text = " ".join(s.text_normalized for s in considerandos)
        completes_predecessor = p.PREDECESSOR_PERIOD_PHRASE in recital_text
        constitutional_mandate = p.CONSTITUTIONAL_PERIOD_PHRASE in recital_text

        articles = document.articles()
        list_items = self._list_items_by_article(document)

        for article in articles:
            body = _article_body(article)
            handled = self._try_extract_event(
                article,
                body,
                document,
                result,
                list_items.get(article.order_index, []),
                completes_predecessor=completes_predecessor,
                constitutional_mandate=constitutional_mandate,
                considerandos=considerandos,
            )
            classification = self._classify_article(article, body, handled)
            result.article_classifications.append(classification)
            if classification.article_class == ArticleClass.OTHER and not handled:
                label = article.label_raw or article.order_index
                result.warnings.append(f"Artículo sin clasificación específica: {label}")

        self._extract_references(document, result)
        self._extract_signatories(document, result)
        return result

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------

    def _list_items_by_article(self, document: ParsedDocument) -> dict[int, list[ParsedSection]]:
        """Asocia items de lista ("- Nombre") al artículo inmediatamente anterior."""
        mapping: dict[int, list[ParsedSection]] = {}
        current_article: int | None = None
        for section in document.sections:
            if section.section_type == SectionType.ARTICLE:
                current_article = section.order_index
            elif (
                section.section_type == SectionType.ARTICLE_LIST_ITEM
                and current_article is not None
            ):
                mapping.setdefault(current_article, []).append(section)
        return mapping

    def _try_extract_event(
        self,
        article: ParsedSection,
        body: str,
        document: ParsedDocument,
        result: ExtractionResult,
        list_items: list[ParsedSection],
        *,
        completes_predecessor: bool,
        constitutional_mandate: bool,
        considerandos: list[ParsedSection],
    ) -> bool:
        # Guarda: encargos a unidades organizacionales no son eventos de personal.
        if p.ENCARGAR_ORG_GUARD_RE.match(body):
            return False

        m = p.COLLECTIVE_START_RE.match(body)
        if m and list_items:
            self._collective_start(article, m, list_items, result, constitutional_mandate)
            return True

        m = p.ACCEPT_RESIGNATION_RE.match(body)
        if m and p.looks_like_person_name(m.group("name").strip()):
            self._accept_resignation(article, m, result, completes_predecessor)
            return True

        m = p.END_DESIGNATION_RE.match(body)
        if m:
            self._end_designation(article, m, result)
            return True

        m = p.END_ACTING_RE.match(body)
        if m:
            self._end_acting(article, m, result, considerandos)
            return True

        m = p.ENCARGAR_PERSON_RE.match(body)
        if m and p.looks_like_person_name(m.group("name").strip()):
            self._encargar_person(article, m, result)
            return True

        m = p.START_EVENT_RE.match(body)
        if m and p.looks_like_person_name(m.group("name").strip()):
            self._individual_start(article, m, result, completes_predecessor)
            return True

        return False

    def _individual_start(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        result: ExtractionResult,
        completes_predecessor: bool,
    ) -> None:
        verb = m.group("verb")
        event_type = (
            EventType.APPOINTMENT
            if verb.lower().startswith(("nombrar", "se nombra"))
            else EventType.DESIGNATION
        )
        role_text = p.strip_thanks(m.group("role"))
        role_text, slot = p.extract_position_slot(role_text)
        date_phrase = m.group("date")
        effective = _explicit_date(f"a partir del {date_phrase}" if date_phrase else None)
        mention = _mention(article, m.group("name"))
        assignment = ExtractedAssignment(
            person=mention,
            position_label_raw=role_text,
            org_path=_org_path(role_text),
            assignment_kind=(
                AssignmentKind.BOARD_MEMBERSHIP
                if role_text.lower().startswith("miembro")
                else AssignmentKind.TITULAR
            ),
            position_slot=(
                ExtractedPositionSlot(
                    external_scheme=slot[0], external_code=slot[1], source_phrase=slot[2]
                )
                if slot
                else None
            ),
            valid_from=effective,
            completes_predecessor_period=completes_predecessor,
        )
        result.events.append(
            ExtractedEvent(
                event_type=event_type,
                assignment_effect=AssignmentEffect.START,
                legal_verb_raw=verb,
                article_label=article.label_raw,
                participants=[ExtractedParticipant(role=ParticipantRole.APPOINTEE, person=mention)],
                assignments=[assignment],
                effective_from=effective,
                mandate_hint=(p.PREDECESSOR_PERIOD_PHRASE if completes_predecessor else None),
                evidence=_full_span(article),
                confidence=0.95,
            )
        )

    def _collective_start(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        list_items: list[ParsedSection],
        result: ExtractionResult,
        constitutional_mandate: bool,
    ) -> None:
        role_text = p.strip_thanks(m.group("role"))
        participants: list[ExtractedParticipant] = []
        assignments: list[ExtractedAssignment] = []
        for item in list_items:
            name = item.text_raw.lstrip("- ").strip()
            if not p.looks_like_person_name(name):
                continue
            mention = _mention(item, name)
            participants.append(
                ExtractedParticipant(role=ParticipantRole.APPOINTEE, person=mention)
            )
            assignments.append(
                ExtractedAssignment(
                    person=mention,
                    position_label_raw=role_text,
                    org_path=_org_path(role_text),
                    assignment_kind=(
                        AssignmentKind.BOARD_MEMBERSHIP
                        if "miembro" in role_text.lower() or "directorio" in role_text.lower()
                        else AssignmentKind.TITULAR
                    ),
                )
            )
        result.events.append(
            ExtractedEvent(
                event_type=EventType.DESIGNATION,
                assignment_effect=AssignmentEffect.START,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=participants,
                assignments=assignments,
                is_collective=True,
                mandate_hint=(p.CONSTITUTIONAL_PERIOD_PHRASE if constitutional_mandate else None),
                evidence=_full_span(article),
                confidence=0.95,
            )
        )

    def _accept_resignation(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        result: ExtractionResult,
        completes_predecessor: bool,  # noqa: ARG002 - la renuncia no hereda mandato
    ) -> None:
        role_text = p.strip_thanks(m.group("role"))
        role_text, _ = p.extract_position_slot(role_text)
        date_phrase = m.group("date")
        effective = _explicit_date(f"a partir del {date_phrase}" if date_phrase else None)
        mention = _mention(article, m.group("name"))
        assignment = ExtractedAssignment(
            person=mention,
            position_label_raw=role_text,
            org_path=_org_path(role_text),
            assignment_kind=AssignmentKind.TITULAR,
            valid_to=effective,
        )
        result.events.append(
            ExtractedEvent(
                event_type=EventType.ACCEPT_RESIGNATION,
                assignment_effect=AssignmentEffect.END,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=[
                    ExtractedParticipant(role=ParticipantRole.RESIGNING_PERSON, person=mention)
                ],
                assignments=[assignment],
                effective_from=effective,
                evidence=_full_span(article),
                confidence=0.95,
            )
        )

    def _end_designation(self, article: ParsedSection, m, result: ExtractionResult) -> None:  # noqa: ANN001
        role_text = p.strip_thanks(m.group("role"))
        mention = _mention(article, m.group("name"))
        result.events.append(
            ExtractedEvent(
                event_type=EventType.END_DESIGNATION,
                assignment_effect=AssignmentEffect.END,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=[
                    ExtractedParticipant(role=ParticipantRole.AFFECTED_PERSON, person=mention)
                ],
                assignments=[
                    ExtractedAssignment(
                        person=mention,
                        position_label_raw=role_text,
                        org_path=_org_path(role_text),
                    )
                ],
                evidence=_full_span(article),
                confidence=0.95,
            )
        )

    def _end_acting(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        result: ExtractionResult,
        considerandos: list[ParsedSection],
    ) -> None:
        rest = m.group("rest")
        prior_doc = None
        prior_kind = None
        pm = p.PRIOR_DOC_IN_ARTICLE_RE.search(rest)
        if pm:
            prior_kind = pm.group("kind")
            prior_doc = pm.group("num")
            rest = rest[: pm.start()].strip()
        role_text = rest.strip().rstrip(".;,")

        participants: list[ExtractedParticipant] = []
        # El artículo no siempre nombra a la persona afectada; se buscan en los
        # considerandos TODOS los candidatos ("se encarga a la señora X, …, el
        # puesto de Y"), no solo el primero: dos encargos declarados son una
        # contradicción que debe abrir conflicto, nunca una elección silenciosa.
        # Junto al nombre se captura el puesto encargado y el instrumento citado,
        # que son los datos contra los que el persister corrobora.
        for recital in considerandos:
            rm = p.RECITAL_ENCARGO_RE.search(recital.text_normalized)
            if rm and p.looks_like_person_name(rm.group("name").strip()):
                # El instrumento relevante es el último citado ANTES de "se
                # encarga": es el que la frase atribuye como origen del encargo.
                cited = None
                for cm in p.RECITAL_CITED_DOC_RE.finditer(recital.text_normalized[: rm.start()]):
                    cited = cm
                substantive = rm.group("substantive")
                participants.append(
                    ExtractedParticipant(
                        role=ParticipantRole.AFFECTED_PERSON_RECITAL_CANDIDATE,
                        person=_mention(recital, rm.group("name")),
                        confidence=0.6,
                        encargo_position_raw=rm.group("position").strip().rstrip(".;,"),
                        substantive_role_raw=substantive.strip() if substantive else None,
                        cited_document_number_raw=cited.group("num") if cited else None,
                    )
                )

        result.events.append(
            ExtractedEvent(
                event_type=EventType.END_ACTING_ASSIGNMENT,
                assignment_effect=AssignmentEffect.END,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=participants,
                assignments=[
                    ExtractedAssignment(
                        person=None,
                        position_label_raw=role_text,
                        org_path=_org_path(role_text),
                        assignment_kind=AssignmentKind.ACTING,
                    )
                ],
                prior_document_number_raw=(f"{prior_kind} N° {prior_doc}" if prior_doc else None),
                evidence=_full_span(article),
                confidence=0.9,
            )
        )

    def _encargar_person(self, article: ParsedSection, m, result: ExtractionResult) -> None:  # noqa: ANN001
        resp = m.group("resp").strip()
        end_condition = None
        returning: ExtractedPersonMention | None = None
        cm = p.END_CONDITION_RE.search(resp)
        if cm:
            end_condition = cm.group("cond").rstrip(".;,")
            resp = resp[: cm.start()].strip().rstrip(".;,")
            rm = p.RETURNING_HOLDER_RE.search(end_condition)
            if rm and p.looks_like_person_name(rm.group("name").strip()):
                returning = _mention(article, rm.group("name"))
        # Recorta cláusulas normativas accesorias ("cuyas funciones u obligaciones...")
        resp = resp.split(", cuyas ")[0].strip().rstrip(".;,")

        date_phrase = m.group("date")
        effective = _explicit_date(
            f"con eficacia anticipada a partir del {date_phrase}" if date_phrase else None
        )
        mention = _mention(article, m.group("name"))
        kind = (
            AssignmentKind.ACTING
            if resp.lower().startswith(("el puesto de", "las funciones de", "el cargo de"))
            else AssignmentKind.ADDITIONAL_RESPONSIBILITY
        )
        event_type = (
            EventType.ACTING_ASSIGNMENT
            if kind == AssignmentKind.ACTING
            else EventType.ADDITIONAL_RESPONSIBILITY
        )
        participants = [ExtractedParticipant(role=ParticipantRole.APPOINTEE, person=mention)]
        if returning is not None:
            participants.append(
                ExtractedParticipant(
                    role=ParticipantRole.RETURNING_HOLDER, person=returning, confidence=0.85
                )
            )
        result.events.append(
            ExtractedEvent(
                event_type=event_type,
                assignment_effect=AssignmentEffect.START,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=participants,
                assignments=[
                    ExtractedAssignment(
                        person=mention,
                        position_label_raw=resp,
                        org_path=_org_path(resp),
                        assignment_kind=kind,
                        valid_from=effective,
                        end_condition_text=end_condition,
                    )
                ],
                effective_from=effective,
                end_condition_text=end_condition,
                evidence=_full_span(article),
                confidence=0.9,
            )
        )

    # ------------------------------------------------------------------
    # Clasificación de artículos
    # ------------------------------------------------------------------

    def _classify_article(
        self, article: ParsedSection, body: str, is_event: bool
    ) -> ArticleClassification:
        label = article.label_raw or f"art-{article.order_index}"
        if is_event:
            cls = ArticleClass.PERSONNEL_EVENT
        elif p.COUNTERSIGNATURE_RE.search(body):
            cls = ArticleClass.COUNTERSIGNATURE
        elif p.SWORN_DECLARATION_RE.search(body):
            cls = ArticleClass.DERIVED_OBLIGATION
        elif p.PUBLICATION_NOTICE_RE.search(body) or (
            p.ENCARGAR_ORG_GUARD_RE.match(body) and "publicaci" in body.lower()
        ):
            cls = ArticleClass.PUBLICATION_NOTICE
        elif p.NOTIFICATION_RE.search(body):
            cls = ArticleClass.NOTIFICATION
        else:
            cls = ArticleClass.OTHER
        return ArticleClassification(
            article_label=label, article_class=cls, evidence=_full_span(article)
        )

    # ------------------------------------------------------------------
    # Referencias documentales
    # ------------------------------------------------------------------

    def _extract_references(self, document: ParsedDocument, result: ExtractionResult) -> None:
        seen: set[tuple[ReferenceType, str]] = set()

        def add(ref_type: ReferenceType, kind: str, num: str, section: ParsedSection) -> None:
            key = (ref_type, num)
            if key in seen:
                return
            seen.add(key)
            result.references.append(
                ExtractedReference(
                    reference_type=ref_type,
                    target_number_raw=num,
                    target_doc_kind_raw=kind,
                    evidence=_full_span(section),
                )
            )

        for section in document.sections_of(SectionType.VISTOS):
            for m in p.SEEN_DOC_RE.finditer(section.text_normalized):
                add(ReferenceType.INTERNAL_SEEN_DOCUMENT, m.group("kind"), m.group("num"), section)

        for section in document.sections_of(SectionType.CONSIDERANDO):
            text = section.text_normalized
            if p.PRIOR_APPOINTMENT_CONTEXT_RE.search(text):
                for m in p.PRIOR_APPOINTMENT_DOC_RE.finditer(text):
                    add(ReferenceType.PRIOR_APPOINTMENT, m.group("kind"), m.group("num"), section)
            if text.startswith("De conformidad"):
                for m in p.NORMATIVE_DOC_RE.finditer(text):
                    add(ReferenceType.NORMATIVE_CITATION, m.group("kind"), m.group("num"), section)

    # ------------------------------------------------------------------
    # Firmantes
    # ------------------------------------------------------------------

    def _extract_signatories(self, document: ParsedDocument, result: ExtractionResult) -> None:
        order = 0
        current: ExtractedSignatory | None = None
        for section in document.sections_of(SectionType.SIGNATURE):
            text = section.text_normalized
            if p.UPPERCASE_NAME_RE.match(text) and p.looks_like_person_name(text.title()):
                order += 1
                current = ExtractedSignatory(
                    person=_mention(section, text), capacity_raw=None, signature_order=order
                )
                result.signatories.append(current)
            elif current is not None:
                current.capacity_raw = (
                    f"{current.capacity_raw}, {text}" if current.capacity_raw else text
                )
