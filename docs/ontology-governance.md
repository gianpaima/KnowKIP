# Gobernanza de la ontología

## Principio

La ontología (ontology/) es un artefacto versionado con ciclo de vida propio.
**Nunca se modifica automáticamente en producción**: ni el extractor ni un LLM
pueden crear clases o propiedades. Los términos desconocidos generan candidatos
de ontología y tareas de revisión (`ReviewTask` tipo ONTOLOGY_CANDIDATE).

## Flujo de cambio

```
DRAFT
  → muestra documental        (≥3 documentos reales que exhiben el concepto)
  → propuesta                 (ontology/decisions/NNNN-*.md con mapeo y ejemplos)
  → validación                (shapes SHACL actualizados; pytest + pyshacl pasan)
  → aprobación                (revisión humana; bump de ontology/VERSION,
                               entrada en CHANGELOG.md, OntologyRelease en BD)
  → STABLE
  → eventual DEPRECATED / REPLACED (nunca eliminación silenciosa)
```

## Versionado

- `ontology/VERSION` (semver) es la versión vigente; cada `ExtractionRun`
  registra con qué versión se produjo cada afirmación.
- `ontology/CHANGELOG.md` documenta cada release.
- La tabla `ontology_release` ancla versiones a commits.
- Los cambios incompatibles (renombrar/eliminar propiedades) exigen versión
  mayor y plan de migración de la proyección RDF.

## Reutilización de vocabularios

| Estándar | Uso |
|---|---|
| ELI | metadatos de documentos legales (fechas, citas) |
| PROV-O | procedencia: artefactos, corridas de extracción, derivación |
| W3C ORG | organizaciones, unidades, puestos (org:Post), membresías |
| SKOS | vocabularios controlados (tipos de evento, secciones, temas) |
| SHACL | validación de la proyección (ontology/shapes/) |

Akoma Ntoso: no se adopta en el MVP; el mapeo futuro de secciones está
documentado en ontology/decisions/0001-akoma-ntoso-mapping.md.

## Named graphs

La proyección separa: `urn:kipu:graph:source` (documento y artefactos),
`:extraction` (corridas), `:candidate` (afirmaciones candidatas),
`:accepted` (aceptadas), `:ontology` (versión). Esto permite consumir solo
conocimiento aceptado o auditar el pipeline completo.

## Criterios para conceptos nuevos

1. Aparece en documentos reales (con evidencia) — no especulativo.
2. No es expresable con los términos existentes + SKOS.
3. Tiene shape de validación si introduce obligaciones estructurales.
4. Tiene etiqueta y definición en español, y mapeo a estándares si existe.
