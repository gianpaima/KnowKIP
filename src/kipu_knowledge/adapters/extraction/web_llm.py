"""Clasificador LLM de afirmaciones de contexto web (paso 5 del diseño).

La prosa periodística no se domestica con patrones; aquí sí entra un modelo.
Pero el modelo NO es una fuente de verdad, y el diseño lo encierra en tres
jaulas:

1. **Vocabulario cerrado**: solo puede proponer predicados `web:*` del catálogo
   (domain/web_context.CONTEXT_PREDICATES). Un predicado fuera del catálogo se
   descarta — un tipo nuevo de afirmación es una decisión de política, no una
   salida del modelo.
2. **Cita obligatoria y verificada**: cada afirmación debe traer la frase
   exacta del artículo. La verificación es mecánica y vive en la capa de
   aplicación (application/web_claims): una cita que no aparezca byte a byte
   en el texto extraído invalida la afirmación. Aquí no se confía; se propone.
3. **Proveniencia completa**: proveedor, modelo y versión de prompt quedan en
   la ExtractionRun, como ya preveía la ontología.

El proveedor concreto (Anthropic) se llama con httpx, sin SDK: una petición
JSON con salida estructurada. Desactivado por defecto (LLM_EXTRACTOR_ENABLED).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from kipu_knowledge.config import get_settings
from kipu_knowledge.domain import web_context as wc

WEB_CLAIMS_PROMPT_VERSION = "web-context-claims/1.0"

# El prompt es un artefacto versionado: cambiarlo obliga a subir la versión de
# arriba, porque cada afirmación registra con qué prompt se produjo.
_SYSTEM_PROMPT = """\
Eres un extractor de afirmaciones de contexto sobre una persona con función \
pública, a partir de un artículo de prensa peruano. Devuelves EXCLUSIVAMENTE \
un array JSON. Cada elemento: {"predicate": ..., "quote": ..., "data": {...}}.

Predicados permitidos (cualquier otro invalida la afirmación):
- "web:prior_public_role": cargo público previo que el artículo atribuye a la \
persona. data: {"role": ..., "org": ..., "period_text": ...}
- "web:private_sector_role": actividad en el sector privado declarada. \
data: {"role": ..., "org": ...}
- "web:education": formación académica declarada. data: {"degree_or_field": \
..., "institution": ...}
- "web:profession": profesión declarada. data: {"profession": ...}
- "web:public_statement": declaración textual de la propia persona recogida \
por el artículo. data: {"topic": ...}
- "web:covers_event": el artículo cubre un evento de la función pública \
(juramentación, asunción, presentación). data: {"event": ..., "date_text": ...}
- "web:reported_assessment": señalamiento publicado (investigación, \
cuestionamiento) SIN evaluarlo. data: {"nature": ...}
- "web:other_context": contexto relevante que no encaja arriba. data: {}

Reglas inquebrantables:
1. "quote" es una frase COPIADA LITERALMENTE del texto que se te da, sin \
recortes internos ni correcciones. Será verificada carácter a carácter: si no \
aparece tal cual, la afirmación se descarta.
2. Solo afirmaciones sobre la persona indicada. Ignora otras personas.
3. PROHIBIDO extraer: datos sensibles (salud, religión, orientación, familia, \
menores), identificadores personales (DNI), domicilios, vida privada, y toda \
valoración tuya. Si el artículo especula, no extraes.
4. Los campos de "data" resumen la cita; jamás añaden información que la cita \
no diga. Campos desconocidos se omiten.
5. Si no hay nada extraíble, devuelve [].
"""


@dataclass(frozen=True)
class WebContextClaim:
    """Una afirmación propuesta por el clasificador, aún sin verificar."""

    predicate: str
    quote: str
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class WebContextClassifier(Protocol):
    """Contrato del clasificador; la aplicación no sabe qué modelo hay detrás."""

    provider: str
    model: str
    prompt_version: str

    def classify(self, person_name: str, full_text: str) -> list[WebContextClaim]: ...


class WebClassifierNotConfigured(RuntimeError):
    pass


class AnthropicWebContextClassifier:
    """Clasificador sobre la API de mensajes de Anthropic, sin SDK.

    Falla explícitamente si el LLM no está habilitado y configurado; nunca se
    activa solo. La temperatura va a 0: mismas entradas, misma propuesta, que
    es lo más cerca de determinismo que un modelo ofrece.
    """

    prompt_version = WEB_CLAIMS_PROMPT_VERSION

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_extractor_enabled:
            raise WebClassifierNotConfigured(
                "LLM_EXTRACTOR_ENABLED=false: el clasificador de contexto está desactivado"
            )
        if settings.llm_provider.lower() != "anthropic" or not settings.llm_model:
            raise WebClassifierNotConfigured(
                "Configura LLM_PROVIDER=anthropic y LLM_MODEL para habilitar el clasificador"
            )
        if not settings.anthropic_api_key:
            raise WebClassifierNotConfigured("Configura ANTHROPIC_API_KEY (en .env, nunca en git)")
        self.provider = "anthropic"
        self.model = settings.llm_model
        self._api_key = settings.anthropic_api_key

    def classify(self, person_name: str, full_text: str) -> list[WebContextClaim]:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0,
                "system": _SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": (f"Persona: {person_name}\n\nTexto del artículo:\n{full_text}"),
                    }
                ],
            },
            timeout=120.0,
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return parse_claims(text)


def parse_claims(raw: str) -> list[WebContextClaim]:
    """Parsea la salida del modelo con tolerancia cero a estructura inválida.

    Elementos malformados se descartan uno a uno, no en bloque: que el modelo
    estropee una afirmación no debe costar las demás. El vocabulario se filtra
    aquí además de en la verificación, para que ninguna capa dependa sola de
    la otra.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    claims: list[WebContextClaim] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        predicate = entry.get("predicate")
        quote = entry.get("quote")
        if not isinstance(predicate, str) or predicate not in wc.CONTEXT_PREDICATES:
            continue
        if not isinstance(quote, str) or not quote.strip():
            continue
        payload = entry.get("data")
        claims.append(
            WebContextClaim(
                predicate=predicate,
                quote=quote,
                data=payload if isinstance(payload, dict) else {},
            )
        )
    return claims
