# Decisión de ontología 0001: mapeo futuro a Akoma Ntoso

Estado: DOCUMENTED (no implementado en el MVP)

## Contexto

Akoma Ntoso (AKN) es el estándar OASIS para documentos legislativos. Convertir
íntegramente los dispositivos de El Peruano a AKN excede el alcance del MVP y no
aporta al caso de uso principal (hechos de personal con evidencia).

## Decisión

El MVP modela secciones con `kipu:DocumentSection` + `kipu:SectionTypeScheme` (SKOS).
Se documenta el mapeo futuro:

| Kipu                          | Akoma Ntoso                       |
|-------------------------------|-----------------------------------|
| SUMMARY (sumilla)             | `akn:longTitle` / `akn:docTitle`  |
| VISTOS                        | `akn:preamble/akn:container[@name='vistos']` |
| CONSIDERANDO                  | `akn:recitals/akn:recital`        |
| RESOLVE_HEADER                | `akn:formula`                     |
| ARTICLE                       | `akn:body/akn:article`            |
| ANNEX                         | `akn:attachments/akn:attachment`  |
| SIGNATURE                     | `akn:conclusions/akn:signature`   |
| PUBLICATION_CODE              | metadato de manifestación         |

La identidad FRBR de AKN (work/expression/manifestation) se corresponde con
PublicationItem (work) / LegalDocument (expression) / Artifact+Version (manifestation).
