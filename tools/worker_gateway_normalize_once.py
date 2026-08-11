from __future__ import annotations

import subprocess
from pathlib import Path


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"expected {label} fragment was not found")
    return text.replace(old, new, 1)


def _patch_architecture_owner() -> None:
    path = Path("tools/architecture_checks/check_dependencies.py")
    text = path.read_text(encoding="utf-8")
    if '"worker_gateway": OwnerPolicy(' not in text:
        anchor = '    "collection_migration": OwnerPolicy(\n'
        owner = '''    "worker_gateway": OwnerPolicy(
        project_path="apps/worker_gateway",
        distribution_name="worker-gateway",
        allowed_internal_imports=(
            "collection_application",
            "collection_contracts",
            "collection_infrastructure",
        ),
        allowed_external_imports=frozenset(
            {"fastapi", "pydantic", "sqlalchemy", "uvicorn"}
        ),
    ),
'''
        text = _replace_once(text, anchor, owner + anchor, "architecture owner insertion")
    path.write_text(text, encoding="utf-8")


def _patch_gateway_app() -> None:
    path = Path("apps/worker_gateway/src/worker_gateway/app.py")
    text = path.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        "from typing import Annotated, TypeVar, cast",
        "from typing import Annotated, cast",
        "gateway typing import",
    )
    text = _replace_once(
        text,
        "from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response",
        "from fastapi import APIRouter, Depends, FastAPI, Request, Response",
        "gateway FastAPI import",
    )
    text = _replace_once(
        text,
        "from fastapi.responses import JSONResponse",
        "from fastapi.responses import JSONResponse\n"
        "from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer",
        "gateway security import",
    )
    text = _replace_once(
        text,
        '_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,99}$")\n'
        "_CommandT = TypeVar(\"_CommandT\")\n",
        '_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,99}$")\n'
        '_WORKER_BEARER = HTTPBearer(auto_error=False, scheme_name="WorkerBearer")\n',
        "gateway module constants",
    )
    text = _replace_once(
        text,
        '''def _authenticate_worker(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> WorkerPrincipal:
    return _dependencies(request).authenticator.authenticate(authorization)
''',
        '''def _authenticate_worker(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_WORKER_BEARER),
    ],
) -> WorkerPrincipal:
    authorization = (
        f"{credentials.scheme} {credentials.credentials}"
        if credentials is not None
        else None
    )
    return _dependencies(request).authenticator.authenticate(authorization)
''',
        "gateway authentication dependency",
    )
    text = _replace_once(
        text,
        "def _command(factory: Callable[[], _CommandT]) -> _CommandT:",
        "def _command[CommandT](factory: Callable[[], CommandT]) -> CommandT:",
        "gateway command generic",
    )
    path.write_text(text, encoding="utf-8")


def _patch_auth_names() -> None:
    path = Path("apps/worker_gateway/src/worker_gateway/auth.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace("_SECRET_CONTRACT_REVISION", "_CREDENTIAL_DOCUMENT_REVISION")
    text = text.replace("_SECRET_CONTRACT", "_CREDENTIAL_DOCUMENT_CONTRACT")
    path.write_text(text, encoding="utf-8")


def _patch_contract_names() -> None:
    path = Path("apps/worker_gateway/src/worker_gateway/contracts.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace("_TOKEN_PATTERN", "_WIRE_IDENTITY_PATTERN")
    path.write_text(text, encoding="utf-8")


def _patch_app_tests() -> None:
    path = Path("apps/worker_gateway/tests/test_app.py")
    text = path.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        "from __future__ import annotations\n\nfrom datetime",
        "from __future__ import annotations\n\nfrom collections.abc import Callable\nfrom datetime",
        "gateway app-test imports",
    )
    text = _replace_once(
        text,
        "    readiness_probe: object | None = None,\n",
        "    readiness_probe: Callable[[], None] | None = None,\n",
        "gateway readiness probe annotation",
    )
    text = _replace_once(
        text,
        "    probe = readiness_probe if callable(readiness_probe) else lambda: None\n",
        "    probe = readiness_probe or (lambda: None)\n",
        "gateway readiness probe selection",
    )
    path.write_text(text, encoding="utf-8")


def _patch_main_tests() -> None:
    path = Path("apps/worker_gateway/tests/test_main.py")
    text = path.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        '    monkeypatch.setenv("WORKER_GATEWAY_HOST", "0.0.0.0")\n',
        '    monkeypatch.setenv("WORKER_GATEWAY_HOST", "0.0.0.0")  # noqa: S104\n',
        "non-local bind security fixture",
    )
    path.write_text(text, encoding="utf-8")


def _patch_runtime_test() -> None:
    path = Path("database/tests/test_worker_gateway_runtime.py")
    text = path.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        "from __future__ import annotations\n\nimport os",
        "from __future__ import annotations\n\nimport os\nfrom collections.abc import Iterator",
        "gateway runtime-test imports",
    )
    if "def engine() -> Iterator[Engine]:" not in text:
        anchor = "def _digest(*parts: str) -> str:\n"
        fixture = '''@pytest.fixture
def engine() -> Iterator[Engine]:
    value = sa.create_engine(_database_url(), poolclass=NullPool)
    try:
        yield value
    finally:
        value.dispose()


'''
        text = _replace_once(text, anchor, fixture + anchor, "gateway runtime fixture")
    text = _replace_once(
        text,
        "    assert work == {",
        "    assert dict(work) == {",
        "gateway persisted row comparison",
    )
    path.write_text(text, encoding="utf-8")


def _patch_composition_order() -> None:
    path = Path("apps/worker_gateway/src/worker_gateway/__main__.py")
    text = path.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        "    engine = sa.create_engine(database_url, pool_pre_ping=True)\n"
        "    authenticator = WorkerAuthenticator.from_secret_file(credential_file)\n",
        "    authenticator = WorkerAuthenticator.from_secret_file(credential_file)\n"
        "    engine = sa.create_engine(database_url, pool_pre_ping=True)\n",
        "gateway composition ordering",
    )
    path.write_text(text, encoding="utf-8")


def _patch_architecture_test() -> None:
    path = Path("tools/architecture_checks/tests/test_check_dependencies.py")
    text = path.read_text(encoding="utf-8")
    if "test_worker_gateway_rejects_direct_domain_dependency" not in text:
        text += '''


def test_worker_gateway_rejects_direct_domain_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/worker_gateway/src/worker_gateway/app.py",
        "from collection_domain import WorkLease\\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner collection_domain" in violations[0].message
'''
    path.write_text(text, encoding="utf-8")


def _regenerate_dependency_document() -> None:
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    policy = subprocess.check_output(
        ["python", "tools/architecture_checks/check_dependencies.py", "--print-policy"],
        text=True,
    ).strip()
    path = Path("docs/architecture/dependency-rules.md")
    content = path.read_text(encoding="utf-8")
    start = content.index(start_marker)
    end = content.index(end_marker, start) + len(end_marker)
    path.write_text(content[:start] + policy + content[end:], encoding="utf-8")


def main() -> None:
    _patch_architecture_owner()
    _patch_gateway_app()
    _patch_auth_names()
    _patch_contract_names()
    _patch_app_tests()
    _patch_main_tests()
    _patch_runtime_test()
    _patch_composition_order()
    _patch_architecture_test()
    _regenerate_dependency_document()


if __name__ == "__main__":
    main()
