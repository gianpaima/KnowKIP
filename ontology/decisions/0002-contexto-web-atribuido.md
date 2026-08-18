# 0002 — Contexto web atribuido (prensa y redes sociales)

Estado: **DRAFT** (2026-08-18). Sigue el flujo de gobernanza: esta propuesta
acompaña al esquema relacional ya implementado (`web_document`,
`web_person_mention`, `web_reference`; ver docs/web-context-design.md), pero
los términos NO entran en los TTL ni se bumpea VERSION hasta cumplir el flujo
completo (muestra documental ≥3, shapes, aprobación humana). La proyección RDF
de estas tablas queda desactivada mientras tanto.

## Motivación

La política de fuentes (reestructurada el 2026-08-18) admite prensa y redes
sociales públicas como **contexto atribuido** del perfil público de personas
que ya están en la base por actos oficiales. La ontología debe poder expresar
"la fuente F afirmó X sobre la persona P en el documento W, con esta cita" sin
que X pueda confundirse con el registro funcional.

## Términos propuestos

- `kipu:WebDocument` ⊑ `prov:Entity` — página web de contexto capturada.
  Mapeo: `schema:NewsArticle` (kind NEWS_ARTICLE), `schema:SocialMediaPosting`
  (SOCIAL_POST), `schema:ProfilePage` (SOCIAL_PROFILE).
- `kipu:webDocumentKind`, `kipu:bodyScope` — datatype properties (SKOS
  aparte si crecen).
- `kipu:WebPersonMention` ⊑ `prov:Entity` — aparición con evidencia
  (`kipu:evidence` reutilizado).
- `kipu:citesOfficialAct` ⊑ `eli:cites` (aprox.) — de `kipu:WebDocument` a
  `kipu:LegalDocument`, siempre con evidencia.
- Las afirmaciones de contexto reutilizan `kipu:Assertion` con predicados
  `web:*` (vocabulario cerrado en `domain/web_context.py`) y **obligatorio**
  `prov:wasAttributedTo` → el `kipu:SourceSystem` publicador. Shape SHACL
  propuesto: toda afirmación con predicado `web:*` debe tener
  `prov:wasAttributedTo`, `kipu:evidence` y NO debe existir en el grafo
  `:accepted` funcional — irán a un named graph propio `urn:kipu:graph:context`.

## Muestra documental

1 de ≥3: artículo RPP 2026-08-13 sobre César Luna Victoria (cita RS
027-2026-EF, ya ingerida como 2540905-3). Pendiente capturar formalmente esta
y dos muestras más cuando los primeros publicadores estén inspeccionados y
habilitados.

## Pendiente para pasar a STABLE

- [ ] 2 muestras documentales más, capturadas en el CAS.
- [ ] Shapes SHACL del párrafo anterior en `ontology/shapes/`.
- [ ] Términos en un `web_context.ttl` + entrada en CHANGELOG + bump MINOR.
- [ ] Aprobación humana y `OntologyRelease` en BD.
