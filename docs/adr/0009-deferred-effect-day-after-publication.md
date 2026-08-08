# ADR-0009: Diferimiento calculable al día siguiente de la publicación

Estado: aceptado (2026-08-08)

Extiende, sin derogar, a [ADR-0007](0007-legal-effect-date-determination.md), que
a su vez extiende a [ADR-0006](0006-no-effective-date-inference.md).

## Contexto

ADR-0007 determina la fecha de inicio de efectos para los actos con norma
catalogada, y la veta cuando la parte resolutiva contiene una cláusula que
posterga la vigencia —la excepción que el propio artículo 6 de la Ley N.º 27594
reserva—. Ese veto trataba todas las cláusulas de vigencia como si fueran lo
mismo.

No lo son. Una resolución que dice

> Artículo 2.- La acción de personal dispuesta en el artículo 1 precedente,
> tendrá efectividad a partir del día siguiente de la publicación de la presente
> resolución en el Diario Oficial El Peruano, sin perjuicio del procedimiento de
> entrega y recepción de cargo correspondiente.

no deja la fecha indeterminada: la fija con exactitud sobre un dato que el
sistema ya tiene capturado y citado. Publicada el 2026-08-07, los efectos corren
desde el 2026-08-08. No hay juicio humano que ejercer; hay una suma.

Es distinto de una resolución que ata su vigencia a un hecho futuro («hasta la
instalación del Directorio»), y distinto también del **día hábil** siguiente,
cuyo cómputo exige un calendario de feriados que este sistema no tiene y no
debe improvisar.

El veto indiscriminado producía además un callejón sin salida en el panel de
revisión: la tarea quedaba abierta, `APPLY_LEGAL_EFFECT_DATE` fallaba con 422 y
`MARK_DATE_NOT_STATED` afirmaba algo que el documento contradice. Tres eventos
del corpus (RCG N.º 430-2026-CG y N.º 431-2026-CG) estaban en ese estado.

## Decisión

- El detector binario `find_postponement_clause` se sustituye por un
  clasificador, `find_deferral_clause`, que devuelve la cláusula con su tipo:
  - `DAY_AFTER_PUBLICATION` — el día siguiente **anclado explícitamente a la
    publicación**. Determina: `published_on + 1 día natural`.
  - `INDETERMINATE` — todo lo demás, incluido el día *hábil* siguiente y el
    «día siguiente» sin ancla («rige a partir del día siguiente», ¿siguiente a
    qué?). Veta, como hasta ahora.
- La norma catalogada **no cambia** con el diferimiento: sigue siendo la que
  faculta al acto a disponer en contrario (Ley 27594 art. 6 para designaciones,
  RGLSC 233.3 para términos). Lo que se añade al fundamento es la cláusula del
  propio acto, su tipo y el identificador de la sección dispositiva de la que
  salió, de modo que la afirmación se pueda auditar sin volver a correr el
  clasificador.
- Ante una resolución que contuviera cláusulas de las dos clases, gana la
  indeterminada: devolverla al humano es más conservador que quedarse con la
  mitad que sí se sabe sumar.
- `effective_from` sigue sin tocarse: dice lo que el documento dice, y lo que el
  documento dice es una fecha relativa, no una fecha.
- La versión de regla pasa a `legal-effect-date/1.1`.
- Cuando la regla veta por diferimiento, la tarea de revisión ya no dice que el
  documento «no expresa la fecha»: dice que la difiere a un momento que estos
  datos no permiten fechar. Y el panel deja de ofrecer acciones imposibles (ver
  abajo).

## Salida para lo que sigue siendo indeterminado

Vetar sin ofrecer salida es lo que dejó las tareas atascadas. Se añade la acción
`SET_LEGAL_EFFECT_DATE`: el revisor fija la fecha a mano, con nota justificativa
obligatoria, y se escribe en `legal_effect_from` —nunca en `effective_from`—
con el fundamento marcado como decisión humana y el identificador del
`ReviewDecision` que la respalda. No genera `Assertion`, porque no hay
`EvidenceSpan` que citar para un juicio humano: el registro auditable es la
decisión.

El panel, además, deja de ofrecer `APPLY_LEGAL_EFFECT_DATE` cuando el veredicto
vivo no la determina. Ofrecer un botón que garantiza un 422 no es informar, es
hacer perder el trabajo escrito en el formulario.

## Consecuencias

- La regla 12 y ADR-0006 siguen intactos: lo prohibido es que el sistema invente
  una fecha a partir de la publicación. Aquí no la inventa — el documento manda
  computarla así, y la norma le reconoce esa facultad.
- Cambian respuestas ya publicadas para los tres eventos afectados. Por eso la
  versión de regla sube y la determinación queda como afirmación revisable.
- El catálogo de patrones calculables crece solo con formas ancladas a un dato
  capturado. Un patrón que exija calendario de feriados, plazos hábiles o
  cualquier tabla que el sistema no tenga **no entra**: se veta y lo decide un
  humano. Añadirlo sin ese dato sería exactamente el error que ADR-0006 evita.
