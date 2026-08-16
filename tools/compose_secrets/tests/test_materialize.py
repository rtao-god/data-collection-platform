from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_materializer() -> ModuleType:
    path = Path(__file__).parents[1] / "materialize.py"
    spec = importlib.util.spec_from_file_location("compose_secret_materializer", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _environment(*, suffix: str = "one") -> dict[str, str]:
    return {
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID": f"access-{suffix}",
        "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY": f"secret-{suffix}",
        "WORKER_GATEWAY_CREDENTIALS_JSON": json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "worker-gateway-local-credentials-v1",
                "credentials": [
                    {
                        "token": f"worker-token-{suffix}-00000000000000000000000000000000",
                        "workerId": "worker-local",
                        "capabilities": ["http_fetch"],
                    }
                ],
            },
            separators=(",", ":"),
        ),
    }


def test_materialize_writes_exact_private_files(tmp_path: Path) -> None:
    materializer = _load_materializer()
    output = tmp_path / "compose-secrets"

    paths = materializer.materialize(output, _environment())

    assert set(paths) == {
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_SECRET_FILE",
        "COLLECTOR_OBJECT_STORE_SECRET_KEY_SECRET_FILE",
        "WORKER_GATEWAY_CREDENTIALS_SECRET_FILE",
    }
    assert (
        paths["COLLECTOR_OBJECT_STORE_ACCESS_KEY_SECRET_FILE"].read_text(encoding="utf-8")
        == "access-one"
    )
    assert (
        paths["COLLECTOR_OBJECT_STORE_SECRET_KEY_SECRET_FILE"].read_text(encoding="utf-8")
        == "secret-one"
    )
    credential_path = paths["WORKER_GATEWAY_CREDENTIALS_SECRET_FILE"]
    assert json.loads(credential_path.read_text(encoding="utf-8"))["contract"] == (
        "worker-gateway-local-credentials"
    )
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        for path in paths.values():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_materialize_replaces_an_existing_owned_secret_atomically(tmp_path: Path) -> None:
    materializer = _load_materializer()
    output = tmp_path / "compose-secrets"

    first = materializer.materialize(output, _environment(suffix="one"))
    second = materializer.materialize(output, _environment(suffix="two"))

    assert first == second
    assert (
        second["COLLECTOR_OBJECT_STORE_ACCESS_KEY_SECRET_FILE"].read_text(encoding="utf-8")
        == "access-two"
    )
    assert not tuple(output.glob(".*.*"))


def test_missing_source_returns_typed_owner_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    materializer = _load_materializer()
    environment = _environment()
    del environment["COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY"]

    exit_code = materializer.run(
        ["--output-directory", str(tmp_path / "compose-secrets")],
        environment=environment,
    )
    output = capsys.readouterr()
    payload = json.loads(output.err)

    assert exit_code == 2
    assert payload["owner"] == "ApplicationComposeSecrets"
    assert payload["code"] == "COMPOSE_SECRET_SOURCE_MISSING"
    assert payload["context"] == {"sourceVariable": "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY"}


def test_symlink_secret_target_is_rejected(tmp_path: Path) -> None:
    materializer = _load_materializer()
    output = tmp_path / "compose-secrets"
    output.mkdir()
    external = tmp_path / "external"
    external.write_text("must-not-change", encoding="utf-8")
    target = output / "collector-object-store-access-key"
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(materializer.ComposeSecretMaterializationError) as error:
        materializer.materialize(output, _environment())

    assert error.value.code == "COMPOSE_SECRET_TARGET_UNSAFE"
    assert external.read_text(encoding="utf-8") == "must-not-change"
