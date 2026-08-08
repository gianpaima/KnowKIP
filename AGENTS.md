# AGENTS.md — Kipu Knowledge

## Propósito

Plataforma de conocimiento verificable sobre actos publicados en fuentes oficiales.
MVP: resoluciones de personal público en El Peruano (Normas Legales) — captura
inmutable, segmentación, extracción determinista de eventos de personal,
afirmaciones con evidencia, exportación RDF/JSON-LD y revisión humana.

## Comandos

```bash
# Instalar (uv gestiona Python 3.12 y dependencias)
uv sync --extra pdf

# Servicios (PostgreSQL en host:5433, MinIO en 9000/9001)
docker compose up -d postgres minio

# Migraciones
uv run alembic upgrade head

# Ingesta / validación / exportación
uv run kipu ingest-fixture 2540861-1
uv run kipu ingest-date --date 2026-08-07 [--dry-run]   # recolección diaria (red)
uv run kipu crawl-report --date 2026-08-07              # bitácora de la corrida
uv run kipu validate --publication-code 2540861-1
uv run kipu export-rdf --publication-code 2540861-1
uv run kipu rebuild-projections

# API + UI de revisión (http://127.0.0.1:8000/docs y /review)
# Incluye el expediente por persona en /review/persons
uv run kipu serve

# Re-extraer un documento ya capturado con el extractor corregido (supersede)
uv run kipu reprocess --publication-code 2540891-1

# Calidad (todo debe pasar antes de un cambio)
uv run pytest
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy
```

## Convenciones arquitectónicas

- Monolito modular con capas: `domain` (contratos, enums, normalización, modelos
  Pydantic de extracción) → `application` (casos de uso) → `adapters` (BD, storage,
  fuente, parsing, extracción, RDF, búsqueda, resolución) → `interfaces` (API, CLI, UI).
- El núcleo NO importa FastAPI, MinIO, proveedores LLM ni otra infraestructura:
  depende solo de los Protocols de `domain/contracts.py`.
- PostgreSQL es la fuente operativa de verdad; el RDF es proyección derivada
  reconstruible (`kipu rebuild-projections`). Ver docs/adr/.
- Los bytes capturados viven en el ArtifactStore direccionado por contenido
  (`sha256/ab/cd/<hash>`) y nunca se sobrescriben.
- Enums de dominio en `domain/enums.py`; en BD son VARCHAR+CHECK (portables SQLite/PG).

## Reglas no negociables

1. **No inventar fechas ni identidades.** Si el documento no expresa una fecha,
   `effective_from` queda `NOT_STATED` con valor null; jamás se usa la fecha de
   publicación como fecha efectiva.
   Distinto es que una **norma** fije esa fecha: el artículo 6 de la Ley N.º 27594
   dispone que una designación o nombramiento surte efecto el día de su
   publicación en El Peruano, y el artículo 233.3 del RGLSC dice lo propio para el
   término. Eso se registra en `personnel_event.legal_effect_from` con la norma
   citada (`domain/legal_effect.py`, regla `legal-effect-date/1.0`), **nunca**
   encima de `effective_from`, y solo para los tipos de acto catalogados. Se veta
   —y vuelve a revisión humana— si la publicación autoritativa no es el diario
   oficial, si no consta su fecha o si la parte resolutiva posterga la vigencia.
   Ver docs/adr/0007.
   El nombre por sí solo NUNCA vincula dos menciones. Vincular
   exige una señal independiente del nombre, registrada en la afirmación:
   identificador declarado por la fuente (`IDENTIFIER_LINKED`), decisión humana
   previa (`PRECEDENT_LINKED`), oficio unipersonal (`OFFICE_CORROBORATED`) o
   corroboración por recital (`AFFECTED_PERSON_RECITAL_CORROBORATED`,
   `application/corroboration.py`): un considerando del mismo documento declara
   quién ejercía exactamente el puesto que el artículo concluye, con candidato
   único y sin instrumento previo contradictorio. Señales que se contradicen
   abren `EXTRACTION_CONFLICT`, no eligen. El catálogo de oficios unipersonales
   (`domain/offices.py`) es restrictivo por defecto: lo no catalogado va a
   revisión humana.
   La prohibición recae sobre la **inferencia del sistema**, no sobre lo que un
   humano afirma. Un revisor puede declarar un alias —"esta grafía es esta
   persona, con el cargo que sea"— y entonces la señal independiente es su
   decisión, citada en `IdentityPrecedent` con `role_context` NULL y revocable.
   Para que ese alias no reintroduzca la inferencia por la puerta de atrás se
   exige que la grafía sea discriminante: si ya corresponde a más de una
   persona, se rechaza (`ReviewService._record_precedent`).
2. **Toda afirmación debe tener evidencia**: un `EvidenceSpan` con cita literal,
   hash y localización en una `ArtifactVersion` con SHA-256.
3. El documento original es evidencia; los datos derivados son afirmaciones
   versionadas. Las correcciones se hacen con supersede, nunca borrando.
4. Captura antes que parsing; el parser falla explícitamente ante HTML contaminado
   o campos faltantes.
5. `LIVE_SOURCE_ENABLED=false` por defecto; el scraping real exige revisar
   docs/source-policy.md primero. El descubrimiento por fecha
   (`kipu ingest-date`) recorre solo el índice de la fecha pedida y **verifica
   que la página responda a esa fecha y que lo recolectado cuadre con el total
   que la fuente declara**: un día incompleto falla en vez de pasar por bueno.
   Ningún fallo transitorio se reintenta dentro de la corrida; queda
   `RETRY_PENDING` para `kipu retry-pending`.
6. **Los identificadores personales no se publican.** El DNI sirve para resolver
   identidad; nunca se proyecta a RDF ni se expone por la API. `kipu:Person`
   solo lleva información funcional pública. Excepción deliberada: la UI interna
   de revisión (`/review/artifacts/{id}/raw`) sirve los bytes capturados tal
   cual —pueden contener DNI, porque la captura ES la evidencia— y por eso ese
   endpoint vive solo bajo `/review`, jamás en el API público `/v1`. Toda
   respuesta HTML de ese endpoint viaja con CSP `sandbox` (contenido de
   terceros: nunca ejecuta scripts ni carga recursos remotos en nuestro origin).
7. **Nada descubierto se descarta en silencio.** Todo dispositivo que el índice
   declara queda en `crawl_item` con su veredicto de relevancia, la regla que lo
   decidió y su motivo, se ingiera o no. El filtro
   (`domain/relevance.py`, `personnel-relevance/1.0`) solo excluye lo que
   encabeza con un verbo del catálogo negativo; lo no catalogado se ingiere. La
   sumilla del catálogo no es evidencia de nada: la escribe el buscador, no la
   norma, y por eso no produce afirmaciones. Ver docs/adr/0008.
8. **Una ficha vacía no significa que no haya nada.** El expediente por persona
   (`/review/persons`, `application/person_dossier.py`) declara siempre de
   cuántos documentos se construyó y cuáles de ellos se leyeron sin extraer
   ningún hecho. Buscar por nombre devuelve fichas **candidatas** y avisa
   cuando una grafía responde a más de una persona: encontrar no es
   identificar, y agrupar por nombre reintroduciría por la interfaz la
   inferencia que la regla 1 prohíbe. Lo que un acto atribuye va separado de la
   capacidad con que alguien firma —"firma como Ministra" no es "fue designada
   Ministra"— y las menciones con la misma grafía sin vincular se listan
   aparte, nunca dentro. Nada derivado sin regla citada y evidencia; no hay
   texto redactado por modelo sobre personas reales.
9. **Re-extraer no borra decisiones humanas.** `kipu reprocess` supersede las
   afirmaciones (nunca las borra), desancla los `EvidenceSpan` de las secciones
   que reconstruye —la cita y su sha256 siguen siendo verificables contra el
   artefacto—, reapunta los `IdentityPrecedent` a la mención equivalente del
   documento nuevo, arrastra los `document_source` corroborantes que enlazó un
   operador y retira las fichas de persona que la extracción anterior creó y
   esta ya no sostiene. Si un precedente se queda sin mención equivalente, se
   abre tarea de revisión en vez de resolverlo solo.

## Política de pruebas y revisión

- Sin red en pruebas: todo corre sobre fixtures congelados y SQLite en memoria.
- Golden tests (`tests/golden/`): un JSON esperado por caso A–H. Si cambias el
  parser o el extractor, regenera los esperados SOLO tras revisar el diff a mano.
- Invariantes (`tests/invariants/`): codifican las reglas de la sección anterior;
  no las debilites para hacer pasar un cambio.
- Extracciones de identidad, fechas inferidas y conflictos van a revisión humana
  (ReviewTask); una decisión humana crea ReviewDecision y nunca borra la extracción.
- CI (GitHub Actions) ejecuta lint + format + mypy + pytest sin acceso a fuentes vivas.
