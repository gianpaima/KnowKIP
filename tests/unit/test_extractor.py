"""Pruebas unitarias del extractor determinista sobre textos sintéticos y reales."""

from datetime import date
from pathlib import Path

import pytest

from kipu_knowledge.adapters.extraction.deterministic import DeterministicExtractor
from kipu_knowledge.adapters.extraction.patterns import split_encargo_appositive, split_org_path
from kipu_knowledge.adapters.parsing.html_parser import ElPeruanoHtmlParser
from kipu_knowledge.domain.contracts import SourceReference
from kipu_knowledge.domain.enums import (
    ArticleClass,
    AssignmentEffect,
    AssignmentKind,
    DateStatus,
    DocumentTypeCode,
    EventType,
    ParticipantRole,
    SectionType,
)
from kipu_knowledge.domain.parsed import ParsedDocument, ParsedSection

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "elperuano"


def _doc_with_articles(*articles: str) -> ParsedDocument:
    sections = []
    for i, text in enumerate(articles):
        label = text.split("- ")[0] + "- " if ".-" in text[:30] else None
        sections.append(
            ParsedSection(
                section_type=SectionType.ARTICLE,
                label_raw=label.strip() if label else None,
                order_index=i,
                text_raw=text,
                text_normalized=text,
            )
        )
    return ParsedDocument(
        publication_code="0000000-0",
        source_series="NL",
        title_raw="t",
        document_type_raw="RESOLUCIÓN MINISTERIAL",
        document_type_code=DocumentTypeCode.RESOLUCION_MINISTERIAL,
        number_raw="N° 1-2026-X",
        number_normalized="1-2026-X",
        sections=sections,
    )


@pytest.fixture(scope="module")
def extractor() -> DeterministicExtractor:
    return DeterministicExtractor()


def _extract_fixture(code: str):
    parser = ElPeruanoHtmlParser()
    ref = SourceReference("EL_PERUANO_NL", "NL", code, None)
    doc = parser.parse((FIXTURES / f"{code}.html").read_bytes(), ref)
    return DeterministicExtractor().extract(doc)


class TestVerbPatterns:
    def test_designar_simple(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Designar al señor JUAN PEREZ QUISPE en el cargo de "
            "Director de la Oficina General de Administración del Ministerio de Salud."
        )
        result = extractor.extract(doc)
        assert len(result.events) == 1
        event = result.events[0]
        assert event.event_type == EventType.DESIGNATION
        assert event.assignment_effect == AssignmentEffect.START
        assert event.effective_from.status == DateStatus.NOT_STATED
        assert event.participants[0].person.text_raw == "JUAN PEREZ QUISPE"

    def test_designar_con_fecha(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- DESIGNAR a partir del 06 de agosto de 2026, a la señora "
            "MARIA LOPEZ DIAZ en el cargo de confianza de Gerenta General de la "
            "Biblioteca Nacional del Perú."
        )
        event = extractor.extract(doc).events[0]
        assert event.effective_from.value == date(2026, 8, 6)
        assert event.effective_from.status == DateStatus.EXPLICIT
        assert event.effective_from.source_phrase  # la fecha lleva su frase fuente

    def test_designar_a_partir_de_la_fecha_no_fabrica_fecha(self, extractor):
        """ "… – APCI, a partir de la fecha": coletilla de eficacia sin cifra. Se
        recorta de la ruta del puesto —o viajaba dentro del nombre de la
        organización— pero NO produce fecha: sin cifra no hay fecha expresada."""
        doc = _doc_with_articles(
            "Artículo 1.- Designar a la señora ROSA ELVIRA PAREDES ROJAS en el cargo de "
            "Directora Ejecutiva de la Agencia Peruana de Cooperación Internacional – APCI, "
            "a partir de la fecha."
        )
        event = extractor.extract(doc).events[0]
        assert event.effective_from.status == DateStatus.NOT_STATED
        assignment = event.assignments[0]
        assert assignment.org_path.organization_name == (
            "Agencia Peruana de Cooperación Internacional – APCI"
        )
        assert "a partir" not in assignment.position_label_raw

    def test_renuncia_con_ultimo_dia_de_labores(self, extractor):
        """RJ de la ANIN (agosto 2026): el fin declarado con "considerándosele
        como último día de labores…". Sin reconocerlo, la frase entera —fecha
        incluida— viajaba dentro del cargo y de ahí al nombre del órgano."""
        doc = _doc_with_articles(
            "Artículo 1.- Aceptar la renuncia del señor JUAN PEREZ QUISPE al cargo de "
            "Asesor de la Jefatura de la Autoridad Nacional de Infraestructura, "
            "considerándosele como último día de labores el 7 de agosto de 2026 y en "
            "consecuencia dar por concluida la designación dispuesta en el artículo 1 "
            "de la Resolución Jefatural N°00011-2026-ANIN/JEF."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.ACCEPT_RESIGNATION
        assert event.effective_from.status == DateStatus.EXPLICIT
        assert event.effective_from.value == date(2026, 8, 7)
        assignment = event.assignments[0]
        assert assignment.org_path.organization_name == "Autoridad Nacional de Infraestructura"
        assert "último día" not in assignment.position_label_raw
        assert "considerándosele" not in assignment.position_label_raw

    def test_encargo_concluido_con_persona_en_el_articulo(self, extractor):
        """RS de la PCM sobre el CEPLAN: el artículo nombra a la persona junto al
        puesto. Sin separarla, el nombre viajaba dentro de la etiqueta y el
        agradecimiento dentro del nombre del órgano."""
        doc = _doc_with_articles(
            "Artículo 1.- Dar por concluido el encargo del señor LUIS ENRIQUE DE LA FLOR "
            "SAENZ en el puesto de Presidente del Consejo Directivo del Centro Nacional "
            "de Planeamiento Estratégico (CEPLAN), dándosele las gracias por los "
            "servicios prestados."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.END_ACTING_ASSIGNMENT
        assert event.participants[0].role == ParticipantRole.AFFECTED_PERSON
        assert event.participants[0].person.text_raw == "LUIS ENRIQUE DE LA FLOR SAENZ"
        assignment = event.assignments[0]
        assert assignment.person is not None
        assert assignment.org_path.organization_name == (
            "Centro Nacional de Planeamiento Estratégico (CEPLAN)"
        )
        assert assignment.org_path.role_label == "Presidente del Consejo Directivo"
        assert "gracias" not in assignment.position_label_raw
        assert "señor" not in assignment.position_label_raw

    def test_nombrar_es_appointment(self, extractor):
        doc = _doc_with_articles(
            "Artículo 2.- Nombrar a la señora Clara Rossana Urteaga Goldstein como "
            "Presidenta del Tribunal Fiscal del Ministerio de Economía y Finanzas."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.APPOINTMENT

    def test_aceptar_renuncia_con_fecha(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Aceptar la renuncia, a partir del 4 de agosto de 2026, del señor "
            "CARLOS MANUEL YAÑEZ LAZO al cargo de Jefe del Centro Nacional de Estimación, "
            "Prevención y Reducción del Riesgo de Desastres -CENEPRED, dándosele las gracias "
            "por los servicios prestados."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.ACCEPT_RESIGNATION
        assert event.assignment_effect == AssignmentEffect.END
        assert event.effective_from.value == date(2026, 8, 4)
        assignment = event.assignments[0]
        assert assignment.valid_to.value == date(2026, 8, 4)
        assert "dándosele" not in assignment.position_label_raw

    def test_designar_con_coletilla_de_regimen(self, extractor):
        """Regresión RM 299-2026-VIVIENDA: la cláusula del régimen laboral no es
        parte del nombre del órgano ni del puesto (fabricaba organizaciones como
        "Ministerio de Vivienda…, bajo el régimen de la Ley N° 30057")."""
        doc = _doc_with_articles(
            "Artículo Único.- Designar al señor XAVIER ARNULFO ZAGACETA MALDONADO, en el "
            "puesto de Asesor de Secretaría General del Ministerio de Vivienda, Construcción "
            "y Saneamiento, bajo el régimen de la Ley N° 30057, Ley del Servicio Civil."
        )
        assignment = extractor.extract(doc).events[0].assignments[0]
        assert assignment.org_path.organization_name == (
            "Ministerio de Vivienda, Construcción y Saneamiento"
        )
        assert "régimen" not in assignment.position_label_raw

    def test_designar_con_coletilla_puesto_de_confianza(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Designar a la señora Mayra Mercedes Elisabeth Figueroa Valderrama "
            "Vda. De Sánchez, en el puesto de Viceministra de Minas del Ministerio de Energía "
            "y Minas, puesto considerado de confianza."
        )
        assignment = extractor.extract(doc).events[0].assignments[0]
        assert assignment.org_path.organization_name == "Ministerio de Energía y Minas"
        assert "confianza" not in assignment.position_label_raw

    def test_designar_con_regimen_cas_no_rompe_la_sigla(self, extractor):
        """La sigla pegada con guion o raya es parte del nombre; la coletilla CAS no."""
        doc = _doc_with_articles(
            "Artículo 2.- Designar al señor Johan Jan Cubas Arias, en el cargo de Asesor I "
            "de la Gerencia General del Organismo de Formalización de la Propiedad Informal "
            "– COFOPRI, bajo el régimen especial de Contratación Administrativa de Servicios "
            "regulado por el Decreto Legislativo N° 1057, bajo la modalidad de CAS confianza."
        )
        assignment = extractor.extract(doc).events[0].assignments[0]
        assert assignment.org_path.organization_name == (
            "Organismo de Formalización de la Propiedad Informal – COFOPRI"
        )

    def test_designar_con_grupo_y_codigo_de_puesto(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Designar, a partir del 10 de agosto de 2026, al señor Renato "
            "Adrian Salinas Huett, en el puesto de confianza de Jefe de la Oficina de "
            "Administración del Organismo de Supervisión de los Recursos Forestales y de "
            "Fauna Silvestre – OSINFOR, perteneciente al grupo de Directivo Público, con "
            "código de puesto DP00102032, bajo el régimen de la Ley N° 30057, Ley del "
            "Servicio Civil."
        )
        event = extractor.extract(doc).events[0]
        assignment = event.assignments[0]
        assert event.effective_from.value == date(2026, 8, 10)
        assert assignment.org_path.organization_name == (
            "Organismo de Supervisión de los Recursos Forestales y de Fauna Silvestre – OSINFOR"
        )
        assert "DP00102032" not in assignment.position_label_raw

    def test_designar_con_cargo_estructural_tras_guion(self, extractor):
        """El cargo estructural del clasificador ("- Directora de Sistema
        Administrativo II") tampoco es parte del nombre de la entidad."""
        doc = _doc_with_articles(
            "Artículo Único.- Designar a la señora GESSICA GISELLE REQUENA RENTERIA en el "
            "cargo de Jefa de la Oficina General de Gestión Documentaria del Ministerio de "
            "Defensa - Directora de Sistema Administrativo II."
        )
        assignment = extractor.extract(doc).events[0].assignments[0]
        assert assignment.org_path.organization_name == "Ministerio de Defensa"
        assert assignment.org_path.unit_chain == ["Oficina General de Gestión Documentaria"]

    def test_dar_por_concluida_designacion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Dar por concluida la designación del señor PEDRO GOMEZ RIOS "
            "en el cargo de Asesor del Despacho Ministerial del Ministerio del Interior."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.END_DESIGNATION
        assert event.assignment_effect == AssignmentEffect.END

    def test_dar_por_concluido_encargo_sin_persona(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1.- Dar por concluido el encargo de puesto de Presidenta del "
            "Tribunal Fiscal del Ministerio de Economía y Finanzas, dispuesto mediante "
            "la Resolución Suprema Nº 044-2025-EF."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.END_ACTING_ASSIGNMENT
        assert event.prior_document_number_raw == "Resolución Suprema N° 044-2025-EF"

    def test_encargar_persona_con_condicion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 1º.- ENCARGAR, al servidor civil Arnold Anthony Crisóstomo Quispe, "
            "con eficacia anticipada a partir del 30 de julio de 2026, la obligación de "
            "brindar información ante la Intendencia Nacional de Bomberos del Perú, "
            "hasta el retorno del descanso vacacional del servidor Jhon Eduardo Cárcamo Litano."
        )
        event = extractor.extract(doc).events[0]
        assert event.event_type == EventType.ADDITIONAL_RESPONSIBILITY
        assert event.effective_from.value == date(2026, 7, 30)
        assert event.end_condition_text is not None
        assert event.end_condition_text.startswith("hasta el retorno")
        roles = {p.role for p in event.participants}
        assert ParticipantRole.RETURNING_HOLDER in roles
        assert event.assignments[0].assignment_kind == AssignmentKind.ADDITIONAL_RESPONSIBILITY

    def test_encargar_con_aposicion_no_contamina_la_organizacion(self, extractor):
        # Regresión: este artículo creó una organización cuyo nombre era el
        # artículo entero desde "del Ministerio…" hasta el punto final, porque
        # ninguna de las tres colas (aposición, fecha, condición) se recortaba y
        # "Organismo" no era cabecera reconocida donde cerrar el segmento.
        doc = _doc_with_articles(
            "Artículo 1.- Encargar al señor Julio César Mendoza Alvarado, Viceministro de "
            "Prestaciones Sociales del Ministerio de Desarrollo e Inclusión Social, las "
            "funciones del puesto de Presidente Ejecutivo del Organismo de Focalización e "
            "Información Social (OFIS), en adición a sus funciones, a partir del 7 de "
            "agosto de 2026 y en tanto dure la ausencia de su titular."
        )
        event = extractor.extract(doc).events[0]
        assignment = event.assignments[0]

        assert assignment.org_path.organization_name == (
            "Organismo de Focalización e Información Social (OFIS)"
        )
        # La etiqueta conserva la ruta cruda, pero ya sin fecha, condición ni aposición.
        assert assignment.position_label_raw == (
            "las funciones del puesto de Presidente Ejecutivo del Organismo de Focalización e "
            "Información Social (OFIS)"
        )
        # El cargo que la persona ya ocupaba es un hecho aparte, no parte del encargo.
        appointee = next(p for p in event.participants if p.role == ParticipantRole.APPOINTEE)
        assert appointee.substantive_role_raw == (
            "Viceministro de Prestaciones Sociales del Ministerio de Desarrollo e Inclusión Social"
        )
        assert event.effective_from.value == date(2026, 8, 7)
        assert event.effective_from.status == DateStatus.EXPLICIT
        assert event.end_condition_text == "en tanto dure la ausencia de su titular"
        assert assignment.assignment_kind == AssignmentKind.ADDITIONAL_RESPONSIBILITY

    def test_encargar_condicion_en_tanto_dure(self, extractor):
        # "en tanto dure" es la fórmula que END_CONDITION_RE no cubría; sin ella la
        # condición entera quedaba pegada a la etiqueta del puesto.
        doc = _doc_with_articles(
            "Artículo 1.- Encargar al señor Ricardo Elías Ponce, el puesto de Jefe de la "
            "Oficina General de Administración del Ministerio de Cultura, en tanto dure la "
            "ausencia de su titular."
        )
        event = extractor.extract(doc).events[0]
        assert event.end_condition_text == "en tanto dure la ausencia de su titular"
        assert event.assignments[0].org_path.organization_name == "Ministerio de Cultura"
        assert event.assignments[0].assignment_kind == AssignmentKind.ACTING

    def test_encargar_sin_aposicion_conserva_todo_el_encargo(self, extractor):
        # La aposición solo existe si tras la coma arranca una fórmula de encargo.
        # Un puesto con coma legítima no debe partirse.
        doc = _doc_with_articles(
            "Artículo 1.- Encargar a la señora Rosa María Linares Tello, el puesto de Jefa "
            "de la Oficina General de Planeamiento, Presupuesto y Modernización del "
            "Ministerio de la Producción."
        )
        event = extractor.extract(doc).events[0]
        appointee = next(p for p in event.participants if p.role == ParticipantRole.APPOINTEE)
        assert appointee.substantive_role_raw is None
        assert event.assignments[0].org_path.organization_name == "Ministerio de la Producción"
        assert "Presupuesto y Modernización" in event.assignments[0].org_path.unit_chain[0]

    def test_encargar_a_oficina_no_es_evento(self, extractor):
        # Regla 23: encargos de publicación a unidades no son eventos de personal.
        doc = _doc_with_articles(
            "Artículo 2.- ENCARGAR a la Oficina de Tecnologías de la Información la "
            "publicación de la presente Resolución en el portal web institucional."
        )
        result = extractor.extract(doc)
        assert result.events == []
        assert result.article_classifications[0].article_class == ArticleClass.PUBLICATION_NOTICE

    def test_declaracion_jurada_no_es_designacion(self, extractor):
        doc = _doc_with_articles(
            "Artículo 2.- La mencionada servidora debe presentar su Declaración Jurada de "
            "Ingresos, Bienes y Rentas conforme a la normatividad vigente."
        )
        result = extractor.extract(doc)
        assert result.events == []
        assert result.article_classifications[0].article_class == ArticleClass.DERIVED_OBLIGATION

    def test_multiple_actions_in_one_document(self, extractor):
        result = _extract_fixture("2540905-3")
        assert len(result.events) == 2
        types = [e.event_type for e in result.events]
        assert EventType.ACCEPT_RESIGNATION in types
        assert EventType.DESIGNATION in types

    def test_collective_event_three_people(self, extractor):
        result = _extract_fixture("2540905-2")
        event = result.events[0]
        assert event.is_collective
        assert len(event.assignments) == 3
        names = [a.person.text_raw for a in event.assignments]
        assert names == [
            "Inés Marylin Choy Chong",
            "Luis Miguel Palomino Bonilla",
            "Gustavo Adolfo Yamada Fukusaki",
        ]


class TestOrgPathSplit:
    def test_full_chain(self):
        split = split_org_path(
            "Jefa de Atención al Ciudadano y Gestión Documental de la Oficina de Atención "
            "al Ciudadano y Gestión Documental de la Secretaría General del Ministerio de "
            "Desarrollo Agrario y Riego"
        )
        assert split.organization == "Ministerio de Desarrollo Agrario y Riego"
        assert split.unit_chain == [
            "Oficina de Atención al Ciudadano y Gestión Documental",
            "Secretaría General",
        ]
        assert split.role_label == "Jefa de Atención al Ciudadano y Gestión Documental"

    def test_programa_nacional_es_organizacion_no_unidad(self):
        # RD 000128-2026-MINEDU-VMGI-PRONIED-DE: sin "Programa Nacional" como
        # cabecera de órgano, la ruta entera quedaba dentro del cargo y el
        # puesto nacía sin organización (POSITION_ORG_UNRESOLVED evitable).
        split = split_org_path(
            "Directora del Sistema Administrativo II de la Unidad de Recursos Humanos "
            "de la Oficina General de Administración del Programa Nacional de "
            "Infraestructura Educativa - PRONIED"
        )
        assert split.organization == "Programa Nacional de Infraestructura Educativa - PRONIED"
        assert split.unit_chain == [
            "Unidad de Recursos Humanos",
            "Oficina General de Administración",
        ]
        assert split.role_label == "Directora del Sistema Administrativo II"

    def test_organismos_adscritos_son_organizacion(self):
        # Casos reales pendientes de agosto 2026: EsSalud, IPEN, PROMPERÚ y
        # FONDEPES designan (o reciben representantes) y sus cabeceras no se
        # reconocían, así que cada puesto nacía sin organización.
        essalud = split_org_path(
            "representante del Estado ante el Consejo Directivo del "
            "Seguro Social de Salud – ESSALUD"
        )
        assert essalud.organization == "Seguro Social de Salud – ESSALUD"
        assert essalud.role_label == "representante del Estado ante el Consejo Directivo"

        ipen = split_org_path(
            "Director de la Oficina de Planeamiento y Presupuesto del "
            "Instituto Peruano de Energía Nuclear – IPEN"
        )
        assert ipen.organization == "Instituto Peruano de Energía Nuclear – IPEN"
        assert ipen.unit_chain == ["Oficina de Planeamiento y Presupuesto"]

        promperu = split_org_path(
            "Gerente General de la Comisión de Promoción del Perú para la "
            "Exportación y el Turismo - PROMPERÚ"
        )
        assert promperu.organization == (
            "Comisión de Promoción del Perú para la Exportación y el Turismo - PROMPERÚ"
        )
        assert promperu.role_label == "Gerente General"

        fondepes = split_org_path(
            "Director de la Dirección General de Proyectos y Gestión Financiera para el "
            "Desarrollo Pesquero Artesanal y Acuícola del Fondo Nacional de Desarrollo Pesquero"
        )
        assert fondepes.organization == "Fondo Nacional de Desarrollo Pesquero"
        assert fondepes.unit_chain == [
            "Dirección General de Proyectos y Gestión Financiera para el "
            "Desarrollo Pesquero Artesanal y Acuícola"
        ]
        assert fondepes.role_label == "Director"

    def test_sigla_sola_del_catalogo_es_organizacion(self):
        # Cola de agosto 2026: "… del FONDEPES" nombra a la entidad solo por su
        # sigla y el puesto nacía sin organización (3 tareas evitables). La
        # comparación es sensible a mayúsculas: solo la sigla, nunca la palabra.
        split = split_org_path("Jefe de la Oficina General de Asesoría Jurídica del FONDEPES")
        assert split.organization == "FONDEPES"
        assert split.unit_chain == ["Oficina General de Asesoría Jurídica"]
        assert split.role_label == "Jefe"

    def test_organismos_de_la_puesta_al_dia_son_organizacion(self):
        # Casos reales de la cola de agosto 2026: AGRO RURAL, DINI, ANIN,
        # SENCICO e INGEMMET designan en resoluciones propias y sus cabeceras
        # no se reconocían, así que cada puesto nacía sin organización.
        agro = split_org_path(
            "Jefe de la Unidad de Asesoría Jurídica del Programa de Desarrollo "
            "Productivo Agrario Rural – AGRO RURAL"
        )
        assert agro.organization == "Programa de Desarrollo Productivo Agrario Rural – AGRO RURAL"
        assert agro.unit_chain == ["Unidad de Asesoría Jurídica"]

        dini = split_org_path("Director Ejecutivo de la Dirección Nacional de Inteligencia – DINI")
        assert dini.organization == "Dirección Nacional de Inteligencia – DINI"
        assert dini.role_label == "Director Ejecutivo"

        anin = split_org_path("Asesor de Jefatura de la Autoridad Nacional de Infraestructura")
        assert anin.organization == "Autoridad Nacional de Infraestructura"
        assert anin.unit_chain == ["Jefatura"]

        ingemmet = split_org_path(
            "miembro del Consejo Directivo del Instituto Geológico, Minero y Metalúrgico – INGEMMET"
        )
        assert ingemmet.organization == "Instituto Geológico, Minero y Metalúrgico – INGEMMET"
        assert ingemmet.role_label == "miembro del Consejo Directivo"

        sencico = split_org_path(
            "Presidente Ejecutivo del Servicio Nacional de Capacitación para la "
            "Industria de la Construcción - SENCICO"
        )
        assert sencico.organization == (
            "Servicio Nacional de Capacitación para la Industria de la Construcción - SENCICO"
        )
        assert sencico.role_label == "Presidente Ejecutivo"

    def test_cola_del_segmento_de_organizacion_vuelve_al_rol(self):
        # Casos reales de ORG_VARIANT_CHECK de agosto 2026: lo que sigue a la
        # entidad no es parte de su nombre. "ante …" y "en representación …"
        # distinguen el puesto y vuelven al rol; la acumulación no.
        mef = split_org_path(
            "representante del Ministerio de Economía y Finanzas ante el "
            "Directorio del Banco de la Nación"
        )
        assert mef.organization == "Ministerio de Economía y Finanzas"
        assert mef.role_label == "representante ante el Directorio del Banco de la Nación"

        oece = split_org_path(
            "miembro del Consejo Directivo del Organismo Especializado para las "
            "Contrataciones Públicas Eficientes – OECE, en representación del Poder Ejecutivo"
        )
        assert oece.organization == (
            "Organismo Especializado para las Contrataciones Públicas Eficientes – OECE"
        )
        assert oece.role_label == (
            "miembro del Consejo Directivo, en representación del Poder Ejecutivo"
        )

        produce = split_org_path(
            "Presidente del Consejo de Supervisión de Fondos del Ministerio de la "
            "Producción, en adición a las funciones que actualmente desempeña"
        )
        assert produce.organization == "Ministerio de la Producción"
        assert "adición" not in (produce.role_label or "")

    def test_programa_sectorial_no_es_cabecera_de_organizacion(self):
        # "Programa" a secas partiría el cargo estructural del clasificador.
        split = split_org_path("Director de Programa Sectorial II de la Oficina de Presupuesto")
        assert split.role_label == "Director de Programa Sectorial II"
        assert split.organization is None
        assert split.unit_chain == ["Oficina de Presupuesto"]

    def test_no_org_head_is_conservative(self):
        split = split_org_path("Superintendente Nacional de Aduanas y de Administración Tributaria")
        assert split.organization is None
        assert split.unit_chain == []

    def test_organismo_publico_es_organizacion_no_unidad(self):
        split = split_org_path(
            "las funciones del puesto de Presidente Ejecutivo del Organismo de Focalización "
            "e Información Social (OFIS)"
        )
        assert split.organization == "Organismo de Focalización e Información Social (OFIS)"
        assert split.role_label == "las funciones del puesto de Presidente Ejecutivo"

    def test_organismo_cierra_el_segmento_anterior(self):
        # Sin "Organismo" como cabecera el nombre del ministerio se extendía hasta
        # el final del texto porque no había dónde cortar.
        split = split_org_path(
            "Jefe de la Oficina de Presupuesto del Organismo de Evaluación y Fiscalización "
            "Ambiental"
        )
        assert split.organization == "Organismo de Evaluación y Fiscalización Ambiental"
        assert split.unit_chain == ["Oficina de Presupuesto"]

    def test_comma_inside_unit_name(self):
        split = split_org_path(
            "Jefa de Presupuesto de la Oficina de Presupuesto de la Oficina General de "
            "Planeamiento, Presupuesto y Modernización del Ministerio de la Producción"
        )
        assert split.organization == "Ministerio de la Producción"
        assert "Oficina General de Planeamiento, Presupuesto y Modernización" in split.unit_chain


class TestEncargoAppositive:
    def test_splits_on_declared_encargo_head(self):
        resp, appositive = split_encargo_appositive(
            "Viceministro de Prestaciones Sociales del Ministerio de Desarrollo e Inclusión "
            "Social, las funciones del puesto de Presidente Ejecutivo del OFIS"
        )
        assert appositive == (
            "Viceministro de Prestaciones Sociales del Ministerio de Desarrollo e Inclusión Social"
        )
        assert resp == "las funciones del puesto de Presidente Ejecutivo del OFIS"

    def test_no_appositive_when_encargo_starts_the_text(self):
        text = "el puesto de Jefe de la Oficina General de Administración"
        assert split_encargo_appositive(text) == (text, None)

    def test_no_appositive_without_a_declared_head(self):
        # Sin fórmula de encargo tras la coma no hay frontera: partir por la primera
        # coma trocearía "Planeamiento, Presupuesto y Modernización".
        text = "Jefa de la Oficina General de Planeamiento, Presupuesto y Modernización"
        assert split_encargo_appositive(text) == (text, None)

    def test_first_head_wins_over_later_ones(self):
        resp, appositive = split_encargo_appositive(
            "Director de Sistemas, el cargo de Jefe de la Unidad de Personal, "
            "las funciones de custodia"
        )
        assert appositive == "Director de Sistemas"
        assert resp == "el cargo de Jefe de la Unidad de Personal, las funciones de custodia"


class TestSignatories:
    def test_signature_capacity_pairing(self):
        result = _extract_fixture("2540903-1")
        names = [(s.person.text_raw, s.capacity_raw) for s in result.signatories]
        assert names == [
            ("KEIKO SOFÍA FUJIMORI HIGUCHI", "Presidenta de la República"),
            ("RAFAEL JORGE BELAUNDE LLOSA", "Ministro de Defensa"),
        ]

    def test_multiline_capacity(self):
        result = _extract_fixture("2540702-1")
        assert result.signatories[0].capacity_raw == (
            "Intendente Nacional, Intendencia Nacional de Bomberos del Perú"
        )


# ---------------------------------------------------------------------------
# Los seis huecos de la edición del 2026-08-07
# ---------------------------------------------------------------------------


def test_resignation_may_be_formulada_or_presentada(extractor):
    """Regresión: solo se reconocía "presentada por".

    Con "formulada por" el nombre capturado empezaba en la palabra "formulada",
    fallaba el guardado de nombre-plausible y el artículo no producía evento:
    quien renunció desaparecía del sistema.
    """
    result = _extract_fixture("2540926-1")
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == EventType.ACCEPT_RESIGNATION
    assert event.assignment_effect == AssignmentEffect.END
    # Sin coma final: la coma separa el nombre del cargo, no forma parte del nombre.
    assert event.assignments[0].person.text_raw == "MARCO ANTONIO MALDONADO GUTARRA"


def test_first_workday_is_an_expressed_date_not_an_inferred_one(extractor):
    """ "…, siendo su primer día de labores el 07 de agosto de 2026".

    La fuente SÍ expresa la fecha, solo que con otra fórmula. Sin reconocerla se
    perdían dos cosas: la fecha quedaba NOT_STATED pese a constar —y entonces la
    respondía la norma, que no es lo mismo que lo que el documento dice— y la
    frase entera contaminaba la etiqueta del puesto.
    """
    result = _extract_fixture("2540909-1")
    event = result.events[0]
    assert event.effective_from.status == DateStatus.EXPLICIT
    assert event.effective_from.value == date(2026, 8, 7)
    assert "primer día de labores" not in event.assignments[0].position_label_raw
    assert event.assignments[0].position_label_raw.startswith("Asesor de Alta Dirección")


def test_collective_end_is_extracted_like_a_collective_start(extractor):
    """ "Dejar sin efecto las designaciones … de los servidores que se indican"."""
    result = _extract_fixture("2540315-1")
    kinds = [(e.event_type, e.assignment_effect, e.is_collective) for e in result.events]
    assert (EventType.END_DESIGNATION, AssignmentEffect.END, True) in kinds
    assert (EventType.DESIGNATION, AssignmentEffect.START, True) in kinds
    names = {a.person.text_raw for e in result.events for a in e.assignments}
    assert names == {
        "JORGE PEREZ VEGA",
        "FREDY BELBER DAZA NAVAL",
        "ETHAN SEBASTIAN ARENAS RODRIGUEZ",
        "EVELYN ESTEFANY MORALES ORDINOLA",
    }


def test_tabular_collective_keeps_each_person_with_their_own_entity(extractor):
    """Cada fila de la tabla lleva su entidad, y esa entidad es la que vale.

    Colgar a todas las personas del puesto genérico del artículo ("Jefe/a del
    Órgano de Control Institucional") las fundiría en un único cargo que no
    existe: la misma etiqueta designa un puesto distinto en cada entidad.
    """
    result = _extract_fixture("2540891-1")
    by_effect = {e.assignment_effect: e for e in result.events}
    ended = {
        a.person.text_raw: a.org_path.organization_name
        for a in by_effect[AssignmentEffect.END].assignments
    }
    started = {
        a.person.text_raw: a.org_path.organization_name
        for a in by_effect[AssignmentEffect.START].assignments
    }
    assert ended["YORGES AVALOS, DANTE AARON"] == "SERVICIO NACIONAL DE SANIDAD AGRARIA - SENASA"
    assert started["YORGES AVALOS, DANTE AARON"] == "MINISTERIO DE SALUD"
    assert ended["PANTOJA URIZAR GARFIAS, ANA TERESA"] == "MINISTERIO DE SALUD"
    assert started["PANTOJA URIZAR GARFIAS, ANA TERESA"] == "ACADEMIA DE LA MAGISTRATURA"


def test_tabular_collective_reads_the_identifier_column(extractor):
    """El DNI de una tabla va sin etiqueta al lado: la etiqueta es la cabecera.

    Publicarlo en una columna rotulada es declararlo con la misma claridad que
    "identificado con DNI N° …", y es la señal que permite reconocer que quien
    cesa en un artículo es quien es designado en el otro.
    """
    result = _extract_fixture("2540891-1")
    identifiers = {
        a.person.text_raw: [
            (i.scheme, i.value_raw, i.evidence.quoted_text) for i in a.person.identifiers
        ]
        for e in result.events
        for a in e.assignments
    }
    assert identifiers["YORGES AVALOS, DANTE AARON"][0][1] == "41345673"
    assert identifiers["PANTOJA URIZAR GARFIAS, ANA TERESA"][0][1] == "10274380"
    # La cita es literal: el valor tal cual aparece en su celda.
    assert identifiers["YORGES AVALOS, DANTE AARON"][0][2] == "41345673"


def test_a_table_without_a_declared_name_column_extracts_nobody(extractor):
    """Sin cabecera que diga qué columna es el nombre no se atribuye por posición.

    Perder una fila es recuperable; atribuir un nombre o un documento de
    identidad a quien no le corresponde, no. El fallo queda visible en warnings.
    """
    from kipu_knowledge.domain.parsed import TABLE_CELL_SEPARATOR

    article = "Artículo 1.- Designar en el cargo de Jefe a los siguientes servidores:"
    document = _doc_with_articles(article)
    document.sections.append(
        ParsedSection(
            section_type=SectionType.ARTICLE_TABLE_HEADER,
            order_index=1,
            text_raw=TABLE_CELL_SEPARATOR.join(["1", "columna sin rótulo"]),
            text_normalized="1 | columna sin rótulo",
        )
    )
    document.sections.append(
        ParsedSection(
            section_type=SectionType.ARTICLE_TABLE_ROW,
            order_index=2,
            text_raw=TABLE_CELL_SEPARATOR.join(["1", "JORGE PEREZ VEGA"]),
            text_normalized="1 | JORGE PEREZ VEGA",
        )
    )
    result = extractor.extract(document)
    assert result.events == []
    assert any("sin columna de nombre" in w for w in result.warnings)


def test_effective_date_clause_is_not_a_publication_notice(extractor):
    """ "Tendrán efectividad a partir del día siguiente de la publicación".

    Nombra la publicación, así que se clasificaba como aviso de publicación y
    el artículo que sí lo era quedaba sin clasificar. La distinción importa:
    esta cláusula es la que veta la regla de fecha legal (docs/adr/0007).
    """
    result = _extract_fixture("2540891-1")
    classes = {c.article_label: c.article_class for c in result.article_classifications}
    assert classes["Artículo 3.-"] == ArticleClass.EFFECTIVE_DATE_CLAUSE
    assert classes["Artículo 6.-"] == ArticleClass.PUBLICATION_NOTICE
