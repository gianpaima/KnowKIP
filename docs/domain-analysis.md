# Análisis de dominio

## El problema

Las resoluciones de personal público (designaciones, nombramientos, encargaturas,
renuncias, conclusiones) se publican como texto legal en El Peruano. Responder
"¿quién ocupaba el puesto P en la fecha D?" exige convertir ese texto en hechos
estructurados **sin perder la evidencia ni fingir certeza que el texto no da**.

## Conceptos centrales y su separación

### Documento ≠ evento ≠ asignación (ADR-0003)

- **LegalDocument**: el acto publicado (RM, RS, RJ, RI) con su estructura
  (VISTOS, CONSIDERANDO, SE RESUELVE, artículos, firmas).
- **PersonnelEvent**: lo que un artículo declara (p.ej. "Aceptar la renuncia...").
  Un documento puede declarar 0, 1 o N eventos (la RS de SUNAT declara dos:
  renuncia + designación). Un evento puede involucrar N personas (el directorio
  del BCRP designa a tres en un solo artículo).
- **RoleAssignment**: el estado persona-puesto que los eventos abren (START),
  cierran (END) o modifican. Tiene temporalidad doble: validez del hecho
  (valid_from/valid_to con estatus epistemológico) y registro del sistema
  (recorded_at/superseded_at).

### Estatus epistemológico de fechas

Los documentos reales exhiben cuatro situaciones distintas, todas presentes en el
corpus de referencia:

| Situación | Ejemplo real | Modelado |
|---|---|---|
| Fecha explícita | BNP: "a partir del 06 de agosto de 2026" | value + EXPLICIT |
| Eficacia anticipada | INBP: "con eficacia anticipada a partir del 30 de julio" | value + EXPLICIT |
| No expresada | MIDAGRI, CENEPRED (designaciones sin fecha) | null + NOT_STATED |
| Condición textual | INBP: "hasta el retorno del descanso vacacional de..." | end_condition_text + CONDITIONAL |

**Nunca** se rellena effective_from con la fecha de publicación (regla 12):
la vigencia jurídica de una designación no expresada es una cuestión de derecho
administrativo que el sistema no debe resolver por su cuenta.

### Menciones antes que entidades

`PersonMention` captura la forma textual exacta. La entidad canónica `Person`
solo se vincula cuando: (a) no existe ningún candidato homónimo (crear persona
nueva no fusiona nada, es seguro), o (b) un humano confirma el vínculo.
Los homónimos (p.ej. la Presidenta firma 5 documentos del corpus) quedan
CANDIDATE_MATCH con tarea de revisión — nunca auto-merge por nombre (regla 13).

### Puestos y plazas

La identidad de un puesto = organización + unidad + etiqueta normalizada
(+ PositionSlot cuando existe, p.ej. correlativo CAP 007 de la BNP). Dos puestos
con el mismo título en entidades distintas son puestos distintos. La encargatura
adicional (INBP) es `ADDITIONAL_RESPONSIBILITY`: terminarla no toca el puesto
base de la persona (regla 18).

### Mandatos

El periodo institucional se modela separado de la asignación individual
(`Mandate`): el sucesor de SUNAT completa el periodo de 5 años de su antecesor;
los directores del BCRP sirven por el periodo constitucional presidencial.

## Hallazgos empíricos del corpus (fixtures reales)

1. El visor de El Peruano incrusta el HTML original del dispositivo dentro de
   `div#x<código>`; el `<title>` interno de la página puede pertenecer a OTRO
   dispositivo (contaminación observada en 2540861-1). Por eso el parser
   delimita por contenedor y valida código, título y número.
2. La fecha de publicación no está en el dispositivo sino en el chrome del
   buscador ("Fecha de publicación: 06/08/2026").
3. El artículo que concluye un encargo (Tribunal Fiscal) NO nombra a la persona
   afectada; solo los considerandos la mencionan. Se modela como participante
   candidato con confianza reducida + tarea de revisión.
4. La misma entidad aparece con grafías distintas de guion ("-CENEPRED" vs
   "- CENEPRED"): la normalización colapsa el espaciado de guiones.
5. Artículos no-evento recurrentes: refrendo ministerial, obligación de
   declaraciones juradas (PRODUCE), encargo de publicación web a una oficina
   (BNP/INBP), notificación. Se clasifican y NO generan eventos (regla 23).

## Extensibilidad

`SourceAdapter` + `source_family` desacoplan el dominio de El Peruano. Fuentes
futuras (Boletín Oficial, contrataciones, concesiones, entidades privadas)
añaden adaptadores y vocabularios SKOS nuevos sin tocar el núcleo. Los campos
`organization_type` / `public_private_category` ya anticipan entidades no públicas.
