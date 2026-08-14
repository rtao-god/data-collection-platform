from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    repair = ROOT / ".github/stage8b_apply_20260814/repair2.py"
    if repair.exists():
        subprocess.run([sys.executable, str(repair)], check=True)

    schemas = ROOT / "apps/control_api/src/control_api/schemas.py"
    text = schemas.read_text(encoding="utf-8")
    text = text.replace(
        "from pydantic import BaseModel, ConfigDict, Field\n",
        "from pydantic import BaseModel, ConfigDict, Field, field_validator\n",
    )
    submit_marker = '''class DecisionResponse(ApiModel):
'''
    submit_validators = '''    @field_validator("rationale")
    @classmethod
    def require_plain_rationale(cls, value: str) -> str:
        return _plain_text(value)

    @field_validator("evidence_references")
    @classmethod
    def require_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("evidenceReferences must be non-empty, unique, and ordered")
        return value


'''
    if "def require_plain_rationale" not in text:
        if submit_marker not in text:
            raise RuntimeError("decision response insertion point is missing")
        text = text.replace(submit_marker, submit_validators + submit_marker, 1)

    manual_marker = '''class ActivateSuppressionRequest(ApiModel):
'''
    manual_validator = '''    @field_validator("value_text")
    @classmethod
    def require_plain_value(cls, value: str) -> str:
        return _plain_text(value)


'''
    if "def require_plain_value" not in text:
        if manual_marker not in text:
            raise RuntimeError("manual observation validator insertion point is missing")
        text = text.replace(manual_marker, manual_validator + manual_marker, 1)

    resolve_marker = '''class ResolveSuppressionRequest(ApiModel):
'''
    suppression_validator = '''    @field_validator("scopes")
    @classmethod
    def require_canonical_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("scopes must be non-empty, unique, and ordered")
        return value


'''
    if "def require_canonical_scopes" not in text:
        if resolve_marker not in text:
            raise RuntimeError("suppression validator insertion point is missing")
        text = text.replace(resolve_marker, suppression_validator + resolve_marker, 1)

    helper = '''

def _plain_text(value: str) -> str:
    if "<" in value or ">" in value:
        raise ValueError("plain text must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\\n\\r\\t" for character in value):
        raise ValueError("plain text contains a forbidden control character")
    return value
'''
    if "def _plain_text" not in text:
        text = text.rstrip() + helper + "\n"
    schemas.write_text(text, encoding="utf-8")

    test = ROOT / "apps/control_api/tests/test_app.py"
    text = test.read_text(encoding="utf-8")
    if "test_decision_rejects_markup_and_noncanonical_evidence" not in text:
        text += '''

def test_decision_rejects_markup_and_noncanonical_evidence() -> None:
    service = Service()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    markup = client(service).post(
        f"/review/cases/{service.case_id}/decisions",
        json={
            "expectedRevision": 0,
            "outcome": "accept_candidate",
            "rationale": "<b>unsafe</b>",
            "evidenceReferences": [DIGEST],
        },
        headers=headers,
    )
    assert markup.status_code == 422

    noncanonical = client(service).post(
        f"/review/cases/{service.case_id}/decisions",
        json={
            "expectedRevision": 0,
            "outcome": "accept_candidate",
            "rationale": "Verified.",
            "evidenceReferences": ["sha256:" + "b" * 64, DIGEST],
        },
        headers=headers,
    )
    assert noncanonical.status_code == 422
'''
        test.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
