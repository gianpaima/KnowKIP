# ADR-0005: Modelo de afirmaciones con procedencia

Estado: aceptado (2026-08-06)

## Contexto

Los hechos derivados deben ser auditables (¿de dónde salió esto?, ¿qué versión
del extractor lo produjo?, ¿quién lo aceptó?) y corregibles sin perder historia.

## Decisión

- Toda afirmación (`Assertion`) referencia obligatoriamente un `EvidenceSpan`
  (cita literal + hash + localizador dentro de una ArtifactVersion con SHA-256).
  El constraint es NOT NULL: ni siquiera las candidatas existen sin evidencia.
- Cada afirmación pertenece a una `ExtractionRun` que registra parser_version,
  extractor_version, ontology_version y, si aplica, proveedor/modelo/prompt LLM.
- Estados: CANDIDATE → AUTO_ACCEPTED / HUMAN_ACCEPTED / HUMAN_REJECTED →
  SUPERSEDED. Las correcciones crean una afirmación nueva y marcan la anterior
  SUPERSEDED (superseded_at, superseded_by_id); nunca se borra ni se edita.
- Política de auto-aceptación: solo hechos que re-enuncian texto explícito del
  artículo (extracción determinista). Identidades entre menciones, fechas no
  explícitas y vínculos desde considerandos quedan CANDIDATE con ReviewTask.

## Consecuencias

- La cadena evidencia → corrida → afirmación → decisión humana es reconstruible
  end-to-end (y se proyecta a PROV-O en RDF).
- El reproceso supersede en bloque las afirmaciones de corridas anteriores.
