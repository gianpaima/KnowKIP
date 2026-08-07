# Kipu Knowledge

Plataforma de conocimiento verificable sobre resoluciones publicadas en El
Peruano (Normas Legales). Captura inmutable de evidencia, segmentación,
extracción determinista de eventos de personal público, afirmaciones con
procedencia, exportación RDF/JSON-LD validada con SHACL, API de consulta y
revisión humana mínima.

**Principio rector**: el documento original es evidencia; los datos derivados
son afirmaciones versionadas que nunca la reemplazan. Si la fuente no expresa
una fecha o una identidad, el sistema responde con incertidumbre explicable —
no inventa (ver `docs/adr/0006`).

## Requisitos

- [uv](https://docs.astral.sh/uv/) (gestiona Python 3.12 automáticamente)
- Docker (PostgreSQL y MinIO)

## Puesta en marcha

Los comandos son idénticos en PowerShell y en shells POSIX salvo donde se indica.

### 1. Instalar dependencias

```powershell
uv sync --extra pdf     # el extra pdf (PyMuPDF) es opcional
```

### 2. Iniciar PostgreSQL y MinIO

```powershell
docker compose up -d postgres minio
```

PostgreSQL queda en `localhost:5433` (5433 para no chocar con instalaciones
nativas) y MinIO en `localhost:9000` (consola: 9001).

Configuración opcional: copiar la plantilla de entorno.

```powershell
Copy-Item .env.example .env      # PowerShell
```
```bash
cp .env.example .env             # POSIX
```

### 3. Ejecutar migraciones

```powershell
uv run alembic upgrade head
```

### 4. Ingerir el fixture 2540861-1

```powershell
uv run kipu ingest-fixture 2540861-1
```

Otros comandos de ingesta: `kipu ingest-file <ruta.html>`,
`kipu ingest-device <url>` (requiere `LIVE_SOURCE_ENABLED=true`; leer antes
`docs/source-policy.md`), `kipu reprocess --publication-code <código>`.

`kipu backfill-source-links [--dry-run]` re-deriva de las capturas ya guardadas
en el CAS la URL del PDF que cada dispositivo declara, más el sistema fuente y
el vínculo documento↔publicación, para las filas ingeridas antes de que esos
datos existieran. No toca la red.

`kipu apply-legal-effect-dates [--dry-run]` determina la fecha de inicio de
efectos que fija la norma para las filas anteriores a esa regla (ver más abajo).

### 4b. Respaldo y varias fuentes del mismo acto

Un acto se publica en el diario oficial y también en el portal de la entidad que
lo emitió. Son publicaciones distintas del mismo `LegalDocument`; solo la del
diario oficial es autoritativa y de ella se extrae.

```powershell
# Requieren LIVE_SOURCE_ENABLED=true (leer antes docs/source-policy.md)
uv run kipu capture-pdf --publication-code 2540861-1     # respalda el PDF en el CAS
uv run kipu recapture   --publication-code 2540861-1     # versión nueva solo si cambió
uv run kipu capture-issue --issue-code NL20260806        # cuadernillo de la edición
uv run kipu link-source --publication-code 2540861-1 `
  --name "gob.pe - MIDAGRI" --external-code 8450966 --authority ISSUING_ENTITY `
  --url "https://www.gob.pe/institucion/midagri/normas-legales/8450966-..." `
  --pdf-url "https://cdn.www.gob.pe/uploads/document/file/10412064/....pdf" `
  --matched-by "mismo número de resolución y misma entidad emisora"
```

`--matched-by` es obligatorio: afirmar que dos publicaciones son el mismo acto
es una decisión humana y queda registrada. Nada empareja fuentes solo.

### 5. Consultar por API

```powershell
uv run kipu serve
# en otra terminal:
curl http://127.0.0.1:8000/v1/documents/by-source/NL/2540861-1
```

Swagger en `http://127.0.0.1:8000/docs`; UI de revisión en
`http://127.0.0.1:8000/review`. También hay ejemplos en `requests.http`.

### 5b. Resolución de identidad

La regla 13 prohíbe fusionar menciones **solo por el nombre**, no fusionar. Tres
señales independientes del nombre autorizan vincular sin revisión humana, y cada
vinculación registra en su afirmación cuál la sostuvo:

| Señal | Estado | Por qué basta |
|---|---|---|
| Identificador declarado (DNI, C.E.) | `IDENTIFIER_LINKED` | Identifica de forma unívoca: la fuente lo afirma, el sistema no lo infiere |
| Decisión humana previa | `PRECEDENT_LINKED` | Un revisor ya resolvió esa clave; se cita su decisión |
| Nombre + oficio unipersonal | `OFFICE_CORROBORATED` | Hay una sola Presidenta y un solo Ministro por cartera a la vez |

Si dos señales apuntan a personas distintas no se elige la más fuerte: se abre
un `EXTRACTION_CONFLICT` de prioridad 1 y decide un humano.

Un cargo genérico no corrobora nada — "Jefe Institucional" lo tiene cualquier
organismo. El catálogo de oficios unipersonales vive en
`domain/offices.py`, se versiona con la ontología y es **restrictivo por
defecto**: lo que no coincide va a revisión humana. Los identificadores no se
proyectan a RDF; son dato personal y `kipu:Person` se limita a información
funcional pública.

Un precedente tiene **alcance**, que el revisor elige al decidir:

| Alcance | Clave | Cuándo usarlo |
|---|---|---|
| `office` (por defecto) | nombre + cargo declarado | El cargo aporta una segunda señal. Sin cargo declarado no se registra nada |
| `global` | solo el nombre | La grafía **es** esa persona con cualquier cargo: el mismo nombre escrito con y sin segundo nombre ("ELMER CUBA BUSTINZA" / "ELMER RAFAEL CUBA BUSTINZA") |

El alias `global` no debilita la regla 13, que prohíbe que *el sistema* infiera
identidad por parecido de nombres: aquí la afirmación la aporta un humano y
queda trazable. Sí renuncia a la segunda señal, así que solo se admite sobre
grafías discriminantes: si el nombre ya corresponde a más de una persona la
decisión se rechaza con 422, porque el alias vincularía homónimos futuros sin
abrir tarea.

Los precedentes son auditables y revocables (`SPLIT_ENTITY` revoca los que
produjeron un enlace equivocado, incluido el alias):

```powershell
curl http://127.0.0.1:8000/v1/identity-precedents   # incluye "scope": office | global
```

Las grafías conocidas de una persona se derivan de sus menciones vinculadas y
sirven para encontrarla, con independencia de cómo la nombre cada documento:

```powershell
curl "http://127.0.0.1:8000/v1/persons?name=Elmer%20Cuba%20Bustinza"
curl http://127.0.0.1:8000/v1/persons/<id>          # campo "aliases"
```

### 6. Exportar RDF

```powershell
uv run kipu export-rdf --publication-code 2540861-1     # var/exports/*.ttl y *.jsonld
uv run kipu rebuild-projections                          # todo + dataset.trig (named graphs)
```

### 7. Validación completa

```powershell
uv run kipu validate --publication-code 2540861-1   # SHACL
uv run pytest                                       # 90+ pruebas, sin red
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy
```

## Estructura

```
src/kipu_knowledge/
  domain/         contratos (Protocols), enums, normalización, modelos Pydantic
  application/    ingest, queries (líneas de tiempo e incertidumbre), review, export
  adapters/       db, storage (CAS fs/MinIO), sources, parsing, extraction, rdf, search
  interfaces/     api (FastAPI v1), cli (Typer), review_ui (Jinja2)
alembic/          migraciones
ontology/         módulos Turtle, context.jsonld, shapes SHACL, VERSION, CHANGELOG
fixtures/         9 dispositivos reales capturados (casos A–H) + manifest
docs/             domain-analysis, architecture, source-policy, ontology-governance, adr/
tests/            unit, golden (A–H congelados), integration, invariants
```

## Fechas: lo que el documento dice y lo que la norma determina

Muchas resoluciones no expresan desde cuándo surte efectos lo que disponen. Eso
**no** autoriza a suponer la fecha de publicación… salvo que una norma lo diga:
el artículo 6 de la Ley N.º 27594 fija que una designación o nombramiento surte
efecto **el día** de su publicación en El Peruano —ese día, no el siguiente—,
salvo disposición en contrario que postergue su vigencia; el artículo 233.3 del
Reglamento General de la Ley del Servicio Civil dice lo mismo del término.

Las dos cosas se registran por separado y nunca se pisan:

| Campo | Qué significa |
|---|---|
| `effective_from` + `effective_from_status` | Lo que el documento expresa. Sin frase que la sustente queda `NOT_STATED` |
| `legal_effect_from` + `legal_effect_basis_json` | Lo que la norma determina, con la norma citada, el texto de la regla y la versión (`legal-effect-date/1.0`) |

La determinación se veta y el caso vuelve a revisión humana si la publicación
autoritativa no es el diario oficial (sin publicación en El Peruano el acto no
produce efectos), si no consta la fecha de publicación, o si la parte resolutiva
posterga la vigencia. Cada fecha determinada lleva como evidencia la frase de la
propia captura que declara la fecha de publicación. Ver `docs/adr/0007`.

```powershell
uv run kipu apply-legal-effect-dates --dry-run   # qué determinaría y por qué
uv run kipu apply-legal-effect-dates             # lo aplica y cierra las tareas
```

Reconstruye la cita releyendo los bytes del CAS: no toca la red.

### Ejemplo (caso CENEPRED)

¿Quién era jefe de CENEPRED el 2026-08-05? La renuncia del titular fue aceptada
"a partir del 4 de agosto de 2026" y la designación del sucesor no expresa fecha
de inicio, pero se publicó el 2026-08-06. `GET
/v1/positions/{id}/timeline?on=2026-08-05` responde:

```json
{
  "holder_at": {
    "status": "vacant",
    "basis": "legal_rule",
    "reason": "Ninguna asignación cubre la fecha consultada y todas las fechas que lo deciden están determinadas...",
    "supporting_evidence": [
      {"person_mention": "CARLOS MANUEL YAÑEZ LAZO", "valid_to": "2026-08-04", "effective_end_basis": "source"},
      {"person_mention": "MIGUEL YAMASAKI KOIZUMI", "valid_from": null, "valid_from_status": "NOT_STATED",
       "legal_effect_from": "2026-08-06", "effective_start_basis": "legal_rule"}
    ]
  }
}
```

El 2026-08-06 la misma consulta ya devuelve `confirmed` con el sucesor. Donde
ninguna de las dos vías determina la fecha, la respuesta sigue siendo
`unresolved` con la evidencia de ambos extremos: hay preguntas que el sistema
responde "no se puede saber con esta evidencia", y eso es correcto.

## Limitaciones conocidas del MVP

- Descubrimiento por fecha: interfaz preparada (`discover`), sin implementación
  live (exige validar la política de scraping de listados).
- Cuadernillo PDF: soporte de captura y capa de texto implementado, pero
  NL20260806.pdf no estaba disponible; la prueba correspondiente se omite.
- OCR: no incluido (extra futuro; nunca primera opción).
- Extractor LLM: solo punto de extensión tipado, desactivado por defecto.
- Resolución de entidades: exacta y conservadora; sin fuzzy matching. Los
  homónimos sin señal corroborante pasan por revisión humana. Las grafías que
  omiten un nombre de pila ("ELMER CUBA BUSTINZA" frente a "ELMER RAFAEL CUBA
  BUSTINZA") no se fusionan solas: se registran como personas distintas y se
  abre `PERSON_VARIANT_CHECK`. Las erratas siguen sin detectarse.
- Catálogo de oficios unipersonales mantenido a mano: no hay fuente oficial
  legible por máquina de la estructura del Estado. Cubre Presidencia, PCM,
  carteras ministeriales y órganos constitucionales únicos; jefaturas de
  organismos públicos no están cubiertas y van a revisión.
- Identificadores: el extractor los reconoce, pero ninguno de los 9 fixtures
  declara DNI — las resoluciones de designación nombran por nombre y cargo. La
  ruta está probada con casos sintéticos, no con datos reales de la fuente.
- Fuseki/OpenSearch: perfiles de compose opcionales, no cableados al pipeline.

Ver `AGENTS.md` para convenciones y reglas de contribución.
