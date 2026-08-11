from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

_MANIFEST_NAME = "manifest.json"
_SCHEMA_SUFFIX = ".schema.json"
_WORKER_GATEWAY_OPENAPI_NAME = "worker-gateway.openapi.json"


@dataclass(frozen=True, slots=True)
class ContractTarget:
    file_name: str
    model: type[BaseModel]


@dataclass(frozen=True, slots=True)
class ContractOutput:
    file_name: str
    content: bytes
    digest: str


def contract_targets() -> tuple[ContractTarget, ...]:
    from collection_contracts import (
        AttributesDocument,
        CampaignDocument,
        CampaignSnapshot,
        EntityKindsDocument,
        ErrorEnvelope,
        ManualSeedRow,
        SourceBindingsDocument,
        SourcePolicy,
        TaxonomyDocument,
    )

    return (
        ContractTarget("attributes.schema.json", AttributesDocument),
        ContractTarget("campaign-document.schema.json", CampaignDocument),
        ContractTarget("campaign-snapshot.schema.json", CampaignSnapshot),
        ContractTarget("entity-kinds.schema.json", EntityKindsDocument),
        ContractTarget("error-envelope.schema.json", ErrorEnvelope),
        ContractTarget("manual-seed-row.schema.json", ManualSeedRow),
        ContractTarget("source-bindings.schema.json", SourceBindingsDocument),
        ContractTarget("source-policy.schema.json", SourcePolicy),
        ContractTarget("taxonomy.schema.json", TaxonomyDocument),
    )


def build_outputs(targets: tuple[ContractTarget, ...]) -> tuple[ContractOutput, ...]:
    names = tuple(target.file_name for target in targets)
    if len(names) != len(set(names)):
        raise ValueError("generated contract file names must be unique")
    if any(not name.endswith(_SCHEMA_SUFFIX) for name in names):
        raise ValueError(f"generated contract files must end with {_SCHEMA_SUFFIX}")

    outputs: list[ContractOutput] = []
    manifest_items: list[dict[str, str]] = []
    for target in sorted(targets, key=lambda item: item.file_name):
        schema = target.model.model_json_schema(
            by_alias=True,
            ref_template="#/$defs/{model}",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        content = _canonical_json(schema)
        digest = _sha256(content)
        outputs.append(ContractOutput(target.file_name, content, digest))
        manifest_items.append(
            {
                "file": target.file_name,
                "model": f"{target.model.__module__}.{target.model.__qualname__}",
                "sha256": digest,
            }
        )

    manifest = {
        "contract": "collector-generated-json-schema-manifest",
        "contractRevision": "json-schema-manifest-v1",
        "generator": "tools/contract_generation/generate.py",
        "items": manifest_items,
    }
    manifest_content = _canonical_json(manifest)
    outputs.append(
        ContractOutput(
            _MANIFEST_NAME,
            manifest_content,
            _sha256(manifest_content),
        )
    )
    return tuple(outputs)


def build_worker_gateway_openapi_output() -> ContractOutput:
    from worker_gateway import create_app

    document = create_app().openapi()
    if document.get("openapi") != "3.1.0":
        raise ValueError("Worker Gateway must generate OpenAPI 3.1.0")
    content = _canonical_json(document)
    return ContractOutput(
        _WORKER_GATEWAY_OPENAPI_NAME,
        content,
        _sha256(content),
    )


def synchronize_outputs(
    output_directory: Path,
    outputs: tuple[ContractOutput, ...],
    *,
    check: bool,
) -> tuple[str, ...]:
    expected = {output.file_name: output.content for output in outputs}
    existing = (
        {path.name: path for path in output_directory.glob("*.json")}
        if output_directory.is_dir()
        else {}
    )

    drift: list[str] = []
    for file_name, content in expected.items():
        path = existing.get(file_name)
        if path is None:
            drift.append(f"missing generated contract: {file_name}")
        elif path.read_bytes() != content:
            drift.append(f"stale generated contract: {file_name}")
    for file_name in sorted(set(existing).difference(expected)):
        drift.append(f"unexpected generated contract: {file_name}")

    if check:
        return tuple(drift)

    output_directory.mkdir(parents=True, exist_ok=True)
    for file_name, content in expected.items():
        _atomic_write(output_directory / file_name, content)
    for file_name in set(existing).difference(expected):
        existing[file_name].unlink()
    return ()


def generate(repository_root: Path, *, check: bool) -> tuple[str, ...]:
    root = repository_root.resolve(strict=True)
    schema_drift = synchronize_outputs(
        root / "contracts" / "json_schema",
        build_outputs(contract_targets()),
        check=check,
    )
    openapi_drift = synchronize_outputs(
        root / "contracts" / "openapi",
        (build_worker_gateway_openapi_output(),),
        check=check,
    )
    return schema_drift + openapi_drift


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generate-contracts")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    drift = generate(args.repository_root, check=args.check)
    if drift:
        for message in drift:
            print(message, file=sys.stderr)
        return 1
    if args.check:
        print("Generated contract drift check passed.")
    else:
        print("Generated contracts updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
