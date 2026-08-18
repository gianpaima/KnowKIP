# Diseño: contexto web atribuido (prensa, redes sociales y otras fuentes)

Estado: **política aprobada y bases implementadas** (2026-08-18): enums,
tablas `web_document` / `web_person_mention` / `web_reference`, vocabulario de
predicados `web:*` y migración. Pendiente: adaptadores de descubrimiento y
captura, extracción, UI. La política que lo rige está en
`docs/source-policy.md` (secciones "Clasificación y finalidad" y "Fuentes de
contexto web"), reestructurada en la misma fecha.

## 1. Qué hace

Dada una persona que ya está en la base con un cargo respaldado por una
resolución, buscar en fuentes web admitidas (prensa formal peruana, portales
institucionales, cuentas públicas de redes sociales) contenido sobre esa
persona, capturarlo con el mismo rigor probatorio que el corpus oficial y
asociarlo como **contexto atribuido** para construir su perfil público:

1. Siempre se puede volver a la fuente (URL + copia inmutable + cita textual).
2. Lo que el sistema "sabe" por contexto queda estructurado y atribuido
   ("RPP afirma que…"), nunca fundido con el registro funcional oficial.
3. La estructura es un grafo de afirmaciones con proveniencia (estilo
   declaración+referencia de Wikidata), base de construcciones futuras.

### Prueba de concepto verificada (2026-08-18)

Persona de la base: **César Alfonso Luna Victoria León**
(`person 9fd25b5b280a41c483921d4778cebd50`), Superintendente Nacional de
Aduanas y de Administración Tributaria, designado por **RS N° 027-2026-EF**
(publicación 2540905-3, El Peruano 2026-08-06).

La búsqueda `"Luna Victoria" SUNAT superintendente` devolvió cobertura en El
Comercio, Gestión, La República, RPP, Caretas y gob.pe. El artículo de RPP
(13-08-2026, Luz Alarcón) contiene, citable textualmente:

- «designado … mediante Resolución Suprema 027-2026-EF» → **ancla verificable
  al acto ya ingerido** (`web_reference`).
- «abogado tributarista de la Pontificia Universidad Católica del Perú (PUCP)»
  → `web:education` / `web:profession`.
- «ministro del extinto Ministerio de Industria, Turismo, Integración y
  Negociaciones Comerciales Internacionales (Mitinci) y ministro de Pesquería
  en los años 90» → `web:prior_public_role` ×2 (anteriores al corpus: dirigen
  la futura ingesta histórica).
- «consultor empresarial y fue director independiente de Pesquera Exalmar»
  → `web:private_sector_role`.

## 2. Principio rector: contexto, no autoridad

- `SourceAuthority` gana `PRESS`, `SOCIAL_MEDIA` y `OTHER_WEB`: peso jurídico
  **nulo**. Un medio o una cuenta hablan de actos; no los publican.
- De una fuente de contexto **jamás** se crean ni modifican `PersonnelEvent`,
  `RoleAssignment`, `Position`, `Organization` ni fechas de efectos. Ni con
  revisión humana: si el contexto revela un acto oficial que falta, el camino
  es localizar e ingerir la norma.
- Todo lo extraído es una **afirmación atribuida**: el sujeto ontológico es
  "la fuente F afirmó X sobre la persona P en el documento W". En RDF,
  `prov:wasAttributedTo`, siempre reificado.
- Una contradicción con el corpus oficial abre `WEB_DISCREPANCY` y ahí muere
  su efecto automático.

## 3. Modelo de datos (implementado)

La cadena CAS → `Artifact`/`ArtifactVersion` → `EvidenceSpan` → `Assertion`
es agnóstica de la fuente y se reutiliza tal cual.

### 3.1 Fuente y publicación

- Una fila `source_system` por medio o plataforma (`authority=PRESS` /
  `SOCIAL_MEDIA` / `OTHER_WEB`), nunca un cajón genérico: la atribución es por
  publicador concreto. `policy_status` refleja su inspección (§ política).
- Un `publication_item` por página capturada, `source_series="WEB"` y
  `publication_code = sha256(url_canónica)[:16]`
  (`domain/web_context.py::web_publication_code`; la canonicalización quita
  fragmento y parámetros de tracking). La unicidad existente
  `(source_system, source_series, publication_code)` dedupe por URL dentro de
  cada publicador. `canonical_url` guarda la URL real.
- Captura con el mismo `PoliteFetcher` hacia el mismo CAS; re-capturas del
  mismo documento (correcciones, ediciones) son versiones nuevas del mismo
  artefacto.

### 3.2 `web_document` (análogo de `legal_document`)

Qué es la página según su propia captura, parseada de los metadatos que las
fuentes publican (JSON-LD `NewsArticle`/`SocialMediaPosting` de schema.org,
OpenGraph como fallback):

- `kind`: `NEWS_ARTICLE` | `SOCIAL_POST` | `SOCIAL_PROFILE` |
  `INSTITUTIONAL_NEWS` | `OTHER` (`WebDocumentKind`).
- `headline_raw`, `published_at_raw`/`published_at`, `modified_at_raw`,
  `author_raw` (byline), `account_raw` (handle de la cuenta en posts y
  perfiles), `section_raw`, `language`.
- `body_scope`: `FULL` | `PARTIAL_PAYWALL` | `METADATA_ONLY`
  (`WebBodyScope`) — el sistema sabe si tiene el cuerpo entero o un recorte,
  para no citar como completo lo que no lo es.
- `parsed_from_artifact_version_id`: la captura exacta de la que salió todo.

### 3.3 `web_person_mention`

Vínculo documento ↔ persona con el patrón de `person_mention`:
`text_raw`/`text_normalized`, `role_context_raw` (el cargo con que la fuente
la nombra), `evidence_span_id`, `canonical_person_id`, `resolution_status`
(reutiliza `ResolutionStatus`), `matched_by` (la señal literal que sostuvo la
vinculación), `identity_precedent_id` (los precedentes de identidad humanos
aplican también aquí).

**Guard de homonimia**: la vinculación automática exige nombre + al menos una
señal corroborante en el mismo documento — el número de una norma ya ingerida
que involucra a esa persona, o cargo+organización coincidentes con una
`RoleAssignment` vigente en la fecha del documento. Nombre solo ⇒ tarea
`WEB_MENTION_RESOLUTION` y la mención queda `UNRESOLVED`. Contaminar el corpus
curado de identidades con un homónimo sería el peor fallo posible de este
mecanismo.

### 3.4 `web_reference`: ancla al acto oficial

Análoga a `document_reference`: cuando el documento cita una norma
("Resolución Suprema 027-2026-EF") se registra con su cita, su
`target_number_raw` y — si el número normalizado coincide con un
`legal_document` ingerido — `target_document_id`. Es la única vinculación
totalmente automática permitida, porque es mecánica y verificable.

Esta ancla convierte "artículo sobre Juan Pérez" en "artículo que **cita el
mismo acto** que respalda la asignación de Juan Pérez", y da al dossier su
agrupador natural (cobertura por acto).

### 3.5 Afirmaciones: `assertion` con predicados `web:*`

Cada dato extraído es una fila en `assertion`:

- `extraction_run_id`: corrida normal; `ExtractionRun` ya versiona parser,
  extractor, ontología y modelo/prompt del LLM.
- `subject_type='person'`, `subject_id` = persona canónica (solo con mención
  resuelta; si no, la afirmación espera).
- `predicate` del vocabulario de `domain/web_context.py`, en espacio de
  nombres `web:` que impide confundirlas con hechos oficiales:
  - `web:cites_official_act` — cita una norma (objeto: la `web_reference`)
  - `web:covers_event` — cubre un evento de la función (juramentación…)
  - `web:prior_public_role` — cargo público previo declarado por la fuente
  - `web:private_sector_role` — actividad privada declarada
  - `web:education` / `web:profession` — formación / profesión declaradas
  - `web:public_account` — cuenta pública atribuida a la persona
  - `web:public_statement` — declaración pública de la persona
  - `web:reported_assessment` — señalamiento publicado (investigación,
    cuestionamiento), **solo** como dicho-de-la-fuente
  - `web:other_context` — lo demás, sin forzar tipología
- `object_value_json`: estructura ligera por tipo, **siempre acompañando a la
  cita, nunca en su lugar**.
- `evidence_span_id` obligatorio (la CHECK existente lo impone); la cita es
  literal de la captura, con offsets y sha256 del fragmento.
- `review_status`: nace `CANDIDATE`; puede llegar a `HUMAN_ACCEPTED` **como
  afirmación atribuida**. No existe camino por el que se transforme en
  `RoleAssignment` ni en ningún hecho del registro funcional.

Qué no genera afirmaciones (política vigente): datos sensibles, vida privada,
contenido no público, y veredictos propios del sistema. `web:reported_assessment`
registra que la fuente publicó un señalamiento; el sistema no lo evalúa.

### 3.6 Sin resumen libre

No se guarda "un resumen" del documento. La contextualización ES el conjunto
de afirmaciones tipadas con sus citas: un resumen generado sería contenido
nuestro sin fuente citable. El dossier renderiza las afirmaciones agrupadas
(por acto citado, por tipo) y eso cumple la función del resumen.

## 4. Descubrimiento (pendiente de implementar)

Comando MVP: `kipu enrich-person <person_id> [--since FECHA] [--outlets ...]`.
A demanda; nada corre solo al principio.

Consultas por persona, desde la base: variantes de nombre
(`person.preferred_name` + `text_normalized` de sus menciones, apellidos
compuestos entrecomillados), señales de cargo (sigla de la organización,
sustantivo del cargo), número de la norma como consulta de alta precisión, y
ventana temporal (por defecto `valid_from`/`legal_effect_from` − 7 días →
hoy).

Dos capas: (1) buscador restringido a la lista blanca — el resultado es
bitácora `CrawlRun`/`CrawlItem` con `source_series="WEB"`: qué consulta se
lanzó, qué URLs devolvió, cuáles se capturaron y por qué se descartaron las
demás (sin zonas mudas, como el índice diario); (2) captura de cada URL
aceptada con `PoliteFetcher`. Filtro previo: dominio en lista blanca, URL del
publicador (no agregadores), título/snippet con alguna variante del nombre.

## 5. Extracción (pendiente de implementar)

1. **Metadatos**: JSON-LD schema.org; fallback OpenGraph/`<title>`. Determinista.
2. **Menciones**: variantes del nombre sobre el texto extraído, párrafo
   completo como `EvidenceSpan`, guard de homonimia (§3.3).
3. **Referencias a normas**: mismos patrones de `document_reference`.
4. **Clasificador de afirmaciones `web:*`**: aquí entra un LLM (la prosa
   periodística no se domestica con patrones), dentro de `ExtractionRun` con
   `model_provider/model_name/prompt_version`. Cada afirmación debe traer la
   cita literal; una cita que no aparezca byte a byte en la captura invalida
   la afirmación — verificación mecánica post-LLM, no confianza en el modelo.

Los pasos 1–3 solos ya producen el ancla persona↔acto↔documento con evidencia:
la primera entrega no necesita LLM.

## 6. Volver a la fuente: contrato de trazabilidad

Para cada afirmación, el sistema reconstruye y muestra: la cita (sha256 +
offsets), la captura inmutable (CAS, `captured_at`, cabeceras — verificable
con `verified_bytes`), la URL canónica y el publicador, y la corrida que la
extrajo (versiones de parser/extractor/modelo/prompt/ontología).

Contra el deterioro de enlaces (la web borra y reescribe): un futuro
`kipu verify-web-links` revalida por lotes con petición condicional
(ETag/Last-Modified ya se guardan); si el contenido cambió, nueva
`ArtifactVersion`, y si la cita ya no aparece, la afirmación se marca — la
evidencia sigue siendo la captura original, pero el lector debe saberlo.
Enviar URLs a archive.org como tercer respaldo queda fuera del MVP: publica a
un tercero qué personas estamos mirando; decisión aparte si se quiere.

## 7. Discrepancias y revisión

- `WEB_MENTION_RESOLUTION` — homonimia/identidad (§3.3).
- `WEB_DISCREPANCY` — el documento afirma algo incompatible con el corpus
  oficial. El sistema no elige, y a diferencia de `SOURCE_DISCREPANCY` la
  resolución jamás toca el registro funcional: se descarta la lectura, se
  anota la divergencia como afirmación atribuida, o dispara la búsqueda de una
  norma no ingerida.

## 8. Orden de implementación

1. ✔ Política reestructurada (`source-policy.md`).
2. ✔ Esquema y modelo: enums (`SourceAuthority.PRESS/SOCIAL_MEDIA/OTHER_WEB`,
   `WebDocumentKind`, `WebBodyScope`, `ReviewTaskType.WEB_*`), tablas
   `web_document`/`web_person_mention`/`web_reference`, vocabulario
   `domain/web_context.py`, migración alembic.
3. ✔ Inspección y alta de RPP y La República (2026-08-18; detalle en
   `source-policy.md`, catálogo ejecutable en `domain/web_sources.py`).
   Pendientes: El Comercio y Gestión (con `body_scope` de muro de pago);
   plataformas sociales tras inspección de condiciones.
4. ✔ `kipu enrich-person` sin LLM (`application/web_enrich.py`): lista blanca
   → captura → metadatos JSON-LD → menciones (con detector determinista de
   formas cortas del nombre: subsecuencia ordenada con apellido, guion
   editorial tratado como espacio) → guard de homonimia (señal A: norma
   citada; señal B: cargo/entidad de asignación vigente en el párrafo) →
   `web_reference` → afirmación `web:cites_official_act`. Verificado en vivo
   con 3 artículos reales (RPP ×2, La República) sobre el caso SUNAT.
5. ✔ Clasificador LLM (`adapters/extraction/web_llm.py` +
   `application/web_claims.py`): vocabulario cerrado, cita verificada byte a
   byte contra la captura, supersede al re-clasificar, proveniencia
   modelo/prompt en la corrida. `kipu classify-web-context`; requiere
   LLM_EXTRACTOR_ENABLED + ANTHROPIC_API_KEY (apagado por defecto).
6. ✔ Sección "Contexto público (prensa y web)" en el dossier
   (`person_dossier._web_context` + `person_detail.html`): una entrada por
   documento con sus menciones, normas citadas (con ancla al acto ingerido) y
   afirmaciones con cita; lo no vinculado va aparte.
7. (Futuro) disparo automático al ingerir designaciones de alto perfil;
   `verify-web-links` programado; descubrimiento vía RSS/sitemaps permitidos;
   detección de discrepancias que pueble `WEB_DISCREPANCY`; propuesta de
   ontología (`ontology/decisions/0002-contexto-web-atribuido.md`) por el
   flujo de gobernanza.

## 9. Por qué esto escala

Cada persona termina con dos capas separadas y enlazadas: el **registro
funcional** (actos oficiales, fechas, sucesión — lo que ya existe) y el
**perfil público atribuido** (quién publicó qué sobre esa persona, cuándo, con
qué palabras, citando qué acto). Es un grafo de conocimiento con proveniencia
consultable y exportable: RAG con citas reales, líneas de tiempo
persona-acto-cobertura, detección de cargos previos que dirige la ingesta
histórica. La invariante que lo sostiene: ninguna afirmación entra sin cita,
sin captura y sin atribución.
