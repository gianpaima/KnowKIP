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
- El descubrimiento por fecha (`discover`) expone la interfaz pero devuelve
  vacío: habilitarlo exige validar la política de scraping de listados.

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

## Secretos

Nunca confirmar secretos: `.env` está en .gitignore; usar `.env.example` como
plantilla. Las credenciales de compose son de desarrollo local únicamente.
