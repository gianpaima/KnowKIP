# ADR-0003: Separación documento / evento / asignación

Estado: aceptado (2026-08-06)

## Contexto

Los casos reales lo exigen: la RS 027-2026-EF (SUNAT) declara dos actos en un
documento; la RS 250-2026-PCM (BCRP) designa a tres personas en un artículo;
la RS 028-2026-EF concluye un encargo sin nombrar a la afectada en el artículo.

## Decisión

Tres agregados distintos:

- **LegalDocument**: el acto publicado y su estructura textual (evidencia).
- **PersonnelEvent**: cada declaración resolutiva (0..N por documento, 1..N
  personas por evento), con verbo original + clasificación controlada.
- **RoleAssignment**: el estado persona-puesto abierto/cerrado por eventos
  (start_event_id / end_event_id), con temporalidad de validez y de registro.

El mandato institucional (Mandate) se modela aparte de la asignación individual
(el sucesor de SUNAT completa el periodo del antecesor).

## Consecuencias

- La línea de tiempo de un puesto se deriva de asignaciones, no de documentos.
- Un evento END sin asignación vinculable queda marcado unresolved (shape SHACL)
  y genera tarea de revisión, en lugar de adivinar a quién afecta.
