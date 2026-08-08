# ADR-0008: Descubrimiento por fecha y recolección diaria

- Estado: aceptado
- Fecha: 2026-08-07
- Contexto previo: ADR-0004 (captura antes que parsing), ADR-0005 (procedencia)

## Contexto

Hasta ahora cada acto entraba a mano: `ingest-device <url>` o `ingest-fixture`.
Eso sirve para construir el corpus de prueba, pero no para mantener el
conocimiento al día: alguien tiene que saber qué se publicó y pegar URLs.

Para automatizarlo hacía falta un índice de lo publicado en una fecha. Se
evaluaron tres candidatos (inspección registrada en docs/source-policy.md):

1. **El cuadernillo** (`/cuadernillo/NL/YYYYMMDD`). Descartado: su página solo
   declara el PDF de la edición completa. Los bytes ya capturados de NL20260806
   no contienen ni un enlace a un dispositivo.
2. **El sitemap** (`sitemap/sitemap-normas_legales.xml`). Descartado: 36 URLs
   con `lastmod` de hoy para todas. Es una ventana móvil de "lo último", no la
   edición de una fecha; usarlo daría días incompletos sin avisar.
3. **La búsqueda por rango de fechas**, que es lo que enlaza la propia portada:
   `/?fechaIni=&fechaFin=&tipoPublicacion=NL&ci=ONLY&start=N`. Elegida.

## Decisión

### 1. El índice se parsea, se verifica y se archiva

El índice se sirve renderizado en el servidor y declara dos cosas que se usan
como control: el rango consultado ("Dispositivos del dd/mm/aaaa al dd/mm/aaaa")
y el total ("N dispositivos encontrados"). Se exige que el rango sea el pedido
—la portada redirige a la fecha de hoy, y sin esa guarda se ingeriría el día
equivocado en silencio— y que lo recolectado entre todas las páginas cuadre con
el total. Si no cuadra, la corrida **falla** en lugar de devolver un día
incompleto, que se leería como "ese día se publicó menos".

Sus bytes se archivan en el CAS como representación `LISTING` del ítem de la
edición. El índice es la constancia de qué dijo la fuente que se publicó ese
día; sin él, "se ingirieron 19 normas" no se puede contrastar con nada.

Con una excepción que la primera corrida real obligó a hacer explícita: el
índice **nunca** llega dos veces con los mismos bytes, porque el sitio inyecta
en cada respuesta un token anti-bot y una marca de tiempo al milisegundo. La
deduplicación del CAS —bytes idénticos, misma versión— no puede funcionar ahí.
Lo que decide es lo que el listado declara: si el total y los dispositivos son
los de la última versión archivada, se reutiliza; si cambiaron —una norma
añadida más tarde, una fe de erratas— se abre versión, que es precisamente el
hecho que interesa registrar.

### 2. El filtro de relevancia decide sobre el catálogo, no sobre el documento

El diario publica de todo. El extractor solo entiende actos de personal, así que
ingerir la edición entera llenaría la base de documentos sin afirmaciones. El
filtro (`domain/relevance.py`, regla `personnel-relevance/1.0`) clasifica la
**sumilla del catálogo** con dos listas explícitas de verbos, sin puntuaciones
ni umbrales.

Lo que no está en ninguna lista queda `UNDECIDED` y **se ingiere igual**: un
catálogo incompleto debe costar trabajo de más, nunca un documento perdido. Un
acto de personal en cualquier parte de la sumilla manda sobre un verbo negativo
al principio ("Dejan sin efecto designaciones y designan fedatarios…" entra).

### 3. Todo lo visto se registra, se ingiera o no

Tabla `crawl_item`: un renglón por dispositivo descubierto, con lo que el
catálogo declaraba, el veredicto de relevancia con su regla y su motivo, el
estado final y el error literal si lo hubo. Esta es la parte que hace honesto al
filtro: sin ella, "se ingirieron N normas" sería indistinguible de "ese día se
publicaron N". Un invariante lo congela.

La sumilla del catálogo **no es evidencia** de ningún hecho del documento: la
escribe el buscador, no la norma. Por eso vive en la bitácora del recolector y
no produce ninguna afirmación.

### 4. Lo transitorio se reintenta aparte, nunca en caliente

El 2026-08-07 se observó un 404 pasajero en la ruta del PDF, con 200 minutos
después para la misma URL. En un proceso desatendido, tratarlo como definitivo
pierde el documento; reintentarlo en el acto castiga a un servidor que acaba de
fallar. Los fallos se clasifican: lo transitorio queda en `RETRY_PENDING` (o
`INGESTED_PDF_PENDING` si solo faltó respaldar el archivo) y lo reintenta un
`kipu retry-pending` explícito. Lo que no mejora reintentando —parseo, HTML
contaminado— queda `FAILED` y pide que alguien mire.

### 5. Lo relevante que no produce eventos se cuenta

`crawl_item.events_extracted` guarda cuántos eventos de personal salieron. Un
cero en un dispositivo que el filtro llamó relevante no es un fallo —el
documento está capturado y su texto íntegro— pero tampoco puede pasar por una
ingesta correcta: es un hueco del extractor. En la edición del 2026-08-07 pasó
en 6 de 19. El comando lo dice al terminar y `crawl-report` lo lista.

## Consecuencias

- La ingesta puede programarse (ver README, sección de programación) y correrse
  dos veces sin duplicar nada: lo ya ingerido queda `ALREADY_PRESENT` y una
  captura de bytes idénticos no abre versión.
- Cada publicación queda atada a la edición del día en que el índice la listó
  (`publication_item.issue_id`), con la guarda de que la propia tarjeta repita
  esa fecha.
- El filtro es un punto de pérdida potencial y por eso es auditable: lo
  descartado se recupera ingiriendo su código, o corriendo la fecha con
  `--include-not-relevant`, sin volver a descubrir nada.
- Queda pendiente: el índice se recorre completo en cada corrida (no hay
  revalidación condicional con ETag), y el filtro no distingue entre "no es de
  personal" y "es de personal pero de un tipo que el extractor aún no cataloga"
  (encargos de unidad, delegaciones): ambos acaban en la misma bitácora, pero
  los segundos sí se ingieren, porque su verbo está en el catálogo positivo o
  queda `UNDECIDED`.
