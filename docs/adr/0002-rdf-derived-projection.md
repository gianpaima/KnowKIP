# ADR-0002: RDF como proyección semántica derivada

Estado: aceptado (2026-08-06)

## Contexto

Se requiere exportar el conocimiento como JSON-LD/RDF interoperable (ELI,
PROV-O, ORG, SKOS) y validarlo con SHACL, sin duplicar la autoridad de los datos.

## Decisión

La proyección RDF (`RdfProjection`) se **reconstruye** desde PostgreSQL bajo
demanda (`kipu rebuild-projections`, `kipu export-rdf`, endpoints /v1/exports).
Usa named graphs para separar fuente, extracción, candidatas, aceptadas y
versión de ontología. Nunca se edita RDF a mano ni se escribe conocimiento
directamente en el grafo.

## Consecuencias

- El grafo puede regenerarse íntegramente tras cualquier corrección relacional.
- SHACL valida cada export; una violación es un bug del pipeline, no un dato a
  parchear en el grafo.
- Consumidores semánticos pueden elegir el grafo `accepted` (solo conocimiento
  aceptado) o auditar el pipeline completo vía TriG.
