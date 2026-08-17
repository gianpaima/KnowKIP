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
from kipu_knowledge.domain.legal_effect import find_deferral_clause
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


def _org_path(role_text: str, organization: str | None = None) -> ExtractedOrgPath:
    """Ruta organizacional del puesto.

    `organization` la aporta quien lee una tabla, donde la entidad va en su
    propia columna y no dentro de la etiqueta del cargo. Manda sobre lo que se
    deduzca del texto del rol: es un dato declarado, no una segmentación.
    """
    split = p.split_org_path(role_text)
    return ExtractedOrgPath(
        path_raw=role_text,
        organization_name=organization or split.organization,
        unit_chain=split.unit_chain,
    )


def _cell_identifiers(row: ParsedSection, columns: dict[str, int]) -> list[ExtractedIdentifier]:
    """Documento de identidad declarado en la columna que la cabecera nombra.

    En una tabla el valor viaja sin etiqueta a su lado —la etiqueta es la
    cabecera— así que no lo encuentra el reconocedor de texto corrido. Que la
    fuente lo publique en una columna rotulada es declararlo igual de explícito.
    """
    index = columns.get("identifier")
    cells = row.cells()
    if index is None or index >= len(cells):
        return []
    value = p.identifier_in_cell(cells[index])
    if value is None:
        return []
    start, end = row.cell_span(index)
    return [
        ExtractedIdentifier(
            scheme=IdentifierScheme.DNI,
            value_raw=value,
            evidence=EvidenceRef(
                section_index=row.order_index,
                article_label=row.label_raw,
                char_start=start,
                char_end=end,
                quoted_text=row.text_raw[start:end],
            ),
        )
    ]


def _article_body(section: ParsedSection) -> str:
    text = section.text_raw
    if section.label_raw and text.startswith(section.label_raw):
        text = text[len(section.label_raw) :]
    return text.strip()


def _as_resolutive_unit(article: ParsedSection, body: ParsedSection) -> ParsedSection:
    """El párrafo dispositivo de un artículo, hablando en nombre de ese artículo.

    Cuando la parte dispositiva vive en un párrafo aparte ("Artículo 1.-
    Designación" y debajo "Designar a la señora …"), el hecho lo dice el
    párrafo pero el acto es el artículo. La evidencia se ancla al párrafo —que
    es donde está el texto citado— y la etiqueta sigue siendo la del artículo,
    que es como la fuente numera lo resuelto.
    """
    return ParsedSection(
        section_type=body.section_type,
        label_raw=article.label_raw,
        order_index=body.order_index,
        text_raw=body.text_raw,
        text_normalized=body.text_normalized,
    )


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
        bodies = self._bodies_by_article(document)
        tables = self._tables_by_article(document)

        for article in articles:
            body = _article_body(article)
            attached = bodies.get(article.order_index, [])
            handled = self._try_extract_event(
                article,
                body,
                document,
                result,
                list_items.get(article.order_index, []),
                table=tables.get(article.order_index),
                completes_predecessor=completes_predecessor,
                constitutional_mandate=constitutional_mandate,
                considerandos=considerandos,
            )
            # El encabezado puede ser solo un título ("Artículo 1.- Designación").
            # Entonces lo resuelto está en los párrafos siguientes y hay que
            # leerlos, o el dispositivo entero no afirma nada pese a estar íntegro.
            if not handled:
                for attached_body in attached:
                    unit = _as_resolutive_unit(article, attached_body)
                    if self._try_extract_event(
                        unit,
                        _article_body(unit),
                        document,
                        result,
                        list_items.get(article.order_index, []),
                        table=tables.get(article.order_index),
                        completes_predecessor=completes_predecessor,
                        constitutional_mandate=constitutional_mandate,
                        considerandos=considerandos,
                    ):
                        handled = True
                        break
            # La clasificación mira el artículo completo: "Artículo 2.-
            # Notificación" solo se reconoce como tal por el texto de su cuerpo.
            classified_text = " ".join([body, *(s.text_raw for s in attached)]).strip()
            classification = self._classify_article(article, classified_text, handled)
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

    def _tables_by_article(
        self, document: ParsedDocument
    ) -> dict[int, tuple[ParsedSection | None, list[ParsedSection]]]:
        """Asocia cabecera y filas de tabla al artículo que las encabeza."""
        mapping: dict[int, tuple[ParsedSection | None, list[ParsedSection]]] = {}
        current_article: int | None = None
        for section in document.sections:
            if section.section_type == SectionType.ARTICLE:
                current_article = section.order_index
            elif current_article is None:
                continue
            elif section.section_type == SectionType.ARTICLE_TABLE_HEADER:
                mapping[current_article] = (section, [])
            elif section.section_type == SectionType.ARTICLE_TABLE_ROW:
                header, rows = mapping.get(current_article, (None, []))
                rows.append(section)
                mapping[current_article] = (header, rows)
        return mapping

    def _bodies_by_article(self, document: ParsedDocument) -> dict[int, list[ParsedSection]]:
        """Asocia los párrafos dispositivos al artículo que los encabeza."""
        mapping: dict[int, list[ParsedSection]] = {}
        current_article: int | None = None
        for section in document.sections:
            if section.section_type == SectionType.ARTICLE:
                current_article = section.order_index
            elif section.section_type == SectionType.ARTICLE_BODY and current_article is not None:
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
        table: tuple[ParsedSection | None, list[ParsedSection]] | None = None,
        completes_predecessor: bool,
        constitutional_mandate: bool,
        considerandos: list[ParsedSection],
    ) -> bool:
        # Guarda: encargos a unidades organizacionales no son eventos de personal.
        if p.ENCARGAR_ORG_GUARD_RE.match(body):
            return False

        # Un artículo colectivo publica su lista como items de guion o como
        # tabla. El acto es el mismo; solo cambia de dónde salen las personas.
        header, rows = table if table else (None, [])

        m = p.COLLECTIVE_START_RE.match(body)
        if m and list_items:
            self._collective_start(article, m, list_items, result, constitutional_mandate)
            return True
        if m and rows and self._tabular_collective(article, m, header, rows, result, starts=True):
            return True

        m = p.COLLECTIVE_END_RE.match(body)
        if m and list_items:
            self._collective_end(article, m, list_items, result)
            return True
        if m and rows and self._tabular_collective(article, m, header, rows, result, starts=False):
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
        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
        role_text, slot = p.extract_position_slot(role_text)
        date_phrase = m.group("date")
        effective = _explicit_date(f"a partir del {date_phrase}" if date_phrase else None)
        # "…, siendo su primer día de labores el 07 de agosto de 2026": la fuente
        # declara el inicio con otra fórmula. Se separa del puesto —o quedaría
        # pegada a su etiqueta— y vale como fecha expresada, no inferida.
        first_day = p.FIRST_WORKDAY_RE.search(role_text)
        if first_day:
            role_text = role_text[: first_day.start()].strip().rstrip(".;,")
            if effective.status != DateStatus.EXPLICIT:
                effective = _explicit_date(first_day.group(0).strip(" ,"))
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
        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
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

    def _collective_end(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        list_items: list[ParsedSection],
        result: ExtractionResult,
    ) -> None:
        """Un artículo que termina la designación de varias personas a la vez.

        Espejo de `_collective_start`: mismo puesto declarado en el artículo y
        una persona por item de la lista. Sin fecha: si el documento no la
        expresa queda NOT_STATED, y lo que determine la norma se decide después
        (`domain/legal_effect.py`), nunca aquí.
        """
        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
        participants: list[ExtractedParticipant] = []
        assignments: list[ExtractedAssignment] = []
        for item in list_items:
            name = item.text_raw.lstrip("- ").strip()
            if not p.looks_like_person_name(name):
                continue
            mention = _mention(item, name)
            participants.append(
                ExtractedParticipant(role=ParticipantRole.AFFECTED_PERSON, person=mention)
            )
            assignments.append(
                ExtractedAssignment(
                    person=mention,
                    position_label_raw=role_text,
                    org_path=_org_path(role_text),
                )
            )
        result.events.append(
            ExtractedEvent(
                event_type=EventType.END_DESIGNATION,
                assignment_effect=AssignmentEffect.END,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=participants,
                assignments=assignments,
                is_collective=True,
                evidence=_full_span(article),
                confidence=0.95,
            )
        )

    def _tabular_collective(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        header: ParsedSection | None,
        rows: list[ParsedSection],
        result: ExtractionResult,
        *,
        starts: bool,
    ) -> bool:
        """Designaciones o ceses colectivos publicados en tabla.

        La cabecera declara qué es cada columna; sin ella no se extrae nada,
        porque atribuir un nombre o un documento de identidad por posición
        fabricaría datos. La entidad sale de la fila, no del artículo: en una
        misma tabla cada persona va a un organismo distinto, y colgarlas todas
        del puesto genérico las fundiría en un único cargo inexistente.

        Devuelve si el artículo quedó extraído; un False deja que se clasifique
        como no-evento y quede el warning, que es visible.
        """
        columns = p.table_columns(header.cells()) if header is not None else {}
        if "name" not in columns:
            where = article.label_raw or article.order_index
            result.warnings.append(
                f"Tabla sin columna de nombre declarada en {where}: no se extrajo ninguna persona"
            )
            return False

        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
        participants: list[ExtractedParticipant] = []
        assignments: list[ExtractedAssignment] = []
        for row in rows:
            cells = row.cells()
            if columns["name"] >= len(cells):
                continue
            name = cells[columns["name"]].strip()
            if not p.looks_like_person_name(name.replace(",", " ")):
                continue
            mention = ExtractedPersonMention(
                text_raw=name,
                evidence=_name_span(row, name),
                identifiers=_cell_identifiers(row, columns),
            )
            organization = (
                cells[columns["organization"]].strip()
                if "organization" in columns and columns["organization"] < len(cells)
                else None
            )
            participants.append(
                ExtractedParticipant(
                    role=ParticipantRole.APPOINTEE if starts else ParticipantRole.AFFECTED_PERSON,
                    person=mention,
                )
            )
            assignments.append(
                ExtractedAssignment(
                    person=mention,
                    position_label_raw=role_text,
                    org_path=_org_path(role_text, organization),
                    # Igual que en el colectivo de guiones: una designación es
                    # titular salvo que el puesto sea de órgano colegiado. El
                    # cese no afirma naturaleza, y por eso ahí no se toca.
                    assignment_kind=(
                        (
                            AssignmentKind.BOARD_MEMBERSHIP
                            if "miembro" in role_text.lower() or "directorio" in role_text.lower()
                            else AssignmentKind.TITULAR
                        )
                        if starts
                        else AssignmentKind.UNKNOWN
                    ),
                )
            )
        if not participants:
            return False

        result.events.append(
            ExtractedEvent(
                event_type=EventType.DESIGNATION if starts else EventType.END_DESIGNATION,
                assignment_effect=AssignmentEffect.START if starts else AssignmentEffect.END,
                legal_verb_raw=m.group("verb"),
                article_label=article.label_raw,
                participants=participants,
                assignments=assignments,
                is_collective=True,
                evidence=_full_span(article),
                confidence=0.95,
            )
        )
        return True

    def _accept_resignation(
        self,
        article: ParsedSection,
        m,  # noqa: ANN001
        result: ExtractionResult,
        completes_predecessor: bool,  # noqa: ARG002 - la renuncia no hereda mandato
    ) -> None:
        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
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
        role_text = p.strip_admin_clauses(p.strip_thanks(m.group("role")))
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
        role_text = p.strip_admin_clauses(rest.strip().rstrip(".;,"))

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
        # La fecha de eficacia puede venir al final del encargo en vez de junto al
        # nombre. Se recorta siempre —o viajaría dentro de la etiqueta del puesto y
        # de ahí al nombre de la organización— y vale como fecha solo si el patrón
        # principal no la capturó ya en su posición temprana.
        date_phrase = m.group("date")
        tm = p.TRAILING_EFFECTIVE_FROM_RE.search(resp)
        if tm:
            resp = (resp[: tm.start()] + resp[tm.end() :]).strip().rstrip(".;,")
            date_phrase = date_phrase or tm.group("date")
        # "en adición a sus funciones" no describe el puesto: declara que se acumula
        # al que la persona ya ejerce. Se recorta de la etiqueta y se conserva como
        # la afirmación explícita de la fuente sobre la naturaleza del encargo.
        am = p.ADDITION_CLAUSE_RE.search(resp)
        is_addition = am is not None
        if am:
            resp = (resp[: am.start()] + resp[am.end() :]).strip().rstrip(".;,")
        # Recorta cláusulas normativas accesorias ("cuyas funciones u obligaciones...")
        resp = resp.split(", cuyas ")[0].strip().rstrip(".;,")
        resp = p.strip_admin_clauses(resp)
        # El artículo suele identificar a la persona por el cargo que YA ocupa antes
        # de decir qué se le encarga. Ese cargo es un hecho distinto del encargo: se
        # registra aparte en vez de quedar pegado a la etiqueta del puesto encargado.
        resp, substantive_role = p.split_encargo_appositive(resp)

        effective = _explicit_date(
            f"con eficacia anticipada a partir del {date_phrase}" if date_phrase else None
        )
        mention = _mention(article, m.group("name"))
        kind = (
            AssignmentKind.ADDITIONAL_RESPONSIBILITY
            if is_addition
            else (
                AssignmentKind.ACTING
                if resp.lower().startswith(("el puesto de", "las funciones de", "el cargo de"))
                else AssignmentKind.ADDITIONAL_RESPONSIBILITY
            )
        )
        event_type = (
            EventType.ACTING_ASSIGNMENT
            if kind == AssignmentKind.ACTING
            else EventType.ADDITIONAL_RESPONSIBILITY
        )
        participants = [
            ExtractedParticipant(
                role=ParticipantRole.APPOINTEE,
                person=mention,
                substantive_role_raw=substantive_role,
            )
        ]
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
        # Antes que el aviso de publicación: la cláusula de vigencia nombra la
        # publicación ("efectividad a partir del día siguiente de la
        # publicación") y se la llevaba por delante. Se pregunta con la misma
        # función que clasifica el diferimiento de la fecha legal, de modo que la
        # clasificación del artículo y la regla no puedan discrepar. Aquí da
        # igual de qué clase sea: las dos son cláusulas de vigencia.
        elif find_deferral_clause([body]) is not None:
            cls = ArticleClass.EFFECTIVE_DATE_CLAUSE
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
