"""Operator command that hands one sealed export to the backend Ingestion API.

The command is deliberately thin: preflight and validation belong to `backend_submission`,
package identity to `backend_ingestion`, and the sealed document to the export owner. It can
register and upload exactly one already-sealed package; it never reviews or commits, because
the backend operator owns those editorial decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from collector_cli.backend_submission import (
    ClientCredentialsTokenProvider,
    HttpIngestionTransport,
    SubmissionRefused,
    SubmissionTarget,
    SubmissionTransportError,
    build_submission,
    read_batch,
    read_sealed_export,
    submit,
    verify_selected_scope,
    verify_source_rights,
)
from collector_cli.candidate_selection import SelectionError, parse_candidate_revisions

_SECRET_ENVIRONMENT_VARIABLE = "COLLECTOR_INGESTION_CLIENT_SECRET"  # noqa: S105 - variable name, not a secret
_DEFAULT_SCOPE = "ingestion.upload ingestion.read"


def add_arguments(command: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ingestion = command.add_parser(
        "ingestion",
        help="Submit a sealed export to the backend Ingestion API and read its durable receipt.",
    )
    subcommand = ingestion.add_subparsers(dest="ingestion_command", required=True)

    submit_command = subcommand.add_parser(
        "submit",
        help="Register, upload and complete one exact sealed export package.",
    )
    submit_command.add_argument(
        "--export-path",
        required=True,
        type=Path,
        help="Exact sealed export-package@2 bytes written by 'collector export'.",
    )
    submit_command.add_argument("--campaign-key", required=True)
    submit_command.add_argument(
        "--run-id",
        required=True,
        type=UUID,
        help="Run the sealed package was generated for.",
    )
    submit_command.add_argument(
        "--candidate-revision",
        action="append",
        default=[],
        metavar="CANDIDATE_ID:REVISION",
        help="Exact candidate revision the operator selected for this submission.",
    )
    submit_command.add_argument(
        "--target-site-key", required=True, help="Registered backend target site key."
    )
    submit_command.add_argument(
        "--target-catalog-key", required=True, help="Registered backend target catalog key."
    )
    submit_command.add_argument(
        "--target-configuration-revision-id",
        required=True,
        help="Active Catalog configuration revision registered for this producer.",
    )
    submit_command.add_argument(
        "--producer-identity", required=True, help="Authorized producer identity."
    )
    submit_command.add_argument("--producer-build", required=True)
    submit_command.add_argument("--api-base-url", required=True)
    submit_command.add_argument("--token-endpoint", required=True)
    submit_command.add_argument("--client-id", required=True)
    submit_command.add_argument("--scope", default=_DEFAULT_SCOPE)
    submit_command.add_argument("--correlation-id", required=True)

    status_command = subcommand.add_parser(
        "status",
        help="Read one batch receipt. Read-only: it never registers, uploads or advances state.",
    )
    status_command.add_argument("--batch-id", required=True, type=UUID)
    status_command.add_argument("--api-base-url", required=True)
    status_command.add_argument("--token-endpoint", required=True)
    status_command.add_argument("--client-id", required=True)
    status_command.add_argument("--scope", default=_DEFAULT_SCOPE)
    status_command.add_argument("--correlation-id", required=True)


def run(arguments: argparse.Namespace) -> int:
    try:
        transport = _transport(arguments)
    except SubmissionRefused as exc:
        return _failure(exc.code, str(exc), exc.context, arguments.correlation_id)

    if arguments.ingestion_command == "status":
        try:
            document = read_batch(transport=transport, batch_id=arguments.batch_id)
        except SubmissionTransportError as exc:
            return _failure(exc.code, str(exc), exc.detail, arguments.correlation_id)
        print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    try:
        revisions = _candidate_revisions(arguments.candidate_revision)
        package = read_sealed_export(arguments.export_path)
        verify_selected_scope(package, revisions)
        source_keys = verify_source_rights(package)
        target = SubmissionTarget(
            producer_identity=arguments.producer_identity,
            producer_build=arguments.producer_build,
            target_site_key=arguments.target_site_key,
            target_catalog_key=arguments.target_catalog_key,
            target_configuration_revision_id=arguments.target_configuration_revision_id,
        )
        documents = build_submission(
            package,
            target=target,
            campaign_key=arguments.campaign_key,
            run_id=arguments.run_id,
        )
        receipt = submit(documents=documents, package=package, target=target, transport=transport)
    except SubmissionRefused as exc:
        return _failure(exc.code, str(exc), exc.context, arguments.correlation_id)
    except SubmissionTransportError as exc:
        return _failure(
            exc.code,
            str(exc),
            exc.detail,
            arguments.correlation_id,
            required_action=(
                "Read the batch receipt with 'collector ingestion status' and replay the "
                "identical submission; command identities are derived from the sealed package."
            ),
        )

    document = dict(receipt.as_document())
    document["sourceKeys"] = list(source_keys)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _transport(arguments: argparse.Namespace) -> HttpIngestionTransport:
    secret = os.getenv(_SECRET_ENVIRONMENT_VARIABLE)
    if secret is None or not secret.strip():
        raise SubmissionRefused(
            "SUBMISSION_CLIENT_SECRET_MISSING",
            f"The workload client secret must be provided in {_SECRET_ENVIRONMENT_VARIABLE}.",
            {"environmentVariable": _SECRET_ENVIRONMENT_VARIABLE},
        )
    tokens = ClientCredentialsTokenProvider(
        token_endpoint=arguments.token_endpoint,
        client_id=arguments.client_id,
        client_secret=secret.strip(),
        scope=arguments.scope,
    )
    return HttpIngestionTransport(base_url=arguments.api_base_url, tokens=tokens)


def _candidate_revisions(values: list[str]) -> tuple[tuple[UUID, int], ...]:
    try:
        return parse_candidate_revisions(values)
    except SelectionError as exc:
        raise SubmissionRefused(
            "SUBMISSION_SELECTION_INVALID",
            str(exc),
            {"detail": str(exc)},
        ) from exc


def _failure(
    code: str,
    message: str,
    context: dict[str, str],
    correlation_id: str,
    *,
    required_action: str = (
        "Correct the reported selection, rights or target state and submit the exact same "
        "sealed package again."
    ),
) -> int:
    payload = {
        "type": "collection/ingestion-command-failed",
        "owner": "CollectorCli",
        "code": code,
        "message": message,
        "context": context,
        "requiredAction": required_action,
        "correlationId": correlation_id,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
    return 2
