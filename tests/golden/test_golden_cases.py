"""Golden tests: un fixture congelado por caso A–H, con JSON esperado versionado.

Si un cambio del parser/extractor altera la salida, el diff estructurado indica
exactamente qué cambió. Regenerar los esperados SOLO tras revisión manual
(script en el propio test module: python -m tests.golden.test_golden_cases).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kipu_knowledge.adapters.extraction.deterministic import DeterministicExtractor
from kipu_knowledge.adapters.parsing.html_parser import ElPeruanoHtmlParser
from kipu_knowledge.domain.contracts import SourceReference

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "elperuano"
EXPECTED = Path(__file__).parent / "expected"

CASES = {
    "A": "2540861-1",
    "B1": "2540903-1",
    "B2": "2540903-2",
    "C": "2540905-3",
    "D": "2540905-4",
    "E": "2540779-1",
    "F": "2540702-1",
    "G": "2540905-2",
    "H": "2540896-1",
}


def _actual_payload(code: str) -> dict:
    ref = SourceReference(
        "EL_PERUANO_NL", "NL", code, f"https://busquedas.elperuano.pe/dispositivo/NL/{code}"
    )
    doc = ElPeruanoHtmlParser().parse((FIXTURES / f"{code}.html").read_bytes(), ref)
    result = DeterministicExtractor().extract(doc)
    return {
        "document": {
            "publication_code": doc.publication_code,
            "document_type_code": str(doc.document_type_code),
            "number_normalized": doc.number_normalized,
            "issued_on": doc.issued_on.isoformat() if doc.issued_on else None,
            "published_on": doc.published_on.isoformat() if doc.published_on else None,
            "issue_place_raw": doc.issue_place_raw,
            "title_raw": doc.title_raw,
            # Congelado por caso: el token del path es opaco y un regex demasiado
            # estrecho lo pierde en silencio en vez de fallar.
            "pdf_url": doc.pdf_url,
        },
        "extraction": result.model_dump(mode="json"),
    }


@pytest.mark.parametrize("case,code", CASES.items(), ids=list(CASES.keys()))
def test_golden(case: str, code: str):
    expected = json.loads((EXPECTED / f"{code}.json").read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(_actual_payload(code), sort_keys=True, ensure_ascii=False))
    assert actual == expected, f"La salida del caso {case} ({code}) difiere del golden"
