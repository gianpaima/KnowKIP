# ADR-0004: HTML para extracción, PDF para respaldo/evidencia

Estado: aceptado (2026-08-06)

## Contexto

El visor de El Peruano sirve el dispositivo como HTML server-rendered dentro de
`div#x<código>`, con clases estables (h1.sumilla, h2.resoluci-n, p.cuerpo).
El PDF y el cuadernillo son manifestaciones del mismo acto con maquetación de
imprenta (columnas, saltos) más costosa de segmentar.

## Decisión

- La extracción estructurada opera sobre el HTML delimitado por contenedor,
  validando título, número y código (el corpus demostró contaminación real:
  el `<title>` interno de 2540861-1 pertenece a otro dispositivo).
- El PDF/cuadernillo se captura y respalda como ArtifactVersion (ISSUE_PDF) y
  su capa de texto se extrae con PyMuPDF solo como evidencia complementaria y
  verificación cruzada. OCR queda como extra opcional futuro, nunca primera opción.
- HTML, PDF y cuadernillo se relacionan como representaciones del mismo
  PublicationItem (tabla artifact.representation_type).

## Consecuencias

- Parser determinista y testeable con fixtures congelados.
- Sin dependencia dura de PyMuPDF (extra `pdf`); la ausencia del cuadernillo
  no bloquea el MVP (prueba marcada skip).
