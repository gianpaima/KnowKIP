# ADR-0007: Fecha de inicio de efectos determinada por norma

Estado: aceptado (2026-08-07)

Extiende, sin derogar, a [ADR-0006](0006-no-effective-date-inference.md).

## Contexto

ADR-0006 prohíbe derivar la fecha efectiva de `published_on`, y esa prohibición
sigue siendo correcta como regla de extracción: el sistema no puede suponer que
un acto empieza a regir el día en que salió publicado.

Pero para una clase concreta de actos esa fecha no es un supuesto: está fijada
por una norma. El artículo 6 de la Ley N.º 27594 dispone que las resoluciones de
designación o nombramiento en cargos de confianza surten efecto **a partir del
día de su publicación** en El Peruano, salvo disposición en contrario de la
misma resolución que postergue su vigencia; el artículo 233.3 del Reglamento
General de la Ley del Servicio Civil dice lo propio para la designación y su
término en el Gobierno Nacional.

Dos consecuencias que el modelo anterior no distinguía:

- "a partir del día de su publicación" **incluye** ese día. No es el día
  siguiente: el cómputo desde el día hábil siguiente (art. 144.1 del TUO de la
  Ley N.º 27444) es para plazos procedimentales, no para la eficacia del acto.
- La ausencia de la frase en el texto no vuelve la fecha desconocida. La vuelve
  **no expresada literalmente pero determinada por mandato legal**, que es una
  situación epistemológica distinta y merece un registro distinto.

Tratar los seis casos del corpus como lagunas producía seis tareas de revisión
humana cuya respuesta correcta era siempre la misma y estaba escrita en la ley.

## Decisión

- La determinación **no toca** `effective_from`, que sigue en `NOT_STATED`: ese
  campo dice lo que el documento dice. La fecha determinada vive en
  `personnel_event.legal_effect_from`, con su fundamento en
  `legal_effect_basis_json` (norma, artículo, texto de la regla, si es cita
  literal o resumen, URL y racional), y se proyecta a la asignación afectada
  solo donde la fuente calló.
- La regla es determinista y pura (`domain/legal_effect.py`), re-ejecutable
  sobre los datos congelados, y solo cubre los tipos de evento con norma
  catalogada: designación y nombramiento (Ley 27594 art. 6), aceptación de
  renuncia, término de designación, término de encargo y cese (RGLSC 233.3).
  Un encargo, una delegación o una responsabilidad adicional no están cubiertos
  y siguen yendo a revisión humana.
- Vetos que devuelven el caso al humano en lugar de determinar:
  1. la publicación autoritativa no es el diario oficial, o no consta su sistema
     fuente (sin publicación en El Peruano el acto no produce efectos: el caso
     Wasimikuna, RDE N.º 6-2025-MIDIS/WM-DE, declaró sin efectos jurídicos una
     designación no publicada);
  2. no consta fecha de publicación;
  3. la parte resolutiva posterga la vigencia, que es la excepción que el propio
     artículo 6 reserva.
- La afirmación resultante es AUTO_ACCEPTED y cita como evidencia la frase de la
  **propia captura** que declara la fecha de publicación, con su rango sobre el
  texto del artefacto. Sin esa cita no se registra nada (regla 2).
- Las consultas puntuales usan la fecha expresada y, donde falta, la determinada
  por norma, diciendo siempre con cuál responden (`basis`). CENEPRED al
  2026-08-05 pasa de `unresolved` a `vacant`: el titular anterior cesó el
  2026-08-04 y la designación sucesora, publicada el 2026-08-06, no producía
  efectos todavía. Lo que ninguna de las dos vías determina sigue siendo
  `unresolved`.

## Consecuencias

- La regla 12 se mantiene intacta: lo prohibido sigue siendo que el sistema
  invente una fecha. Aquí no la inventa, la lee de una norma citada y verificable,
  igual que la corroboración por recital no adivina una identidad sino que la
  verifica contra el propio documento.
- El catálogo de normas es una decisión de dominio que se mantiene a mano y crece
  citando la norma que autoriza cada tipo de acto. Ampliarlo sin cita es
  exactamente el error que este ADR evita.
- Añadir una norma o cambiar la regla cambia respuestas ya publicadas. Por eso la
  determinación lleva versión de regla (`legal-effect-date/1.0`) y queda
  registrada como afirmación revisable, no como un cálculo al vuelo.
