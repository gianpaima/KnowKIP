# ADR-0006: No inferencia automática de fechas efectivas

Estado: aceptado (2026-08-06)

## Contexto

Muchas resoluciones no expresan la fecha de inicio (MIDAGRI 2540861-1, CENEPRED
2540903-2, SUNAT art. 2, Tribunal Fiscal art. 2). La tentación obvia es asumir
la fecha de publicación o el día siguiente. La eficacia jurídica real depende de
reglas de derecho administrativo (y a veces de actos posteriores) que el sistema
no debe adjudicar.

## Decisión

- Si el texto no declara la fecha: `effective_from = null`,
  `effective_from_status = NOT_STATED`. Está prohibido derivarla de
  published_on (regla 12; invariante testeado).
- Toda fecha con valor lleva `source_phrase` (frase textual que la sustenta);
  el modelo Pydantic lo valida y rechaza fechas sin base.
- El estatus INFERRED está reservado a inferencias futuras explícitas con
  `inferenceBasis` (método y base documental); el shape SHACL lo exige y el
  extractor determinista tiene prohibido producirlo.
- Las consultas puntuales responden con incertidumbre explicable: para CENEPRED
  al 2026-08-05 la API devuelve `unresolved` con la evidencia de ambos extremos
  (fin explícito 2026-08-04; inicio del sucesor NOT_STATED).

## Consecuencias

- Hay preguntas que el sistema responde "no se puede saber con esta evidencia".
  Es el comportamiento correcto, no una limitación a esconder.
- La revisión humana puede registrar fechas con base documental adicional vía
  supersede, dejando rastro completo.
