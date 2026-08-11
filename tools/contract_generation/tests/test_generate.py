from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


def _load_generator() -> ModuleType:
    path = Path(__file__).parents[1] / "generate.py"
    spec = importlib.util.spec_from_file_location("contract_generator", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AlphaContract(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    alpha: str


class BetaContract(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    beta: int


def _targets(generator: ModuleType) -> tuple[object, ...]:
    return (
        generator.ContractTarget("beta.schema.json", BetaContract),
        generator.ContractTarget("alpha.schema.json", AlphaContract),
    )


def test_outputs_are_deterministic_and_manifest_digests_match() -> None:
    generator = _load_generator()

    first = generator.build_outputs(_targets(generator))
    second = generator.build_outputs(tuple(reversed(_targets(generator))))

    assert first == second
    manifest = json.loads(first[-1].content)
    assert [item["file"] for item in manifest["items"]] == [
        "alpha.schema.json",
        "beta.schema.json",
    ]
    output_by_name = {output.file_name: output for output in first}
    for item in manifest["items"]:
        assert item["sha256"] == output_by_name[item["file"]].digest


def test_worker_gateway_openapi_is_deterministic_and_owner_scoped() -> None:
    generator = _load_generator()

    first = generator.build_worker_gateway_openapi_output()
    second = generator.build_worker_gateway_openapi_output()

    assert first == second
    assert first.file_name == "worker-gateway.openapi.json"
    document = json.loads(first.content)
    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {
        "/health/live",
        "/health/ready",
        "/worker/capabilities",
        "/worker/leases/acquire",
        "/worker/leases/{lease_id}/heartbeat",
        "/worker/registrations",
        "/worker/work/{work_id}/complete",
        "/worker/work/{work_id}/fail",
        "/worker/work/{work_id}/release",
    }
    operation_ids = [
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert document["components"]["securitySchemes"] == {
        "WorkerBearer": {"scheme": "bearer", "type": "http"}
    }
    assert document["paths"]["/worker/leases/acquire"]["post"]["security"] == [
        {"WorkerBearer": []}
    ]
    assert "security" not in document["paths"]["/health/live"]["get"]


def test_check_reports_missing_stale_and_unexpected_files(tmp_path: Path) -> None:
    generator = _load_generator()
    outputs = generator.build_outputs(_targets(generator))
    output_directory = tmp_path / "contracts"
    output_directory.mkdir()
    (output_directory / "alpha.schema.json").write_text("{}\n", encoding="utf-8")
    (output_directory / "obsolete.json").write_text("{}\n", encoding="utf-8")

    drift = generator.synchronize_outputs(output_directory, outputs, check=True)

    assert "stale generated contract: alpha.schema.json" in drift
    assert "missing generated contract: beta.schema.json" in drift
    assert "missing generated contract: manifest.json" in drift
    assert "unexpected generated contract: obsolete.json" in drift


def test_write_mode_replaces_owned_directory_atomically(tmp_path: Path) -> None:
    generator = _load_generator()
    outputs = generator.build_outputs(_targets(generator))
    output_directory = tmp_path / "contracts"
    output_directory.mkdir()
    (output_directory / "obsolete.json").write_text("{}\n", encoding="utf-8")

    assert generator.synchronize_outputs(output_directory, outputs, check=False) == ()
    assert generator.synchronize_outputs(output_directory, outputs, check=True) == ()
    assert {path.name for path in output_directory.iterdir()} == {
        "alpha.schema.json",
        "beta.schema.json",
        "manifest.json",
    }
