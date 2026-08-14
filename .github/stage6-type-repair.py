from __future__ import annotations

from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected Stage 6 source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    _replace_once(
        Path("packages/collection_contracts/src/collection_contracts/observations.py"),
        """def _require_utc(name: str, value: datetime) -> None:\n    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:\n        raise ValueError(f\"{name} must be timezone-aware UTC\")\n""",
        """def _require_utc(name: str, value: datetime) -> None:\n    offset = value.utcoffset()\n    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:\n        raise ValueError(f\"{name} must be timezone-aware UTC\")\n""",
    )

    extractor = Path("packages/extraction_core/src/extraction_core/extractor.py")
    _replace_once(
        extractor,
        "from typing import Protocol, cast\n",
        "from typing import Literal, Protocol, cast\n",
    )
    _replace_once(
        extractor,
        '_SCHEMA_TYPE_TO_ENTITY_KINDS: dict[str, tuple[str, ...]] = {\n',
        '_EntityKindCandidate = Literal["organization", "place", "provider"]\n\n'
        '_SCHEMA_TYPE_TO_ENTITY_KINDS: dict[str, tuple[_EntityKindCandidate, ...]] = {\n',
    )
    _replace_once(
        extractor,
        "    entity_kinds: set[str]\n",
        "    entity_kinds: set[_EntityKindCandidate]\n",
    )

    normalizer = Path("packages/normalization_core/src/normalization_core/normalizer.py")
    _replace_once(
        normalizer,
        """    for rule in profile.field_rules:\n        target_fields.add(rule.target_field)\n        source_fields = tuple(fields_by_key.get(rule.source_field, ()))\n        if not source_fields:\n            observations.append(_missing_observation(record, profile, rule.target_field))\n            continue\n        for source_field in source_fields:\n            if rule.target_field in profile.prohibited_fields:\n                observations.append(\n                    _prohibited_observation(record, profile, rule.target_field, source_field.evidence)\n                )\n                continue\n            observation, issue = _normalize_field(record, profile, rule, source_field)\n""",
        """    for field_rule in profile.field_rules:\n        target_fields.add(field_rule.target_field)\n        source_fields = tuple(fields_by_key.get(field_rule.source_field, ()))\n        if not source_fields:\n            observations.append(_missing_observation(record, profile, field_rule.target_field))\n            continue\n        for source_field in source_fields:\n            if field_rule.target_field in profile.prohibited_fields:\n                observations.append(\n                    _prohibited_observation(\n                        record,\n                        profile,\n                        field_rule.target_field,\n                        source_field.evidence,\n                    )\n                )\n                continue\n            observation, issue = _normalize_field(record, profile, field_rule, source_field)\n""",
    )
    _replace_once(
        normalizer,
        """    for rule in profile.boolean_pattern_rules:\n        target_fields.add(rule.target_field)\n        if rule.target_field in profile.prohibited_fields:\n            matching = _first_matching_block(record, (*rule.negative_patterns, *rule.positive_patterns))\n            observations.append(\n                _prohibited_observation(\n                    record,\n                    profile,\n                    rule.target_field,\n                    matching.evidence if matching is not None else None,\n                )\n                if matching is not None\n                else _missing_observation(record, profile, rule.target_field)\n            )\n            continue\n        observations.append(_normalize_boolean_pattern(record, profile, rule.target_field, rule))\n""",
        """    for boolean_rule in profile.boolean_pattern_rules:\n        target_fields.add(boolean_rule.target_field)\n        if boolean_rule.target_field in profile.prohibited_fields:\n            matching = _first_matching_block(\n                record,\n                (*boolean_rule.negative_patterns, *boolean_rule.positive_patterns),\n            )\n            observations.append(\n                _prohibited_observation(\n                    record,\n                    profile,\n                    boolean_rule.target_field,\n                    matching.evidence if matching is not None else None,\n                )\n                if matching is not None\n                else _missing_observation(record, profile, boolean_rule.target_field)\n            )\n            continue\n        observations.append(\n            _normalize_boolean_pattern(\n                record,\n                profile,\n                boolean_rule.target_field,\n                boolean_rule,\n            )\n        )\n""",
    )
    _replace_once(
        normalizer,
        """        normalized, registrable = _normalize_url(value.value)\n        return UrlObservationValue(\n            original=value.value,\n            normalized=normalized,\n""",
        """        normalized_url, registrable = _normalize_url(value.value)\n        return UrlObservationValue(\n            original=value.value,\n            normalized=normalized_url,\n""",
    )
    _replace_once(
        normalizer,
        """        normalized = tuple(dict.fromkeys(_normalize_text(item) for item in value.values))\n        return StringSetObservationValue(\n            original_values=value.values,\n            normalized_values=normalized,\n""",
        """        normalized_values = tuple(\n            dict.fromkeys(_normalize_text(item) for item in value.values)\n        )\n        return StringSetObservationValue(\n            original_values=value.values,\n            normalized_values=normalized_values,\n""",
    )

    worker = Path("apps/processing_worker/src/processing_worker/worker.py")
    _replace_once(worker, "from typing import cast\n\n", "")
    _replace_once(
        worker,
        '            cast(WorkFailureKind, "transient"),\n',
        '            "transient",\n',
    )

    _replace_once(
        Path("pyproject.toml"),
        'module = ["extruct", "phonenumbers", "tldextract"]\n',
        'module = ["extruct", "lxml", "lxml.*", "phonenumbers", "tldextract"]\n',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
