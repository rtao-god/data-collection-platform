from __future__ import annotations

from uuid import UUID

import pytest

from tools.live_collection.run_bounded_live_proof import (
    DatabaseEvidence,
    LiveProofError,
    _find_value,
    _require_evidence,
    build_schema_value,
    select_operation,
)


def _openapi() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/campaigns/{campaignKey}/runs": {
                "post": {
                    "operationId": "createCampaignRun",
                    "summary": "Create collection run",
                    "parameters": [
                        {
                            "name": "campaignKey",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateRun"}
                            }
                        }
                    },
                }
            },
            "/runs/{runId}/pause": {
                "post": {
                    "operationId": "pauseRun",
                    "summary": "Pause run",
                }
            },
            "/runs/{runId}/coverage": {
                "get": {
                    "operationId": "getRunCoverage",
                    "summary": "Get run coverage",
                }
            },
        },
        "components": {
            "schemas": {
                "CreateRun": {
                    "type": "object",
                    "required": ["campaignKey", "actorId", "correlationId"],
                    "properties": {
                        "campaignKey": {"type": "string"},
                        "actorId": {"type": "string"},
                        "correlationId": {"type": "string"},
                        "optional": {"type": "string"},
                    },
                }
            }
        },
    }


def test_create_run_selection_excludes_lifecycle_mutations() -> None:
    operation = select_operation(
        _openapi(),
        method="POST",
        required=("run",),
        forbidden=("pause", "resume", "cancel", "export"),
    )

    assert operation.path == "/campaigns/{campaignKey}/runs"
    assert operation.operation_id == "createCampaignRun"


def test_required_request_fields_receive_deterministic_live_values() -> None:
    schema = {"$ref": "#/components/schemas/CreateRun"}

    value = build_schema_value(_openapi(), schema)

    assert value == {
        "campaignKey": "berlin_recording_services",
        "actorId": "berlin-live-proof-operator",
        "correlationId": "berlin-live-proof",
    }


def test_nested_identity_lookup_finds_run_id() -> None:
    value = {"result": {"run": {"runId": "00000000-0000-0000-0000-000000000123"}}}

    assert _find_value(value, ("runId", "run_id")) == (
        "00000000-0000-0000-0000-000000000123"
    )
    assert UUID(str(_find_value(value, ("runId",))))


def test_live_proof_requires_all_persistent_evidence() -> None:
    with pytest.raises(LiveProofError, match="candidate rows"):
        _require_evidence(
            DatabaseEvidence(
                raw_artifacts=2,
                succeeded_work_units=2,
                candidate_rows=0,
                sealed_exports=1,
            )
        )


def test_complete_persistent_evidence_is_accepted() -> None:
    _require_evidence(
        DatabaseEvidence(
            raw_artifacts=2,
            succeeded_work_units=2,
            candidate_rows=1,
            sealed_exports=1,
        )
    )
