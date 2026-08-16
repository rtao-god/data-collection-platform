from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import sqlalchemy as sa
import yaml

_CAMPAIGN_KEY = "berlin_recording_services"
_DEFAULT_COMPOSE = Path("deploy/compose/application.yaml")
_TERMINAL_SUCCESS = frozenset({"succeeded", "completed", "sealed", "verified"})
_TERMINAL_FAILURE = frozenset({"failed", "cancelled", "canceled", "blocked", "dead_letter"})


class LiveProofError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    operation_id: str
    definition: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DatabaseEvidence:
    raw_artifacts: int
    succeeded_work_units: int
    candidate_rows: int
    sealed_exports: int


@dataclass(frozen=True, slots=True)
class LiveProof:
    run_id: UUID
    run_state: str
    coverage: Mapping[str, Any]
    export_id: str
    database: DatabaseEvidence
    control_api_url: str


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(arguments),
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        env=None if env is None else dict(env),
    )


def _compose(
    repository_root: Path,
    compose_file: Path,
    *arguments: str,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ("docker", "compose", "-f", str(compose_file), *arguments),
        cwd=repository_root,
        check=check,
        capture=capture,
    )


def _compose_config(repository_root: Path, compose_file: Path) -> Mapping[str, Any]:
    result = _compose(
        repository_root,
        compose_file,
        "config",
        "--format",
        "json",
    )
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise LiveProofError("resolved application Compose is not JSON") from exc
    if not isinstance(value, Mapping):
        raise LiveProofError("resolved application Compose root must be an object")
    return value


def _service_score(name: str, value: Mapping[str, Any], tokens: Sequence[str]) -> int:
    text = " ".join(
        (
            name,
            str(value.get("image", "")),
            str(value.get("command", "")),
            str(value.get("entrypoint", "")),
        )
    ).lower()
    return sum(20 for token in tokens if token in text)


def _select_service(
    config: Mapping[str, Any],
    *tokens: str,
) -> tuple[str, Mapping[str, Any]]:
    services = config.get("services")
    if not isinstance(services, Mapping):
        raise LiveProofError("Compose has no services map")
    candidates = [
        (name, value, _service_score(str(name), value, tokens))
        for name, value in services.items()
        if isinstance(name, str) and isinstance(value, Mapping)
    ]
    candidates.sort(key=lambda item: (-item[2], item[0]))
    if not candidates or candidates[0][2] <= 0:
        raise LiveProofError(f"Compose has no service matching: {', '.join(tokens)}")
    if len(candidates) > 1 and candidates[0][2] == candidates[1][2]:
        raise LiveProofError(f"Compose service match is ambiguous: {', '.join(tokens)}")
    return candidates[0][0], candidates[0][1]


def _published_ports(service: Mapping[str, Any]) -> tuple[int, ...]:
    values: list[int] = []
    ports = service.get("ports")
    if not isinstance(ports, Sequence) or isinstance(ports, (str, bytes, bytearray)):
        return ()
    for item in ports:
        if isinstance(item, Mapping):
            value = item.get("published")
        elif isinstance(item, str):
            value = item.split(":", 1)[0]
        else:
            continue
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(values)


def _control_api_url(config: Mapping[str, Any]) -> str:
    _, service = _select_service(config, "control", "api")
    ports = _published_ports(service)
    candidates = (*ports, 8000, 8080)
    for port in dict.fromkeys(candidates):
        for path in ("/openapi.json", "/health/ready", "/ready"):
            url = f"http://127.0.0.1:{port}{path}"
            try:
                response = httpx.get(url, timeout=2.0, follow_redirects=False)
            except httpx.HTTPError:
                continue
            if response.status_code < 500:
                return f"http://127.0.0.1:{port}"
    raise LiveProofError("Control API has no reachable published endpoint")


def _wait_for_openapi(base_url: str, timeout_seconds: float) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not started"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                base_url + "/openapi.json",
                timeout=5.0,
                follow_redirects=False,
            )
            if response.status_code == 200:
                value = response.json()
                if isinstance(value, Mapping):
                    return value
            last_error = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(2)
    raise LiveProofError(f"Control API OpenAPI did not become ready: {last_error}")


def _operations(openapi: Mapping[str, Any]) -> tuple[Operation, ...]:
    paths = openapi.get("paths")
    if not isinstance(paths, Mapping):
        raise LiveProofError("OpenAPI has no paths map")
    result: list[Operation] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        for method, definition in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(definition, Mapping):
                continue
            result.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    operation_id=str(definition.get("operationId", "")),
                    definition=definition,
                )
            )
    return tuple(result)


def _operation_text(operation: Operation) -> str:
    return " ".join(
        (
            operation.path,
            operation.operation_id,
            str(operation.definition.get("summary", "")),
            str(operation.definition.get("description", "")),
        )
    ).lower()


def select_operation(
    openapi: Mapping[str, Any],
    *,
    method: str,
    required: Sequence[str],
    forbidden: Sequence[str] = (),
) -> Operation:
    candidates: list[tuple[int, Operation]] = []
    for operation in _operations(openapi):
        if operation.method != method.upper():
            continue
        text = _operation_text(operation)
        if not all(token in text for token in required):
            continue
        if any(token in text for token in forbidden):
            continue
        score = sum(text.count(token) for token in required)
        if "operationid" in text:
            score += 1
        candidates.append((score, operation))
    candidates.sort(key=lambda item: (-item[0], item[1].path, item[1].operation_id))
    if not candidates:
        raise LiveProofError(
            f"OpenAPI has no {method.upper()} operation containing {', '.join(required)}"
        )
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise LiveProofError(
            f"OpenAPI operation selection is ambiguous for {', '.join(required)}"
        )
    return candidates[0][1]


def _resolve_schema(openapi: Mapping[str, Any], schema: object) -> Mapping[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    if not reference.startswith("#/components/schemas/"):
        raise LiveProofError(f"unsupported OpenAPI reference: {reference}")
    name = reference.rsplit("/", 1)[-1]
    components = openapi.get("components")
    if not isinstance(components, Mapping):
        raise LiveProofError("OpenAPI components are missing")
    schemas = components.get("schemas")
    if not isinstance(schemas, Mapping) or not isinstance(schemas.get(name), Mapping):
        raise LiveProofError(f"OpenAPI schema is missing: {name}")
    return schemas[name]


def build_schema_value(
    openapi: Mapping[str, Any],
    schema: object,
    *,
    field_name: str = "",
) -> object:
    value = _resolve_schema(openapi, schema)
    if "default" in value:
        return value["default"]
    if "example" in value:
        return value["example"]
    enum = value.get("enum")
    if isinstance(enum, Sequence) and enum:
        return enum[0]
    lowered = field_name.lower()
    if "campaign" in lowered:
        return _CAMPAIGN_KEY
    if "correlation" in lowered:
        return "berlin-live-proof"
    if "actor" in lowered or "operator" in lowered:
        return "berlin-live-proof-operator"
    if "reason" in lowered:
        return "bounded real-source Berlin live proof"
    if "revision" in lowered:
        return 0
    if lowered.endswith("id") or lowered.endswith("_id"):
        return str(uuid5(NAMESPACE_URL, "data-collection-platform:" + lowered))
    schema_type = value.get("type")
    schema_format = value.get("format")
    if schema_format == "uuid":
        return str(uuid5(NAMESPACE_URL, "data-collection-platform:" + lowered))
    if schema_format == "date-time":
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if schema_type == "object" or "properties" in value:
        properties = value.get("properties")
        if not isinstance(properties, Mapping):
            return {}
        required = value.get("required")
        required_names = set(required) if isinstance(required, Sequence) else set()
        return {
            name: build_schema_value(openapi, child, field_name=str(name))
            for name, child in properties.items()
            if name in required_names
        }
    if schema_type == "array":
        minimum = value.get("minItems", 0)
        if isinstance(minimum, int) and minimum > 0:
            return [
                build_schema_value(openapi, value.get("items"), field_name=field_name)
                for _ in range(minimum)
            ]
        return []
    if schema_type == "integer":
        minimum = value.get("minimum", 0)
        return int(minimum) if isinstance(minimum, (int, float)) else 0
    if schema_type == "number":
        minimum = value.get("minimum", 0)
        return float(minimum) if isinstance(minimum, (int, float)) else 0.0
    if schema_type == "boolean":
        return False
    return "bounded-live-proof"


def _request_body(openapi: Mapping[str, Any], operation: Operation) -> object | None:
    request_body = operation.definition.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return None
    return build_schema_value(openapi, media.get("schema"))


def _parameters(openapi: Mapping[str, Any], operation: Operation) -> tuple[dict[str, str], dict[str, str]]:
    path_values: dict[str, str] = {}
    query_values: dict[str, str] = {}
    parameters = operation.definition.get("parameters")
    if not isinstance(parameters, Sequence):
        parameters = ()
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or location not in {"path", "query"}:
            continue
        value = build_schema_value(openapi, parameter.get("schema"), field_name=name)
        if location == "path":
            path_values[name] = str(value)
        elif parameter.get("required") is True:
            query_values[name] = str(value)
    return path_values, query_values


def _auth_headers(config: Mapping[str, Any]) -> Mapping[str, str]:
    _, service = _select_service(config, "control", "api")
    environment = service.get("environment")
    values: dict[str, str] = {}
    if isinstance(environment, Mapping):
        values.update(
            {
                str(key): str(value)
                for key, value in environment.items()
                if value is not None
            }
        )
    values.update(os.environ)
    for key in (
        "COLLECTOR_CONTROL_API_TOKEN",
        "CONTROL_API_TOKEN",
        "COLLECTOR_OPERATOR_TOKEN",
    ):
        token = values.get(key, "")
        if token and not token.startswith("${"):
            return {"Authorization": "Bearer " + token}
    return {}


def _invoke(
    client: httpx.Client,
    base_url: str,
    openapi: Mapping[str, Any],
    operation: Operation,
    *,
    overrides: Mapping[str, str] | None = None,
) -> object:
    path_values, query_values = _parameters(openapi, operation)
    if overrides:
        path_values.update(overrides)
        query_values.update(
            {key: value for key, value in overrides.items() if "{" + key + "}" not in operation.path}
        )
    path = operation.path
    for key, value in path_values.items():
        path = path.replace("{" + key + "}", quote(value, safe=""))
    if "{" in path:
        raise LiveProofError(f"unresolved OpenAPI path parameters: {path}")
    body = _request_body(openapi, operation)
    response = client.request(
        operation.method,
        base_url + path,
        params=query_values or None,
        json=body,
    )
    if response.status_code >= 300:
        raise LiveProofError(
            f"{operation.method} {operation.path} failed with HTTP {response.status_code}: "
            + response.text[:2_000]
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise LiveProofError(
            f"{operation.method} {operation.path} did not return JSON"
        ) from exc


def _find_value(value: object, names: Sequence[str]) -> object | None:
    lowered_names = {name.lower() for name in names}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in lowered_names:
                return item
        for item in value.values():
            found = _find_value(item, names)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found = _find_value(item, names)
            if found is not None:
                return found
    return None


def _run_id(value: object) -> UUID:
    candidate = _find_value(value, ("runId", "run_id", "collectionRunId"))
    if candidate is None:
        raise LiveProofError("run creation response contains no run identity")
    try:
        return UUID(str(candidate))
    except ValueError as exc:
        raise LiveProofError("run creation response contains an invalid run identity") from exc


def _state(value: object) -> str:
    candidate = _find_value(value, ("state", "status", "runState"))
    return str(candidate).lower() if candidate is not None else "unknown"


def _database_url(config: Mapping[str, Any]) -> str:
    services = config.get("services")
    if not isinstance(services, Mapping):
        raise LiveProofError("Compose services are missing")
    for value in services.values():
        if not isinstance(value, Mapping):
            continue
        environment = value.get("environment")
        if not isinstance(environment, Mapping):
            continue
        database_url = environment.get("COLLECTOR_DATABASE_URL")
        if not isinstance(database_url, str) or not database_url:
            continue
        parsed = re.sub(r"@[^:/]+:(\d+)/", r"@127.0.0.1:\1/", database_url)
        return parsed
    environment_url = os.getenv("COLLECTOR_DATABASE_URL")
    if environment_url:
        return environment_url
    raise LiveProofError("Compose exposes no collector database URL")


def _count(connection: sa.Connection, schema: str, table: str) -> int:
    metadata = sa.MetaData()
    owner = sa.Table(table, metadata, schema=schema, autoload_with=connection)
    return int(connection.scalar(sa.select(sa.func.count()).select_from(owner)) or 0)


def _candidate_count(connection: sa.Connection) -> int:
    inspector = sa.inspect(connection)
    total = 0
    for schema in inspector.get_schema_names():
        for table in inspector.get_table_names(schema=schema):
            if "candidate" not in table.lower():
                continue
            total += _count(connection, schema, table)
    return total


def _sealed_export_count(connection: sa.Connection) -> int:
    inspector = sa.inspect(connection)
    total = 0
    for schema in inspector.get_schema_names():
        for table in inspector.get_table_names(schema=schema):
            if "export" not in table.lower():
                continue
            metadata = sa.MetaData()
            owner = sa.Table(table, metadata, schema=schema, autoload_with=connection)
            state = owner.c.get("state") or owner.c.get("status")
            if state is None:
                continue
            total += int(
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(owner)
                    .where(sa.func.lower(state).in_(("sealed", "verified")))
                )
                or 0
            )
    return total


def database_evidence(database_url: str) -> DatabaseEvidence:
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            raw_artifacts = _count(connection, "sources", "artifact_records")
            work_units = sa.Table(
                "work_units",
                sa.MetaData(),
                schema="work",
                autoload_with=connection,
            )
            succeeded = int(
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(work_units)
                    .where(work_units.c.state == "succeeded")
                )
                or 0
            )
            return DatabaseEvidence(
                raw_artifacts=raw_artifacts,
                succeeded_work_units=succeeded,
                candidate_rows=_candidate_count(connection),
                sealed_exports=_sealed_export_count(connection),
            )
    finally:
        engine.dispose()


def _require_evidence(evidence: DatabaseEvidence) -> None:
    missing = [
        name
        for name, value in (
            ("raw artifacts", evidence.raw_artifacts),
            ("succeeded work units", evidence.succeeded_work_units),
            ("candidate rows", evidence.candidate_rows),
            ("sealed exports", evidence.sealed_exports),
        )
        if value <= 0
    ]
    if missing:
        raise LiveProofError("live run produced no " + ", ".join(missing))


def execute(
    repository_root: Path,
    *,
    compose_file: Path,
    startup_timeout_seconds: float,
    run_timeout_seconds: float,
) -> LiveProof:
    config = _compose_config(repository_root, compose_file)
    _compose(repository_root, compose_file, "up", "-d", "--build", capture=False)
    base_url = _control_api_url(config)
    openapi = _wait_for_openapi(base_url, startup_timeout_seconds)
    headers = _auth_headers(config)
    with httpx.Client(timeout=30.0, headers=headers, follow_redirects=False) as client:
        create_run = select_operation(
            openapi,
            method="POST",
            required=("run",),
            forbidden=("pause", "resume", "cancel", "export", "review"),
        )
        created = _invoke(
            client,
            base_url,
            openapi,
            create_run,
            overrides={"campaignKey": _CAMPAIGN_KEY, "campaign_key": _CAMPAIGN_KEY},
        )
        run_id = _run_id(created)
        get_run = select_operation(
            openapi,
            method="GET",
            required=("run",),
            forbidden=("coverage", "export", "review", "list"),
        )
        get_coverage = select_operation(
            openapi,
            method="GET",
            required=("run", "coverage"),
        )
        deadline = time.monotonic() + run_timeout_seconds
        state = "unknown"
        coverage: object = {}
        while time.monotonic() < deadline:
            overrides = {
                "runId": str(run_id),
                "run_id": str(run_id),
                "collectionRunId": str(run_id),
            }
            status = _invoke(client, base_url, openapi, get_run, overrides=overrides)
            coverage = _invoke(client, base_url, openapi, get_coverage, overrides=overrides)
            state = _state(status)
            if state in _TERMINAL_FAILURE:
                raise LiveProofError(
                    f"Berlin live run ended in {state}: {json.dumps(coverage)[:4_000]}"
                )
            if state in _TERMINAL_SUCCESS:
                break
            time.sleep(5)
        else:
            raise LiveProofError(f"Berlin live run did not terminate; last state={state}")

        create_export = select_operation(
            openapi,
            method="POST",
            required=("export",),
            forbidden=("verify", "download"),
        )
        exported = _invoke(
            client,
            base_url,
            openapi,
            create_export,
            overrides={
                "runId": str(run_id),
                "run_id": str(run_id),
                "collectionRunId": str(run_id),
            },
        )
        export_value = _find_value(exported, ("exportId", "export_id", "id"))
        if export_value is None:
            raise LiveProofError("export materialization response contains no export identity")
        export_id = str(export_value)
        verify_export = select_operation(
            openapi,
            method="POST",
            required=("export", "verify"),
        )
        _invoke(
            client,
            base_url,
            openapi,
            verify_export,
            overrides={
                "exportId": export_id,
                "export_id": export_id,
                "runId": str(run_id),
                "run_id": str(run_id),
            },
        )

    evidence = database_evidence(_database_url(config))
    _require_evidence(evidence)
    if not isinstance(coverage, Mapping):
        raise LiveProofError("coverage endpoint returned a non-object")
    return LiveProof(
        run_id=run_id,
        run_state=state,
        coverage=coverage,
        export_id=export_id,
        database=evidence,
        control_api_url=base_url,
    )


def _write_report(path: Path, proof: LiveProof) -> None:
    value = {
        "contract": "berlin-live-collection-proof",
        "contractRevision": "1",
        "campaignKey": _CAMPAIGN_KEY,
        "runId": str(proof.run_id),
        "runState": proof.run_state,
        "exportId": proof.export_id,
        "databaseEvidence": {
            "rawArtifacts": proof.database.raw_artifacts,
            "succeededWorkUnits": proof.database.succeeded_work_units,
            "candidateRows": proof.database.candidate_rows,
            "sealedExports": proof.database.sealed_exports,
        },
        "coverage": proof.coverage,
        "controlApiOrigin": proof.control_api_url,
        "recordedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--compose-file", type=Path, default=_DEFAULT_COMPOSE)
    parser.add_argument("--startup-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/proofs/berlin-live-collection.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_root = args.repository_root.resolve()
    compose_file = args.compose_file
    if not compose_file.is_absolute():
        compose_file = repository_root / compose_file
    try:
        proof = execute(
            repository_root,
            compose_file=compose_file,
            startup_timeout_seconds=args.startup_timeout_seconds,
            run_timeout_seconds=args.run_timeout_seconds,
        )
        report = args.report
        if not report.is_absolute():
            report = repository_root / report
        _write_report(report, proof)
    except (LiveProofError, httpx.HTTPError, OSError, sa.exc.SQLAlchemyError) as exc:
        print(f"Berlin bounded live proof failed: {exc}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
