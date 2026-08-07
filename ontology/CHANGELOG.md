# Changelog de la ontología

Formato: [versión] - fecha - estado

## [0.3.0] - 2026-08-07 - STABLE

Fecha de inicio de efectos determinada por norma. Una fecha que el documento no
expresa puede estar fijada por una regla jurídica; el grafo la publica sin
confundirla con lo que el texto dice.

- `kipu:legalEffectFrom` / `kipu:legalEffectTo`: día en que el acto produce (o
  cesa de producir) efectos jurídicos según la norma aplicable. Convive con
  `kipu:effectiveFrom`, que sigue reflejando literalmente la fuente, y **no** la
  sustituye.
- `kipu:legalEffectFromStatus`: siempre `DERIVED`. La fecha se deriva de una
  regla citada, no de una inferencia.
- `kipu:legalBasis`, `kipu:legalBasisSource` y `kipu:determinationRule`: norma y
  artículo que la fijan, URL de la norma y versión de la regla que la produjo,
  para poder re-ejecutarla.
- Nueva forma SHACL `kipu:LegalEffectDateShape`: una fecha determinada sin norma
  citada ni versión de regla es una violación.

Normas catalogadas en esta versión: Ley N.º 27594 art. 6 (designación y
nombramiento) y RGLSC art. 233.3 (término). Los tipos de acto no catalogados
—encargos, delegaciones, responsabilidades adicionales— siguen sin determinar y
van a revisión humana. Ver docs/adr/0007.

## [0.2.0] - 2026-08-06 - STABLE

Resolución de identidad con señales corroborantes. La regla 13 prohíbe fusionar
menciones *solo por el nombre*; esta versión declara qué señales adicionales
autorizan vincular y deja constancia de cuál se usó en cada caso.

- `kipu:SingularOffice`: oficio del que existe un solo titular a la vez en todo
  el país (Presidencia de la República, Presidencia del Consejo de Ministros,
  cada cartera ministerial). Nombre idéntico + mismo oficio unipersonal descarta
  la homonimia sin intervención humana.
- `kipu:PersonIdentifier`: documento de identidad **declarado por la fuente**
  (DNI, carné de extranjería). Identifica en vez de sugerir, así que vincula con
  confianza total. No se proyecta a RDF: es dato personal y la ontología de
  personas se limita a información funcional pública.
- `kipu:IdentityPrecedent`: decisión humana de identidad reutilizable, con clave
  *nombre normalizado + cargo declarado*, trazable hasta su `ReviewDecision` y
  revocable.
- Estatus de resolución ampliados: `IDENTIFIER_LINKED`, `PRECEDENT_LINKED`,
  `OFFICE_CORROBORATED`. Cada vinculación automática registra en su afirmación
  qué señal la sostuvo.

El catálogo de oficios unipersonales se mantiene a mano (`domain/offices.py`) por
no existir fuente oficial legible por máquina de la estructura del Estado. Lo que
no coincide con el catálogo NO es unipersonal: la omisión manda el caso a
revisión humana, nunca a una fusión.

## [0.1.0] - 2026-08-06 - STABLE

Versión inicial del MVP.

- Módulos: core, legal, organization, people, positions, events, provenance, geography, topics.
- Reutilización: ELI (metadatos legales), PROV-O (procedencia), W3C ORG (organizaciones,
  puestos, membresías), SKOS (vocabularios controlados), SHACL (validación).
- Vocabulario de eventos de personal con 10 conceptos (designación, nombramiento,
  encargatura, responsabilidad adicional, aceptación de renuncia, conclusiones, etc.).
- Estatus epistemológico de fechas: EXPLICIT / DERIVED / INFERRED / NOT_STATED / CONDITIONAL.
- Shapes SHACL: evento↔documento↔evidencia, asignación↔persona/puesto,
  fechas inferidas con base, eventos END con asignación o marca unresolved,
  artefactos con SHA-256.

Gobernanza: cambios solo mediante el flujo DRAFT → propuesta → validación → aprobación
(ver docs/ontology-governance.md). La ontología no se modifica automáticamente.
