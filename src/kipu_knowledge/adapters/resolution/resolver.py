"""Resolución inicial de entidades basada en menciones y candidatos.

Política (regla 13 y sección de resolución):
- Las menciones se crean SIEMPRE antes que las entidades canónicas.
- Personas: nunca auto-merge por coincidencia de nombre. Si existen candidatos con
  el mismo nombre normalizado, la mención queda CANDIDATE_MATCH con tarea de
  revisión. Si no existe ningún candidato, crear una persona nueva es seguro
  (no fusiona nada) y la mención queda AUTO_LINKED.
- Variantes de persona: un nombre que omite un nombre de pila ("ELMER CUBA
  BUSTINZA" frente a "ELMER RAFAEL CUBA BUSTINZA") no coincide de forma exacta y
  antes creaba una persona nueva en silencio. Ahora se sigue creando la persona
  nueva —no se fusiona nada automáticamente— pero se emite PERSON_VARIANT_CHECK
  para que el posible duplicado sea visible en vez de invisible.
- Precedentes de identidad: una decisión humana previa se reutiliza en lugar de
  volver a preguntar, con dos alcances. Por cargo (nombre normalizado + cargo
  declarado) o como alias sin restricción de cargo, cuando el revisor declara
  que la grafía ES esa persona. El origen de la fusión sigue siendo humano y
  queda citado; el alias exige además un nombre discriminante.
- Identificadores declarados (DNI, carné de extranjería): si la fuente los
  expresa, vincular no infiere identidad sino que lee la que el documento
  afirma. Es la señal más fuerte y no requiere revisión.
- Oficios unipersonales: nombre normalizado idéntico + mismo cargo del que hay
  un solo titular a la vez (Presidencia de la República, una cartera
  ministerial) descarta la homonimia con dos señales independientes. Los cargos
  genéricos no califican y siguen yendo a revisión (ver domain/offices.py).
- Organizaciones: el nombre oficial normalizado idéntico enlaza (los nombres de
  órganos públicos funcionan como registro); variantes no idénticas crean una
  organización nueva y una tarea ORG_VARIANT_CHECK si hay similitud.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, distinct, func, or_, select
from sqlalchemy.orm import Session

from kipu_knowledge.adapters.db.models import (
    IdentityPrecedent,
    Organization,
    Person,
    PersonIdentifier,
    PersonMention,
)
from kipu_knowledge.domain.contracts import MatchProposal
from kipu_knowledge.domain.normalization import normalize_org_name, person_name_is_variant
from kipu_knowledge.domain.offices import singular_office


class SimpleEntityResolver:
    """EntityResolver por señales exactas. No usa fuzzy matching en el MVP."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def propose_matches(
        self, mention_text_normalized: str, context: dict[str, Any]
    ) -> list[MatchProposal]:
        kind = context.get("kind", "person")
        if kind == "person":
            return self._person_candidates(mention_text_normalized)
        if kind == "organization":
            return self._organization_candidates(mention_text_normalized)
        return []

    def _person_candidates(self, name_normalized: str) -> list[MatchProposal]:
        # Personas ya vinculadas a menciones con el mismo nombre normalizado.
        rows = self._session.execute(
            select(Person, PersonMention)
            .join(PersonMention, PersonMention.canonical_person_id == Person.id)
            .where(
                PersonMention.text_normalized == name_normalized,
                Person.merged_into_person_id.is_(None),
            )
        ).all()
        seen: dict[str, MatchProposal] = {}
        for person, _mention in rows:
            seen[person.id] = MatchProposal(
                entity_id=person.id,
                entity_label=person.preferred_name,
                score=0.8,
                rationale="mismo nombre normalizado en mención previa (requiere revisión humana)",
            )
        return list(seen.values())

    def variant_person_candidates(self, name_normalized: str) -> list[MatchProposal]:
        """Personas cuyo nombre es una grafía compatible sin ser idéntico.

        Se consulta solo cuando no hubo coincidencia exacta. Nunca vincula: el
        llamador crea igualmente una persona nueva y abre PERSON_VARIANT_CHECK.
        """
        rows = self._session.execute(
            select(Person, PersonMention.text_normalized)
            .join(PersonMention, PersonMention.canonical_person_id == Person.id)
            .where(Person.merged_into_person_id.is_(None))
        ).all()
        seen: dict[str, MatchProposal] = {}
        for person, other_normalized in rows:
            if person.id in seen or not person_name_is_variant(name_normalized, other_normalized):
                continue
            seen[person.id] = MatchProposal(
                entity_id=person.id,
                entity_label=person.preferred_name,
                score=0.5,
                rationale=(
                    f"apellidos idénticos y nombres de pila contenidos frente a "
                    f"'{other_normalized}' (posible duplicado; requiere revisión humana)"
                ),
            )
        return list(seen.values())

    def office_corroborated_persons(
        self, name_normalized: str, role_context: str | None
    ) -> list[Person]:
        """Personas con el mismo nombre normalizado en el mismo oficio unipersonal.

        Dos señales independientes del documento: el nombre y el cargo declarado
        junto a él. Cuando el cargo es un oficio del que hay un solo titular a la
        vez, la coincidencia de ambos descarta la homonimia sin intervención
        humana. Un cargo genérico ("Jefe Institucional") no es unipersonal y
        devuelve lista vacía, de modo que el caso cae en revisión.

        Devolver más de una persona significa que la corroboración se contradice
        a sí misma; el llamador debe tratarlo como conflicto, no elegir.
        """
        office = singular_office(role_context)
        if office is None:
            return []
        rows = self._session.execute(
            select(Person, PersonMention.role_context_normalized)
            .join(PersonMention, PersonMention.canonical_person_id == Person.id)
            .where(
                PersonMention.text_normalized == name_normalized,
                PersonMention.role_context_normalized.is_not(None),
                Person.merged_into_person_id.is_(None),
            )
        ).all()
        seen: dict[str, Person] = {}
        for person, other_role in rows:
            if singular_office(other_role) == office:
                seen[person.id] = person
        return list(seen.values())

    def persons_by_identifier(self, identifiers: Sequence[tuple[str, str]]) -> list[Person]:
        """Personas cuya mención declara alguno de estos (esquema, valor).

        Un DNI no es una pista sino un identificador: si la fuente lo expresa, la
        coincidencia no infiere identidad, la lee. Por eso este camino no pasa por
        revisión humana. Más de una persona para el mismo identificador es un
        conflicto de datos, no una elección.
        """
        if not identifiers:
            return []
        rows = self._session.execute(
            select(Person, PersonIdentifier.scheme, PersonIdentifier.value_normalized)
            .join(PersonMention, PersonMention.canonical_person_id == Person.id)
            .join(PersonIdentifier, PersonIdentifier.person_mention_id == PersonMention.id)
            .where(Person.merged_into_person_id.is_(None))
        ).all()
        wanted = {(str(scheme), value) for scheme, value in identifiers}
        seen: dict[str, Person] = {}
        for person, scheme, value in rows:
            if (str(scheme), value) in wanted:
                seen[person.id] = person
        return list(seen.values())

    def person_precedent(
        self, name_normalized: str, role_context: str | None
    ) -> IdentityPrecedent | None:
        """Precedente humano vigente aplicable a esta mención.

        Considera los dos alcances a la vez: el precedente por cargo, cuya clave
        es (nombre, cargo), y el alias declarado sobre la grafía, cuyo
        `role_context` es NULL y aplica aunque la mención no traiga cargo o traiga
        uno distinto. Si ambos existen y coinciden gana el específico, que es el
        que cita el cargo concreto y por tanto explica mejor la vinculación.

        Si los precedentes vigentes apuntan a personas distintas devuelve None
        para que el caso vuelva a revisión humana en lugar de elegir uno; esto
        cubre también la contradicción entre un alias y un precedente por cargo.
        """
        scopes: list[ColumnElement[bool]] = [IdentityPrecedent.role_context.is_(None)]
        if role_context:
            scopes.append(IdentityPrecedent.role_context == role_context)
        rows = (
            self._session.execute(
                select(IdentityPrecedent)
                .where(
                    IdentityPrecedent.subject_type == "person",
                    IdentityPrecedent.name_normalized == name_normalized,
                    or_(*scopes),
                    IdentityPrecedent.revoked_at.is_(None),
                )
                # False (cargo declarado) ordena antes que True (alias global).
                .order_by(IdentityPrecedent.role_context.is_(None), IdentityPrecedent.created_at)
            )
            .scalars()
            .all()
        )
        if not rows or len({row.person_id for row in rows}) > 1:
            return None
        return rows[0]

    def distinct_persons_for_name(self, name_normalized: str) -> int:
        """Cuántas personas canónicas vivas responden a esta grafía.

        Más de una significa que el nombre no es discriminante: no puede sostener
        un alias sin cargo, porque ese alias vincularía a cualquier homónimo
        futuro sin abrir tarea. Las personas absorbidas por una fusión no cuentan:
        ya no son un destino posible.
        """
        return int(
            self._session.execute(
                select(func.count(distinct(Person.id)))
                .select_from(Person)
                .join(PersonMention, PersonMention.canonical_person_id == Person.id)
                .where(
                    PersonMention.text_normalized == name_normalized,
                    Person.merged_into_person_id.is_(None),
                )
            ).scalar_one()
        )

    def person_aliases(self, person_id: str) -> list[str]:
        """Grafías normalizadas distintas con que las fuentes nombran a esta persona.

        No hay tabla de alias: el conjunto se deriva de sus menciones vinculadas,
        que es donde consta que un documento la llamó así. Sirve para búsqueda y
        presentación; autorizar una vinculación es asunto de IdentityPrecedent.
        """
        rows = (
            self._session.execute(
                select(distinct(PersonMention.text_normalized))
                .where(PersonMention.canonical_person_id == person_id)
                .order_by(PersonMention.text_normalized)
            )
            .scalars()
            .all()
        )
        return list(rows)

    def _organization_candidates(self, name_normalized: str) -> list[MatchProposal]:
        exact = (
            self._session.execute(
                select(Organization).where(Organization.name_normalized == name_normalized)
            )
            .scalars()
            .all()
        )
        return [
            MatchProposal(
                entity_id=org.id,
                entity_label=org.preferred_name,
                score=1.0,
                rationale="nombre oficial normalizado idéntico",
            )
            for org in exact
        ]

    @staticmethod
    def similar_org_exists(session: Session, name_normalized: str) -> Organization | None:
        """Heurística mínima de variantes: contención de nombres largos."""
        if len(name_normalized) < 12:
            return None
        rows = session.execute(select(Organization)).scalars().all()
        for org in rows:
            other = normalize_org_name(org.preferred_name)
            if other == name_normalized:
                continue
            if other in name_normalized or name_normalized in other:
                return org
        return None
