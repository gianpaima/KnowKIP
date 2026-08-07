# ADR-0001: PostgreSQL como fuente operativa de verdad

Estado: aceptado (2026-08-06)

## Contexto

El sistema mantiene documentos, evidencia, afirmaciones versionadas y estados de
revisión con integridad transaccional. Se consideró un triple store como base
primaria.

## Decisión

PostgreSQL es la única fuente operativa de verdad. Todo cambio de estado
(ingesta, revisión, supersede) ocurre en transacciones relacionales con
constraints (FK, unique, CHECK). SQLite se usa solo en pruebas gracias a tipos
portables (VARCHAR+CHECK para enums, JSON estándar).

## Consecuencias

- Integridad referencial fuerte para las reglas de dominio (p.ej. Assertion
  exige evidence_span_id NOT NULL).
- El RDF, la búsqueda y cualquier índice futuro son proyecciones reconstruibles
  (ver ADR-0002), nunca autoridad.
- Un triple store (Fuseki, perfil compose `semantic`) es opcional y de solo
  lectura respecto del conocimiento.
