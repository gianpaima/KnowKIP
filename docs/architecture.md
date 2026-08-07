# Arquitectura

## Vista general

Monolito modular (src/kipu_knowledge) con puertos y adaptadores:

```
interfaces/          API (FastAPI) · CLI (Typer) · UI de revisión (Jinja2)
        │
application/         ingest · queries · review · export
        │  (usa solo contratos)
domain/              contracts (Protocols) · enums · normalization ·
                     extraction_models (Pydantic estricto) · parsed
        ▲
adapters/            db (SQLAlchemy/Alembic) · storage (FS CAS, MinIO) ·
                     sources (ElPeruano) · parsing (HTML lxml, PDF PyMuPDF) ·
                     extraction (determinista, LLM opcional) · rdf (rdflib,
                     pySHACL) · search (PG FTS / LIKE) · resolution
```

Regla de dependencia: `domain` no importa nada externo al stdlib+pydantic;
`application` importa domain y adapters concretos solo por inyección/factoría;
`interfaces` orquesta. FastAPI, MinIO, Fuseki, OpenSearch y proveedores LLM son
reemplazables sin tocar el núcleo.

## Flujo vertical (implementado end-to-end)

```
URL / archivo / fixture
  → SourceAdapter.fetch (política live, rate limit, backoff)  [captura SIEMPRE primero]
  → ArtifactStore.put_immutable  → sha256/ab/cd/<hash>         [regla 11]
  → ArtifactVersion (status, headers, ETag, hash, crawler_version)
  → ElPeruanoHtmlParser.parse    → ParsedDocument (secciones tipadas)
  → DeterministicExtractor.extract → ExtractionResult (Pydantic estricto)
  → _ResultPersister             → EvidenceSpans, Mentions, Events, Assignments,
                                   Assertions (con evidencia), ReviewTasks
  → RdfProjection.rebuild        → named graphs (source/extraction/candidate/
                                   accepted/ontology) → TTL / JSON-LD / TriG
  → pySHACL                      → validación con ontology/shapes/
  → API v1 / UI de revisión
```

## Decisiones clave (ver docs/adr/)

- ADR-0001 PostgreSQL fuente operativa de verdad.
- ADR-0002 RDF como proyección semántica derivada y reconstruible.
- ADR-0003 separación documento / evento / asignación.
- ADR-0004 HTML para extracción, PDF para respaldo/evidencia.
- ADR-0005 afirmaciones con procedencia (evidencia obligatoria, supersede).
- ADR-0006 no inferencia automática de fechas efectivas.

## Almacenamiento

- **PostgreSQL** (host 5433 vía compose): 27 tablas, migración Alembic
  autogenerada y verificada también sobre SQLite (pruebas/CI).
- **ArtifactStore**: filesystem CAS por defecto (`var/artifacts`), MinIO
  con el mismo layout de claves activando `ARTIFACT_STORE=minio`.
- **Búsqueda**: PG FTS en español tras la interfaz `SearchBackend`; fallback
  LIKE en SQLite; OpenSearch enchufable (perfil compose `search`).
- **Semántica**: perfil compose `semantic` levanta Fuseki para cargar los
  exports TriG; no es requisito del MVP.

## Extractor LLM opcional (desactivado)

`adapters/extraction/llm.py` define el punto de extensión: salida validada por
los mismos modelos Pydantic, registro de proveedor/modelo/prompt en
ExtractionRun, abstención obligatoria sin evidencia y resultados como
CANDIDATE con revisión humana. No hay proveedor implementado en el MVP y la
bandera `LLM_EXTRACTOR_ENABLED` es false por defecto.

## Reproceso y versionado

`kipu reprocess --publication-code X`: las afirmaciones previas pasan a
SUPERSEDED (nunca se borran; la cadena queda en `assertion` + `extraction_run`);
las filas derivadas deterministas (secciones, eventos, asignaciones) se
reconstruyen desde los bytes originales del artefacto. Los bytes jamás cambian.
