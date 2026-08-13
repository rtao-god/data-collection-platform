from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "packages/observation_contracts/src/observation_contracts/contracts.py",
    "        field_key: str = Field(alias=\"fieldKey\")\n        maximum_length: int = Field(alias=\"maximumLength\", ge=1, le=2000)\n",
    "        field_key: str = Field(alias=\"fieldKey\", pattern=r\"^[a-z][a-z0-9_]{0,99}$\")\n        maximum_length: int = Field(alias=\"maximumLength\", ge=1, le=2000)\n",
)
replace(
    "packages/observation_contracts/src/observation_contracts/contracts.py",
    "            if not result or any(_FIELD_KEY.fullmatch(item) is None for item in result):\n",
    "            if any(_FIELD_KEY.fullmatch(item) is None for item in result):\n",
)
replace(
    "packages/normalization_core/src/normalization_core/normalizers.py",
    "from decimal import Decimal, InvalidOperation\n",
    "from decimal import Decimal, InvalidOperation\nfrom typing import Literal\n",
)
replace(
    "packages/normalization_core/src/normalization_core/normalizers.py",
    "_CURRENCY = re.compile(r\"^[A-Z]{3}$\")\n",
    "_CURRENCY = re.compile(r\"^[A-Z]{3}$\")\n_NUMERIC_MONEY = re.compile(r\"^[0-9]+(?:[.,][0-9]+)*(?:[.,][0-9]+)?$\")\n\nNormalizationKind = Literal[\"text\", \"phone\", \"email\", \"url\", \"address\", \"money\"]\n",
)
replace(
    "packages/normalization_core/src/normalization_core/normalizers.py",
    "        kind: str\n",
    "        kind: NormalizationKind\n",
)
replace(
    "packages/normalization_core/src/normalization_core/normalizers.py",
    "        numeric = raw.replace(\" \", \"\")\n",
    "        numeric = raw.replace(\" \", \"\")\n        if _NUMERIC_MONEY.fullmatch(numeric) is None:\n            raise ValueError(\"money amount is invalid\")\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "        Diagnostic,\n",
    "        AssessmentState,\n        Diagnostic,\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "        ObservationBundle,\n",
    "        ObservationBundle,\n        StructuredSyntax,\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "            syntax: str | None,\n",
    "            syntax: StructuredSyntax | None,\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "            syntax = \"microdata\" if item_property is not None else \"rdfa\" if rdfa_property is not None else None\n",
    "            syntax: StructuredSyntax | None = (\n                \"microdata\" if item_property is not None else \"rdfa\" if rdfa_property is not None else None\n            )\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "        syntax: str | None,\n",
    "        syntax: StructuredSyntax | None,\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "        syntax: str | None,\n",
    "        syntax: StructuredSyntax | None,\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "                    walk(child, child_pointer, depth + 1)\n",
    "                    if mapped == \"address\" and isinstance(child, dict):\n                        continue\n                    walk(child, child_pointer, depth + 1)\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "                evidence = tuple(sorted(set(evidence_values), key=lambda item: item.identity()))\n",
    "                evidence_by_identity = {item.identity(): item for item in evidence_values}\n                evidence = tuple(evidence_by_identity[key] for key in sorted(evidence_by_identity))\n",
)
replace(
    "packages/extraction_core/src/extraction_core/engine.py",
    "            if len(field_observations) == 1:\n                state = \"observed\"\n",
    "            state: AssessmentState\n            if len(field_observations) == 1:\n                state = \"observed\"\n",
)
replace(
    "apps/extraction_worker/src/extraction_worker/gateway.py",
    "            self._client.complete(\n",
    "            if upload.content_digest != bundle.digest():\n                raise RuntimeError(\"extraction bundle upload digest does not match canonical bundle\")\n            self._client.complete(\n",
)
replace(
    "packages/normalization_core/tests/test_normalizers.py",
    '        value = normalize_phone("030 123456", default_region="DE")\n\n        assert value.value == "+4930123456"\n',
    '        value = normalize_phone("030 12345678", default_region="DE")\n\n        assert value.value == "+493012345678"\n',
)
replace(
    "packages/extraction_core/tests/test_engine.py",
    '"telephone":"030 123456",\n',
    '"telephone":"030 12345678",\n',
)
