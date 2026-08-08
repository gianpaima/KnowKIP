"""Vocabularios controlados del dominio.

Se usan enums de cadena para que la persistencia sea portable (VARCHAR + CHECK)
y la proyección RDF pueda mapearlos a conceptos SKOS.
"""

from __future__ import annotations

from enum import StrEnum


class RepresentationType(StrEnum):
    HTML = "HTML"
    PDF = "PDF"
    ISSUE_PDF = "ISSUE_PDF"
    IMAGE = "IMAGE"
    # Índice del día: la página de resultados que enumera los dispositivos de una
    # fecha. Cuelga del ítem de la edición, no de un dispositivo, y sus versiones
    # son las páginas del recorrido (cada una con su requested_url), no
    # correcciones sucesivas de un mismo documento.
    LISTING = "LISTING"


class SourceAuthority(StrEnum):
    """Peso jurídico de quien publica, que no es lo mismo que su fiabilidad.

    En Perú la publicación en el diario oficial es la que produce efectos: el
    portal de la propia entidad emisora reproduce el acto, no lo constituye. El
    sistema necesita el rango explícito porque una discrepancia entre fuentes no
    se resuelve por mayoría ni por fecha de captura, sino por autoridad.
    """

    OFFICIAL_GAZETTE = "OFFICIAL_GAZETTE"  # El Peruano (Normas Legales)
    ISSUING_ENTITY = "ISSUING_ENTITY"  # portal de la entidad que emitió el acto
    MIRROR = "MIRROR"  # recopilador o copia de terceros


class DocumentSourceRole(StrEnum):
    """Papel de una publicación concreta respecto de un documento.

    Solo una puede ser AUTHORITATIVE: es de la que se extrae. Las demás sirven
    para respaldo y contraste, y jamás auto-aceptan hechos que la autoritativa
    no exprese.
    """

    AUTHORITATIVE = "AUTHORITATIVE"
    CORROBORATING = "CORROBORATING"


class Relevance(StrEnum):
    """Si un dispositivo del índice diario entra o no en el alcance del sistema.

    UNDECIDED no es un empate: es la respuesta honesta cuando la sumilla no
    empieza por ningún verbo catalogado. Se ingiere igual, porque descartar por
    desconocimiento perdería documentos sin dejar rastro de la decisión.
    """

    RELEVANT = "RELEVANT"
    NOT_RELEVANT = "NOT_RELEVANT"
    UNDECIDED = "UNDECIDED"


class CrawlItemStatus(StrEnum):
    """En qué quedó cada dispositivo descubierto en una corrida.

    RETRY_PENDING existe porque en esta fuente se observó un 404 transitorio
    (2026-08-07): el fallo se registra para reintentarlo con un comando
    explícito, nunca dentro del mismo recorrido.
    """

    DISCOVERED = "DISCOVERED"  # visto en el índice; aún no se decidió nada
    SKIPPED_NOT_RELEVANT = "SKIPPED_NOT_RELEVANT"
    ALREADY_PRESENT = "ALREADY_PRESENT"  # ya estaba ingerido de antes
    INGESTED = "INGESTED"
    INGESTED_PDF_PENDING = "INGESTED_PDF_PENDING"  # texto ingerido, falta respaldar el PDF
    RETRY_PENDING = "RETRY_PENDING"  # fallo transitorio: se reintenta aparte
    FAILED = "FAILED"  # fallo que no se arregla reintentando (parseo, contaminación)


class SectionType(StrEnum):
    SUMMARY = "SUMMARY"  # sumilla
    DOC_TYPE = "DOC_TYPE"
    DOC_NUMBER = "DOC_NUMBER"
    ISSUE_LINE = "ISSUE_LINE"  # "Lima, 5 de agosto de 2026"
    VISTOS = "VISTOS"
    CONSIDERANDO = "CONSIDERANDO"
    RESOLVE_HEADER = "RESOLVE_HEADER"  # "SE RESUELVE:"
    ARTICLE = "ARTICLE"
    # Cuerpo de un artículo cuyo encabezado es solo un título ("Artículo 1.-
    # Designación") y cuya parte dispositiva viene en el párrafo siguiente. Es
    # una sección propia y no parte del artículo para que la evidencia siga
    # anclada al párrafo que realmente dice el hecho.
    ARTICLE_BODY = "ARTICLE_BODY"
    ARTICLE_LIST_ITEM = "ARTICLE_LIST_ITEM"  # "- Nombre Apellido" dentro de un artículo colectivo
    # Designaciones colectivas publicadas en tabla. Se conserva la fila entera
    # con sus celdas en orden: separarlas en párrafos sueltos pierde qué nombre
    # va con qué entidad y con qué documento de identidad.
    ARTICLE_TABLE_HEADER = "ARTICLE_TABLE_HEADER"
    ARTICLE_TABLE_ROW = "ARTICLE_TABLE_ROW"
    CLOSING = "CLOSING"  # "Regístrese, comuníquese..."
    SIGNATURE = "SIGNATURE"
    PUBLICATION_CODE = "PUBLICATION_CODE"
    ANNEX = "ANNEX"
    OTHER = "OTHER"


class ReferenceType(StrEnum):
    INTERNAL_SEEN_DOCUMENT = "INTERNAL_SEEN_DOCUMENT"
    NORMATIVE_CITATION = "NORMATIVE_CITATION"
    PRIOR_APPOINTMENT = "PRIOR_APPOINTMENT"
    MODIFIES = "MODIFIES"
    REPEALS = "REPEALS"
    CORRECTS = "CORRECTS"
    OTHER = "OTHER"


class ExtractionStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReviewStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    HUMAN_ACCEPTED = "HUMAN_ACCEPTED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ResolutionStatus(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    CANDIDATE_MATCH = "CANDIDATE_MATCH"
    AUTO_LINKED = "AUTO_LINKED"
    IDENTIFIER_LINKED = "IDENTIFIER_LINKED"  # identificador declarado por la fuente
    PRECEDENT_LINKED = "PRECEDENT_LINKED"  # vinculada por decisión humana previa
    OFFICE_CORROBORATED = "OFFICE_CORROBORATED"  # nombre + oficio unipersonal coinciden
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    MERGED = "MERGED"
    SPLIT = "SPLIT"


class EventType(StrEnum):
    DESIGNATION = "DESIGNATION"
    APPOINTMENT = "APPOINTMENT"
    ACTING_ASSIGNMENT = "ACTING_ASSIGNMENT"
    ADDITIONAL_RESPONSIBILITY = "ADDITIONAL_RESPONSIBILITY"
    ACCEPT_RESIGNATION = "ACCEPT_RESIGNATION"
    END_DESIGNATION = "END_DESIGNATION"
    END_ACTING_ASSIGNMENT = "END_ACTING_ASSIGNMENT"
    TERMINATION = "TERMINATION"
    DELEGATION = "DELEGATION"
    OTHER_PERSONNEL_ACTION = "OTHER_PERSONNEL_ACTION"


class AssignmentEffect(StrEnum):
    START = "START"
    END = "END"
    MODIFY = "MODIFY"
    NONE = "NONE"


class DateStatus(StrEnum):
    EXPLICIT = "EXPLICIT"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"
    NOT_STATED = "NOT_STATED"
    CONDITIONAL = "CONDITIONAL"


class AssignmentKind(StrEnum):
    TITULAR = "TITULAR"
    ACTING = "ACTING"
    TEMPORARY = "TEMPORARY"
    ADDITIONAL_RESPONSIBILITY = "ADDITIONAL_RESPONSIBILITY"
    BOARD_MEMBERSHIP = "BOARD_MEMBERSHIP"
    UNKNOWN = "UNKNOWN"


class ArticleClass(StrEnum):
    """Clasificación funcional de un artículo resolutivo."""

    PERSONNEL_EVENT = "PERSONNEL_EVENT"
    DERIVED_OBLIGATION = "DERIVED_OBLIGATION"  # p.ej. declaraciones juradas
    PUBLICATION_NOTICE = "PUBLICATION_NOTICE"  # encargos de publicación web
    # Cláusula que fija cuándo empieza a producir efectos el acto ("tendrán
    # efectividad a partir del día siguiente de la publicación"). Se distingue
    # del aviso de publicación porque es la que veta la regla de fecha legal
    # (docs/adr/0007): confundirlas escondía la razón del veto.
    EFFECTIVE_DATE_CLAUSE = "EFFECTIVE_DATE_CLAUSE"
    NOTIFICATION = "NOTIFICATION"
    COUNTERSIGNATURE = "COUNTERSIGNATURE"  # "es refrendada por..."
    OTHER = "OTHER"


class ParticipantRole(StrEnum):
    APPOINTEE = "APPOINTEE"
    RESIGNING_PERSON = "RESIGNING_PERSON"
    AFFECTED_PERSON = "AFFECTED_PERSON"
    AFFECTED_PERSON_RECITAL_CANDIDATE = "AFFECTED_PERSON_RECITAL_CANDIDATE"
    # Candidato de considerando corroborado mecánicamente: mismo puesto que el
    # artículo concluye, candidato único y sin instrumento contradictorio.
    AFFECTED_PERSON_RECITAL_CORROBORATED = "AFFECTED_PERSON_RECITAL_CORROBORATED"
    RETURNING_HOLDER = "RETURNING_HOLDER"  # persona cuyo retorno termina un encargo
    ISSUING_ORGANIZATION = "ISSUING_ORGANIZATION"


class ReviewTaskType(StrEnum):
    ENTITY_RESOLUTION = "ENTITY_RESOLUTION"
    EFFECTIVE_DATE_UNSTATED = "EFFECTIVE_DATE_UNSTATED"
    LINK_AFFECTED_ASSIGNMENT = "LINK_AFFECTED_ASSIGNMENT"
    POSITION_ORG_UNRESOLVED = "POSITION_ORG_UNRESOLVED"
    ORG_VARIANT_CHECK = "ORG_VARIANT_CHECK"
    PERSON_VARIANT_CHECK = "PERSON_VARIANT_CHECK"
    ONTOLOGY_CANDIDATE = "ONTOLOGY_CANDIDATE"
    EXTRACTION_CONFLICT = "EXTRACTION_CONFLICT"
    # Dos publicaciones del mismo acto dicen cosas distintas. El sistema no elige:
    # ni siquiera cuando una es el diario oficial, porque una divergencia puede
    # significar fe de erratas, versión posterior o captura equivocada.
    SOURCE_DISCREPANCY = "SOURCE_DISCREPANCY"


class ReviewTaskStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class DecisionAction(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    LINK_ENTITY = "LINK_ENTITY"
    CREATE_ENTITY = "CREATE_ENTITY"
    SPLIT_ENTITY = "SPLIT_ENTITY"
    MARK_DATE_NOT_STATED = "MARK_DATE_NOT_STATED"
    # Aplicar la fecha de inicio de efectos que la norma determina (ver
    # domain/legal_effect.py). El revisor no escribe la fecha: confirma que la
    # regla aplica y el sistema la vuelve a derivar de los datos capturados.
    APPLY_LEGAL_EFFECT_DATE = "APPLY_LEGAL_EFFECT_DATE"
    RESOLVE_POSITION = "RESOLVE_POSITION"
    DISMISS = "DISMISS"


class IdentifierScheme(StrEnum):
    """Esquemas de identificación de personas que las fuentes pueden declarar."""

    DNI = "DNI"
    CARNE_EXTRANJERIA = "CARNE_EXTRANJERIA"
    PASAPORTE = "PASAPORTE"
    RUC = "RUC"


class MandateType(StrEnum):
    CONSTITUTIONAL_PERIOD = "CONSTITUTIONAL_PERIOD"
    INSTITUTIONAL_PERIOD = "INSTITUTIONAL_PERIOD"
    OTHER = "OTHER"


class DocumentTypeCode(StrEnum):
    RESOLUCION_MINISTERIAL = "RESOLUCION_MINISTERIAL"
    RESOLUCION_SUPREMA = "RESOLUCION_SUPREMA"
    RESOLUCION_JEFATURAL = "RESOLUCION_JEFATURAL"
    RESOLUCION_DE_INTENDENCIA = "RESOLUCION_DE_INTENDENCIA"
    RESOLUCION_DIRECTORAL = "RESOLUCION_DIRECTORAL"
    OTHER = "OTHER"
