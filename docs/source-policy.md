# Política de fuentes

## Estado de El Peruano (busquedas.elperuano.pe)

Inspección del 2026-08-06:

- `robots.txt` publica `User-agent: * / Allow: /` y un sitemap
  (`https://busquedas.elperuano.pe/sitemap.xml`). No hay rutas prohibidas
  declaradas a la fecha de inspección.
- Las páginas de dispositivo (`/dispositivo/NL/<código>`) sirven el contenido
  server-rendered; no requieren ejecutar JavaScript ni evadir control alguno.
- El sitio usa fingerprinting comercial (script BNI) para analítica; el crawler
  NO lo ejecuta ni lo suplanta — solo consume el HTML público.

Re-inspeccionar robots.txt y condiciones del sitio antes de habilitar cualquier
captura masiva; registrar los hallazgos aquí con fecha.

### Re-inspección del 2026-08-07 (previa a habilitar el descubrimiento diario)

Hecha porque la inspección anterior cubría la captura de dispositivos sueltos y
el descubrimiento por fecha recorre listados. Hallazgos:

- `robots.txt` responde 200 y sigue siendo `User-agent: * / Allow: /`, sin
  rutas prohibidas. Declara `sitemap.xml`, que a su vez apunta a
  `sitemap/sitemap-normas_legales.xml`.
- **El sitemap no sirve como índice diario**: trae 36 URLs con `lastmod` de hoy
  para todas, es decir una ventana móvil de "lo último", no la edición de una
  fecha. Usarlo daría un día incompleto sin avisar.
- **El cuadernillo tampoco es el índice.** `/cuadernillo/NL/YYYYMMDD` es la
  página del PDF de la edición completa; sus bytes (ya capturados para
  NL20260806) no contienen ni un solo enlace `/dispositivo/`.
- El índice real es la búsqueda por rango de fechas, que es lo que enlaza la
  propia portada del sitio en "Dispositivos" de cada edición:
  `/?fechaIni=YYYYMMDD&fechaFin=YYYYMMDD&tipoPublicacion=NL&ci=ONLY&start=N`.
  Se sirve renderizada en el servidor (200, `text/html; charset=utf-8`), pagina
  de 20 en 20 y declara en el propio HTML el rango consultado y el total
  ("32 dispositivos encontrados"). Ambas declaraciones se verifican al parsear.
- La portada sin parámetros (`/`) redirige (302) a `?ci=full&fecha=<hoy>`. Por
  eso el parser exige que el rango declarado sea el pedido: sin esa guarda, una
  redirección ingeriría la fecha equivocada en silencio.
- Volumen observado: 32 dispositivos NL el 2026-08-07 y 29 el 2026-08-02. Con
  el rate limit de 2 s, una edición completa son ~2 peticiones de índice y una
  o dos por dispositivo ingerido: del orden de un minuto de tráfico al día.
  No hay concurrencia.

Conclusión: se habilita el descubrimiento por fecha con el mismo
`PoliteFetcher` de siempre. No se recorre el sitio entero ni se sigue enlace
alguno fuera del índice de la fecha pedida.

### Observado durante la primera recolección real (2026-08-07)

- **El índice nunca se repite byte a byte.** Cada respuesta trae un script
  anti-bot con token distinto (`/bnith__…`) y una cookie con marca de tiempo al
  milisegundo (`x-bni-rncf`). Dos capturas del mismo listado, de 136 925
  caracteres cada una, difieren solo en eso. La deduplicación por sha256 del CAS
  por tanto no aplica al índice: el recolector compara **lo que el listado
  declara** (total y dispositivos) contra la última versión archivada y solo
  abre versión nueva si cambió. Sin eso, cada corrida de una fecha ya
  recolectada archivaría dos páginas más para siempre.
- **El 404 transitorio también ocurre en el índice**, no solo en la ruta del
  PDF: una corrida recibió 404 en la página 2 (`start=20`) y la misma URL
  respondió 200 poco después. La corrida falla entera y no ingiere un día a
  medias; se vuelve a lanzar y ya está. Sigue sin reintentarse en caliente.

### Rutas del PDF (comprobado el 2026-08-07)

Hay dos URLs y **no son intercambiables**:

| Ruta | Respuesta | Uso |
|---|---|---|
| `/dispositivo/NL/<código>/pdf` | `text/html`, ~106 KB | Página del visor. Enlace para mirar en la fuente |
| `/api/archivo/file/<token>/*/<código>.PDF` | `application/pdf`, ~339 KB | El archivo. Es la que se respalda |

La primera se deriva del código y es tentadora por estable, pero devuelve la
página, no el documento: guardarla como respaldo archiva una página web
creyendo que es el PDF. La segunda lleva un token opaco de CDN que solo se
obtiene del payload de la captura (`"urlPDF"`), y el `*` del path es literal y
funciona. Ante una rotación del token, la única vía es re-capturar el HTML.

Observada además una respuesta **404 transitoria** en la ruta del archivo,
seguida de 200 con la misma URL minutos después. El adaptador reintenta 429 y
503 pero no 404 (reintentar un "no existe" enmascararía errores reales), así
que una captura fallida por esta causa se reintenta a mano.

## Otras fuentes del mismo acto

### gob.pe (portales de entidad, desde el 2026-08-07)

El mismo acto se publica en el portal de la entidad emisora
(`www.gob.pe/institucion/<entidad>/normas-legales/<id>-<slug>`) con su PDF en
`cdn.www.gob.pe`. Se captura con `kipu link-source`, con la **misma** política
de User-Agent, rate limit y backoff que el diario oficial (`PoliteFetcher`, que
por eso no vive dentro del adaptador de El Peruano).

Autoridad `ISSUING_ENTITY`, nunca `OFFICIAL_GAZETTE`: en Perú la publicación en
el diario oficial es la que produce efectos. De estas fuentes **no se extraen
hechos**; sirven de respaldo y contraste. Si alguna afirmara algo que el diario
oficial calla, o lo contradijera, corresponde abrir `SOURCE_DISCREPANCY` y que
lo decida una persona.

El emparejamiento entre fuentes **no es automático**: `link-source` exige
`--matched-by`, que se guarda literal en `document_source.matched_by`.

## Reglas del crawler (implementadas en PoliteFetcher)

- `LIVE_SOURCE_ENABLED=false` por defecto: sin la bandera, `fetch()` lanza
  `LiveSourceDisabled`. CI nunca la activa.
- User-Agent identificable y configurable (`CRAWLER_USER_AGENT`), que debe
  incluir un contacto real antes de cualquier uso en vivo.
- Rate limit conservador por dominio (`CRAWLER_RATE_LIMIT_SECONDS`, 2 s por
  defecto), sin concurrencia por dominio en el MVP (cliente secuencial).
- Backoff exponencial con tope y respeto de `Retry-After` en 429/503.
- Se registran ETag y Last-Modified para revalidación condicional futura.
- Prohibido: evadir CAPTCHA o bloqueos, rotación de identidad para burlar
  límites, ocultamiento del agente.
- El descubrimiento por fecha (`kipu discover`, `kipu ingest-date`) recorre solo
  las páginas del índice de la fecha pedida, siguiendo los `start` que la propia
  paginación enlaza, con tope en `CRAWLER_MAX_LISTING_PAGES` (25). No sigue
  enlaces fuera de ese índice ni rastrea el sitio.
- **No hay reintentos en caliente dentro de una corrida.** Un fallo transitorio
  (404 pasajero, timeout, 5xx) deja el dispositivo en `RETRY_PENDING` y lo
  reintenta después `kipu retry-pending`, cuando alguien lo decide. Insistir en
  el acto contra un servidor que acaba de fallar es justo lo que esta política
  evita.

## Artefactos y publicación

- Los HTML/PDF capturados son evidencia interna: **no se republican**. La API
  expone metadatos, la URL oficial y los fragmentos probatorios necesarios
  (EvidenceSpan.quoted_text), no los binarios.
- Almacenamiento direccionado por contenido con SHA-256; los bytes nunca se
  sobrescriben ni se borran.

## Clasificación y finalidad de los datos

- **Ámbito**: exclusivamente información funcional pública (quién ocupa qué
  cargo público, según actos publicados en el diario oficial).
- **Exclusiones**: no se extraen DNI ni otros identificadores personales; no se
  enriquece con redes sociales ni fuentes externas; no se construyen perfiles
  personales; no se interpreta culpabilidad, responsabilidad o idoneidad.
- **Finalidad**: trazabilidad histórica y verificable de la función pública.
- Los nombres de personas provienen del propio acto oficial publicado, que por
  ley es de acceso público; se conservan tal como aparecen, con evidencia.

## Fixtures del repositorio

`fixtures/elperuano/*.html` fueron capturados el 2026-08-06 (HTTP 200,
robots.txt permisivo, User-Agent identificado; ver fixtures/manifest.json).
El cuadernillo NL20260806.pdf **no estaba disponible** en el workspace: la
prueba correspondiente se marca como omitida y no se inventa su contenido.

`fixtures/elperuano/listing/*.html` son tres páginas del índice capturadas el
2026-08-07 (dos del 07/08 y una del 02/08, esta última para probar que la guarda
de rango rechaza un listado que no es el pedido). Sus sha256 están en el
manifiesto.

## Secretos

Nunca confirmar secretos: `.env` está en .gitignore; usar `.env.example` como
plantilla. Las credenciales de compose son de desarrollo local únicamente.
