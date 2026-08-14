from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def add_list_item(text: str, *, section_start: int, assignment: str, item: str) -> str:
    assignment_start = text.index(assignment, section_start)
    list_start = text.index("[", assignment_start)
    list_end = text.index("\n]", list_start)
    rendered = f'  "{item}",'
    if rendered in text[list_start:list_end]:
        return text
    return text[:list_end] + f"\n{rendered}" + text[list_end:]


def add_dependencies(path: Path, dependencies: tuple[str, ...]) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index("dependencies = [")
    end = text.index("\n]", start)
    block = text[start:end]
    additions = ""
    for dependency in dependencies:
        rendered = f'  "{dependency}",'
        if rendered not in block:
            additions += "\n" + rendered
    path.write_text(text[:end] + additions + text[end:], encoding="utf-8")


def update_app_contracts() -> None:
    path = ROOT / "apps/control_api/src/control_api/app.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from review_application import (\n",
        "from review_application import (\n",
    )
    schema_import = '''    ManualObservationRequest,
    ResolveSuppressionRequest,
'''
    replacement = '''    ManualObservationRequest,
    ResolveSuppressionRequest,
'''
    if schema_import not in text:
        raise RuntimeError("control API schema import anchor is missing")
    text = text.replace(schema_import, replacement, 1)
    if "from review_contracts import ManualObservation, SuppressionRevision\n" not in text:
        marker = "from review_application import (\n"
        import_end = text.index(")\n", text.index(marker)) + 2
        text = (
            text[:import_end]
            + "from review_contracts import ManualObservation, SuppressionRevision\n"
            + text[import_end:]
        )
    text = text.replace("response_model=dict,\n        operation_id=\"add_manual_observation\"", "response_model=ManualObservation,\n        operation_id=\"add_manual_observation\"")
    text = text.replace(") -> dict:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        observation = service.add_manual_observation(", ") -> ManualObservation:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        observation = service.add_manual_observation(", 1)
    text = text.replace("        return observation.model_dump(mode=\"json\")\n\n    @app.post(\n        \"/review/suppressions\",\n        response_model=dict,", "        return observation\n\n    @app.post(\n        \"/review/suppressions\",\n        response_model=SuppressionRevision,", 1)
    text = text.replace(") -> dict:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        suppression = service.activate_suppression(", ") -> SuppressionRevision:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        suppression = service.activate_suppression(", 1)
    text = text.replace("        return suppression.model_dump(mode=\"json\")\n\n    @app.post(\n        \"/review/suppressions/{suppression_id}/resolve\",\n        response_model=dict,", "        return suppression\n\n    @app.post(\n        \"/review/suppressions/{suppression_id}/resolve\",\n        response_model=SuppressionRevision,", 1)
    text = text.replace(") -> dict:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        suppression = service.resolve_suppression(", ") -> SuppressionRevision:\n        response.headers[\"X-Correlation-ID\"] = correlation_id\n        suppression = service.resolve_suppression(", 1)
    text = text.replace("        return suppression.model_dump(mode=\"json\")\n\n    return app", "        return suppression\n\n    return app", 1)

    handler_marker = "    @app.exception_handler(ReviewApplicationError)\n"
    http_handler = '''    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        code = "CONTROL_API_UNAUTHORIZED" if exc.status_code == 401 else "CONTROL_API_UNAVAILABLE"
        owner = "ControlApi.Auth" if exc.status_code == 401 else "ControlApi.Readiness"
        action = (
            "Provide a valid reviewer bearer credential."
            if exc.status_code == 401
            else "Restore the Control API dependency and retry."
        )
        body = ErrorResponse(
            code=code,
            owner=owner,
            message=str(exc.detail),
            required_action=action,
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json", by_alias=True),
            headers={"X-Correlation-ID": correlation_id},
        )

'''
    if "async def http_error_handler" not in text:
        if handler_marker not in text:
            raise RuntimeError("Control API exception handler insertion point is missing")
        text = text.replace(handler_marker, http_handler + handler_marker, 1)
    path.write_text(text, encoding="utf-8")


def update_workspace_and_architecture() -> None:
    root_project = ROOT / "pyproject.toml"
    text = root_project.read_text(encoding="utf-8")
    workspace_start = text.index("[tool.uv.workspace]")
    for member in ("apps/control_api", "packages/review_application"):
        text = add_list_item(
            text,
            section_start=workspace_start,
            assignment="members = [",
            item=member,
        )
    mypy_start = text.index("[tool.mypy]")
    for file_path in (
        "apps/control_api/src/control_api",
        "packages/review_application/src/review_application",
        "tools/control_api_contract_generation/generate.py",
    ):
        text = add_list_item(
            text,
            section_start=mypy_start,
            assignment="files = [",
            item=file_path,
        )
    root_project.write_text(text, encoding="utf-8")

    add_dependencies(
        ROOT / "packages/collection_infrastructure/pyproject.toml",
        ("review-application", "review-contracts", "review-core"),
    )

    checker = ROOT / "tools/architecture_checks/check_dependencies.py"
    text = checker.read_text(encoding="utf-8")
    if '"control_api": OwnerPolicy(' not in text:
        marker = '    "collector_cli": OwnerPolicy(\n'
        insertion = '''    "control_api": OwnerPolicy(
        project_path="apps/control_api",
        distribution_name="control-api",
        allowed_internal_imports=(
            "collection_infrastructure",
            "review_application",
            "review_contracts",
        ),
        allowed_external_imports=frozenset({"fastapi", "pydantic", "sqlalchemy", "uvicorn"}),
    ),
'''
        if marker not in text:
            raise RuntimeError("control API architecture insertion point is missing")
        text = text.replace(marker, insertion + marker, 1)
    if '"review_application": OwnerPolicy(' not in text:
        marker = '    "review_contracts": OwnerPolicy(\n'
        insertion = '''    "review_application": OwnerPolicy(
        project_path="packages/review_application",
        distribution_name="review-application",
        allowed_internal_imports=("review_contracts", "review_core"),
        allowed_external_imports=frozenset(),
    ),
'''
        if marker not in text:
            raise RuntimeError("review application architecture insertion point is missing")
        text = text.replace(marker, insertion + marker, 1)

    pattern = re.compile(
        r'(?s)(    "collection_infrastructure": OwnerPolicy\(.*?allowed_internal_imports=\()(.*?)(\),\n        allowed_external_imports=)'
    )
    match = pattern.search(text)
    if match is None:
        raise RuntimeError("collection infrastructure policy was not found")
    existing = set(re.findall(r'"([a-z0-9_]+)"', match.group(2)))
    existing.update({"review_application", "review_contracts", "review_core"})
    rendered = "\n" + "".join(f'            "{value}",\n' for value in sorted(existing)) + "        "
    text = text[: match.start(2)] + rendered + text[match.end(2) :]
    checker.write_text(text, encoding="utf-8")

    policy = subprocess.check_output(
        [sys.executable, str(checker), "--print-policy"],
        text=True,
    ).strip()
    doc = ROOT / "docs/architecture/dependency-rules.md"
    doc_text = doc.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = doc_text.index(start_marker)
    end = doc_text.index(end_marker, start) + len(end_marker)
    doc.write_text(doc_text[:start] + policy + doc_text[end:], encoding="utf-8")


def main() -> int:
    update_app_contracts()
    update_workspace_and_architecture()

    write(
        "apps/control_api/tests/test_auth.py",
        '''from __future__ import annotations

import pytest
from control_api.auth import ReviewAuthenticationError, TokenAuthenticator

TOKEN = "a" * 40


def authenticator() -> TokenAuthenticator:
    return TokenAuthenticator.from_json(
        '{"' + TOKEN + '":{"actorId":"reviewer-1","permissions":["review:read"]}}'
    )


def test_authenticator_returns_configured_principal() -> None:
    principal = authenticator().authenticate(TOKEN)
    assert principal.actor_id == "reviewer-1"
    assert principal.permissions == frozenset({"review:read"})


def test_authenticator_rejects_missing_and_invalid_tokens() -> None:
    value = authenticator()
    with pytest.raises(ReviewAuthenticationError):
        value.authenticate(None)
    with pytest.raises(ReviewAuthenticationError):
        value.authenticate("b" * 40)


def test_authenticator_rejects_unknown_permission() -> None:
    with pytest.raises(ValueError):
        TokenAuthenticator.from_json(
            '{"' + TOKEN + '":{"actorId":"reviewer-1","permissions":["admin"]}}'
        )
''',
    )

    write(
        "apps/control_api/tests/test_app.py",
        '''from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from fastapi.testclient import TestClient
from review_application import ReviewQueuePage
from review_contracts import (
    ReviewCase,
    ReviewDecision,
    deterministic_decision_id,
    review_decision_command_digest,
)

TOKEN = "a" * 40
NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


class Service:
    def __init__(self) -> None:
        self.principal = None
        self.decision_call = None

    def list_cases(self, principal, *, state, limit, cursor):
        self.principal = principal
        return ReviewQueuePage(items=(), next_cursor=None)

    def get_case(self, principal, case_id):
        raise AssertionError("not used")

    def submit_decision(self, principal, **values):
        self.principal = principal
        self.decision_call = values
        case_id = values["case_id"]
        digest = review_decision_command_digest(
            case_id=case_id,
            expected_case_revision=values["expected_revision"],
            outcome=values["outcome"],
            actor_id=principal.actor_id,
            rationale=values["rationale"],
            evidence_references=values["evidence_references"],
            supersedes_decision_id=values["supersedes_decision_id"],
        )
        decision_id = deterministic_decision_id(case_id, 1, digest)
        case = ReviewCase(
            case_id=case_id,
            candidate_id=uuid4(),
            candidate_revision=0,
            revision=1,
            state="decided",
            reason_codes=("MATCH_REVIEW",),
            current_decision_id=decision_id,
            opened_at_utc=NOW,
            recorded_at_utc=NOW,
            correlation_id=values["correlation_id"],
        )
        decision = ReviewDecision(
            decision_id=decision_id,
            case_id=case_id,
            case_revision=1,
            outcome=values["outcome"],
            actor_id=principal.actor_id,
            rationale=values["rationale"],
            evidence_references=values["evidence_references"],
            supersedes_decision_id=None,
            command_digest=digest,
            decided_at_utc=NOW,
            correlation_id=values["correlation_id"],
        )
        return case, decision

    def add_manual_observation(self, principal, **values):
        raise AssertionError("not used")

    def activate_suppression(self, principal, **values):
        raise AssertionError("not used")

    def resolve_suppression(self, principal, **values):
        raise AssertionError("not used")


def client(service: Service) -> TestClient:
    auth = TokenAuthenticator.from_json(
        '{"'
        + TOKEN
        + '":{"actorId":"reviewer-1","permissions":["review:read","review:decide","review:observe","review:suppress"]}}'
    )
    return TestClient(
        create_app(service=service, authenticator=auth, readiness_probe=lambda: True)
    )


def test_missing_token_returns_typed_401() -> None:
    response = client(Service()).get("/review/cases")
    assert response.status_code == 401
    assert response.json()["code"] == "CONTROL_API_UNAUTHORIZED"
    assert "token" not in str(response.json()).lower()


def test_queue_uses_authenticated_principal() -> None:
    service = Service()
    response = client(service).get(
        "/review/cases",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}
    assert service.principal.actor_id == "reviewer-1"


def test_decision_actor_cannot_be_supplied_by_request() -> None:
    service = Service()
    case_id = uuid4()
    body = {
        "expectedRevision": 0,
        "outcome": "accept_candidate",
        "rationale": "Verified.",
        "evidenceReferences": [DIGEST],
        "actorId": "attacker",
    }
    rejected = client(service).post(
        f"/review/cases/{case_id}/decisions",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rejected.status_code == 422

    body.pop("actorId")
    accepted = client(service).post(
        f"/review/cases/{case_id}/decisions",
        json=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Correlation-ID": "decision-api-test",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["decision"]["actor_id"] == "reviewer-1"
    assert service.decision_call["correlation_id"] == "decision-api-test"
''',
    )

    write(
        "tools/control_api_contract_generation/generate.py",
        '''from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import cast

from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from review_application import ReviewService

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts/control_api"


class _Service:
    pass


def render() -> dict[str, str]:
    authenticator = TokenAuthenticator.from_json(
        '{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":'
        '{"actorId":"contract-generator","permissions":["review:read"]}}'
    )
    app = create_app(
        service=cast(ReviewService, _Service()),
        authenticator=authenticator,
        readiness_probe=lambda: True,
    )
    schema = app.openapi()
    openapi = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    operations = []
    for path, path_item in sorted(schema["paths"].items()):
        for method, operation in sorted(path_item.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "operationId": operation["operationId"],
                    "path": path,
                }
            )
    inventory = json.dumps(
        {
            "contract": "collector-control-api-operation-inventory",
            "contractRevision": "control-api-operation-inventory-v1",
            "operations": operations,
            "openapiDigest": f"sha256:{sha256(openapi.encode('utf-8')).hexdigest()}",
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    return {"openapi.json": openapi, "operation-inventory.json": inventory}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        drift = [
            name
            for name, content in expected.items()
            if not (OUTPUT / name).exists()
            or (OUTPUT / name).read_text(encoding="utf-8") != content
        ]
        if drift:
            raise SystemExit("Control API contract drift: " + ", ".join(drift))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    )
    write(
        "tools/control_api_contract_generation/tests/test_generate.py",
        '''from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_control_api_contract_artifacts_are_current() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [sys.executable, "tools/control_api_contract_generation/generate.py", "--check"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
''',
    )

    write(
        "deploy/docker/control-api.Dockerfile",
        '''FROM python:3.13.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \\
    UV_LINK_MODE=copy
WORKDIR /workspace
RUN pip install --no-cache-dir uv==0.10.0
COPY .python-version pyproject.toml uv.lock ./
COPY apps ./apps
COPY connectors ./connectors
COPY packages ./packages
RUN uv sync --frozen --no-dev --no-editable --package control-api

FROM python:3.13.14-slim AS runtime
ENV PATH="/workspace/.venv/bin:${PATH}" \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 control-api \\
    && useradd --uid 10001 --gid control-api --create-home control-api
WORKDIR /workspace
COPY --from=build --chown=control-api:control-api /workspace/.venv /workspace/.venv
USER 10001:10001
ENTRYPOINT ["uvicorn", "control_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
''',
    )

    write(
        "docs/specifications/stage8b-control-api.md",
        '''# Stage 8B — review command adapter and Control API

## Owners

`review_application` owns reviewer permissions, command construction, opaque queue cursors, and orchestration against `ReviewRepository`.

`PostgresReviewRepository` owns atomic optimistic-concurrency transactions, exact command replay, immutable supersession, queue reads, and mapping between PostgreSQL history and review contracts.

`control_api` owns authenticated HTTP transport. Actor identity is derived only from the bearer principal; request bodies cannot select or override the actor.

## Invariants

- Missing or invalid credentials fail with 401 and never appear in logs or error bodies.
- Permissions are explicit per read, decision, observation, and suppression operation.
- Review and suppression writes compare expected revisions inside the database transaction.
- A repeated exact command digest returns the immutable prior result; different content under the same digest is a conflict.
- Manual observations append evidence and never overwrite candidate snapshots or source observations.
- A replacement decision must explicitly supersede the current decision.
- Suppression identity and expiry cannot change during resolution.
- Control API startup never runs migrations.

## Deferred owner

The React review console consumes the generated OpenAPI contract in the next sequential block. It does not own scores, decisions, or export eligibility.
''',
    )
    write(
        ".codex/modules/control-api.md",
        '''# Control API module

- Application owner: `packages/review_application`.
- PostgreSQL owner: `PostgresReviewRepository` in collection infrastructure.
- HTTP composition root: `apps/control_api`.
- Reviewer actor identity comes from authenticated bearer configuration only.
- OpenAPI is generated into `contracts/control_api` and checked for drift.
- Startup migration is forbidden.
''',
    )

    status = ROOT / "docs/implementation-status.md"
    text = status.read_text(encoding="utf-8")
    marker = "## Stage 8B — review command adapter and Control API"
    if marker not in text:
        text = text.rstrip() + f'''\n\n{marker}\n\nStatus: **application, PostgreSQL adapter, authenticated API, generated OpenAPI, and image implemented; frontend remains**.\n\n- Actor identity is derived from the authenticated principal, not request data.\n- Decisions, observations, and suppressions use exact command digests and optimistic concurrency.\n- Review queue pagination uses opaque cursors.\n- Control API startup does not run migrations.\n- The React review console is the next Stage 8 owner.\n'''
        status.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
