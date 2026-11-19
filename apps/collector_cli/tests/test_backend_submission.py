"""Submission tests over a real sealed export-package@2 document.

The document is sealed by the contract's own sealer and the wire documents are produced by the
existing adapter, so these tests exercise the production submission path: preflight, manifest
identity, deterministic command identities and the exact transport order. Every fixture is
marked as a TEST-ONLY synthetic source; nothing here claims real-source rights.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from collection_contracts.export_package import (
    ExportPackagePayload,
    canonical_export_package_json,
    seal_export_candidate,
    seal_export_package,
)
from collector_cli import run
from collector_cli.backend_ingestion import payload_object_key
from collector_cli.backend_submission import (
    SubmissionRefused,
    SubmissionTarget,
    build_submission,
    read_sealed_export,
    submit,
    verify_selected_scope,
    verify_source_rights,
)

_RUN_ID = UUID("00000000-0000-0000-0000-00000000d001")
_CANDIDATE_ID = UUID("00000000-0000-0000-0000-00000000a001")
_BATCH_ID = UUID("00000000-0000-0000-0000-00000000e001")
_NOW = datetime(2026, 11, 16, 0, 3, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64
_POLICY_DIGEST = "sha256:" + "b" * 64
_DECISION_DIGEST = "sha256:" + "c" * 64
_SOURCE_KEY = "synthetic_e2e_fixture"

_TARGET = SubmissionTarget(
    producer_identity="data-collection-platform",
    producer_build="submission-test",
    target_site_key="berlin-recording",
    target_catalog_key="berlin-recording-services",
    target_configuration_revision_id="00000000-0000-0000-0000-00000000d002",
)


def _evidence(*, usage_policy: str = "publishable", legal_status: str = "approved") -> Any:
    return {
        "fieldPath": "names",
        "sourceKey": _SOURCE_KEY,
        "sourceRecordId": "synthetic:record:1",
        "sourceUrl": "https://synthetic.invalid/record/1",
        "observedAtUtc": "2026-11-16T00:01:59+00:00",
        "legalStatus": legal_status,
        "usagePolicy": usage_policy,
        "evidenceDigest": _DIGEST,
        "sourcePolicyDigest": _POLICY_DIGEST,
        "attribution": "TEST-ONLY synthetic evidence",
    }


def _entry(
    *,
    usage_policy: str = "publishable",
    legal_status: str = "approved",
    decision_kind: str = "accept_candidate",
    revision: int = 0,
) -> Any:
    return seal_export_candidate(
        {
            "candidateId": str(_CANDIDATE_ID),
            "candidateRevision": revision,
            "snapshotDigest": "sha256:" + "d" * 64,
            "clusterId": "00000000-0000-0000-0000-00000000a002",
            "entityKind": "organization",
            "localizedNames": [
                {
                    "locale": "de-DE",
                    "state": "observed",
                    "value": "TEST-ONLY Studio Berlin",
                    "fieldPath": "names",
                }
            ],
            "contacts": [],
            "typedAttributes": [],
            "categoryKeys": ["recording_studio"],
            "geography": {
                "coverage": "inside",
                "marketAreaRevision": "berlin_boundary_v1",
                "marketAreaDigest": "sha256:" + "e" * 64,
                "latitude": 52.5007771,
                "longitude": 13.4168436,
                "fieldPath": "geography",
            },
            "fieldEvidence": [
                _evidence(usage_policy=usage_policy, legal_status=legal_status),
                {
                    **_evidence(usage_policy=usage_policy, legal_status=legal_status),
                    "fieldPath": "categories/recording_studio",
                },
                {
                    **_evidence(usage_policy=usage_policy, legal_status=legal_status),
                    "fieldPath": "geography",
                },
            ],
            "quality": {"state": "passed", "issues": []},
            "qualityEvaluationDigest": "sha256:" + "f" * 64,
            "qualityPolicyDigest": "sha256:" + "1" * 64,
            "reviewDecisions": [
                {
                    "decisionId": "00000000-0000-0000-0000-00000000c001",
                    "decisionKind": decision_kind,
                    "decisionDigest": _DECISION_DIGEST,
                    "decidedAtUtc": "2026-11-16T00:02:00+00:00",
                }
            ],
            "sourcePolicies": [
                {
                    "sourceKey": _SOURCE_KEY,
                    "policyDigest": _POLICY_DIGEST,
                    "legalStatus": legal_status,
                    "usagePolicy": usage_policy,
                    "attribution": "TEST-ONLY synthetic evidence",
                }
            ],
            "sourceArtifactIds": ["00000000-0000-0000-0000-00000000b001"],
            "contentDigest": "sha256:" + "0" * 64,
        }
    )


def _sealed_export(**kwargs: Any) -> Any:
    payload = {
        "runId": str(_RUN_ID),
        "campaignBundleDigest": "sha256:" + "2" * 64,
        "campaignKey": "berlin_recording_services",
        "exportProfileKey": "berlin_recording_services",
        "exportProfileRevision": "revision_3",
        "qualityProfileDigest": "sha256:" + "3" * 64,
        "catalogBinding": {
            "siteKey": "berlin-recording",
            "catalogKey": "berlin-recording-services",
            "configurationRevisionId": _TARGET.target_configuration_revision_id,
            "coordinatePrecisionMeters": 25,
            "categories": [
                {"campaignCategory": "recording_studio", "catalogCategory": "recording-studio"}
            ],
            "nameLocales": [{"campaignLocale": "de-DE", "catalogLocale": "de-DE"}],
        },
        "entries": [_entry(**kwargs).model_dump(mode="json", by_alias=True)],
        "generatedAtUtc": _NOW.isoformat(),
        "correlationId": "corr-submission",
    }
    return seal_export_package(ExportPackagePayload.model_validate(payload))


def _payload_digest_in(document: Any) -> str | None:
    """Find the registered payload artifact digest anywhere in a submission document."""

    if isinstance(document, dict):
        if (
            isinstance(document.get("contentDigest"), str)
            and document.get("role") == "candidatePayload"
        ):
            return str(document["contentDigest"])
        for value in document.values():
            found = _payload_digest_in(value)
            if found is not None:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _payload_digest_in(value)
            if found is not None:
                return found
    return None


class _FakeTransport:
    def __init__(self, *, object_key: str) -> None:
        self.calls: list[tuple[str, str, Mapping[str, Any] | None, str | None]] = []
        self._object_key = object_key
        self._registered_digest: str | None = None

    def post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_statuses: tuple[int, ...],
    ) -> Mapping[str, Any]:
        self.calls.append(("post", path, body, idempotency_key))
        self._registered_digest = _payload_digest_in(body) or self._registered_digest
        if path == "/api/ingestion/batches":
            return {
                "batch": {"id": str(_BATCH_ID), "state": "Registered", "aggregateRevision": 0},
                "replayed": False,
            }
        if path.endswith("/upload-request"):
            return {
                "uploadUri": "https://objects.invalid/upload",
                "requiredHeaders": {"x-amz-meta-sha256": self._registered_digest},
                "objectKey": self._object_key,
                "contentType": "application/json",
                "batch": {"id": str(_BATCH_ID), "state": "Uploading", "aggregateRevision": 1},
                "replayed": False,
            }
        return {
            "batch": {"id": str(_BATCH_ID), "state": "Uploaded", "aggregateRevision": 2},
            "replayed": False,
        }

    def get_json(self, path: str) -> Mapping[str, Any]:
        self.calls.append(("get", path, None, None))
        return {"id": str(_BATCH_ID), "state": "Uploaded"}

    def put_object(
        self,
        url: str,
        *,
        content: bytes,
        content_type: str,
        content_digest: str,
        signed_headers: Mapping[str, str],
    ) -> None:
        self.calls.append(
            (
                "put",
                url,
                {"contentDigest": content_digest, "signedHeaders": dict(signed_headers)},
                None,
            )
        )


class _UploadHeaderTamperingTransport(_FakeTransport):
    """Authorization whose signed headers do not bind the registered payload digest."""

    def __init__(self, *, object_key: str, digest: str | None) -> None:
        super().__init__(object_key=object_key)
        self._tampered_digest = digest

    def post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_statuses: tuple[int, ...],
    ) -> Mapping[str, Any]:
        response = dict(
            super().post_json(
                path, body, idempotency_key=idempotency_key, expected_statuses=expected_statuses
            )
        )
        if path.endswith("/upload-request"):
            if self._tampered_digest is None:
                response.pop("requiredHeaders", None)
            else:
                response["requiredHeaders"] = {"x-amz-meta-sha256": self._tampered_digest}
        return response


def _documents(export: Any, **kwargs: Any) -> Any:
    return build_submission(
        export, target=_TARGET, campaign_key="berlin_recording_services", run_id=_RUN_ID
    )


def test_the_manifest_carries_one_payload_artifact_at_the_registered_key() -> None:
    export = _sealed_export()

    documents = _documents(export)

    artifacts = documents.manifest["artifacts"]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["role"] == "candidatePayload"
    assert artifact["objectKey"] == payload_object_key(export)
    assert artifact["contentDigest"] == documents.payload_digest
    assert artifact["size"] == len(documents.payload_bytes)
    assert documents.manifest["itemCount"] == 1
    assert documents.manifest_digest == documents.manifest_digest.lower()


def test_submission_registers_uploads_and_completes_in_that_order() -> None:
    export = _sealed_export()
    documents = _documents(export)
    transport = _FakeTransport(object_key=documents.object_key)

    receipt = submit(documents=documents, package=export, target=_TARGET, transport=transport)

    assert [call[0] for call in transport.calls] == ["post", "post", "put", "post"]
    assert transport.calls[0][1] == "/api/ingestion/batches"
    assert transport.calls[1][1].endswith("/upload-request")
    assert transport.calls[3][1].endswith("/upload-complete")
    assert receipt.batch_id == _BATCH_ID
    assert receipt.state == "Uploaded"
    assert receipt.item_keys == (str(_CANDIDATE_ID),)
    assert receipt.export_digest == export.package_digest


def test_the_registered_manifest_digest_is_the_canonical_manifest_digest() -> None:
    from collector_cli.backend_canonical import compute_digest

    export = _sealed_export()

    documents = _documents(export)

    assert documents.manifest_digest == compute_digest(documents.manifest)


def test_replaying_the_same_package_reuses_the_same_command_identities() -> None:
    export = _sealed_export()
    first = _FakeTransport(object_key=payload_object_key(export))
    second = _FakeTransport(object_key=payload_object_key(export))

    submit(documents=_documents(export), package=export, target=_TARGET, transport=first)
    submit(documents=_documents(export), package=export, target=_TARGET, transport=second)

    first_keys = [call[3] for call in first.calls if call[0] == "post"]
    second_keys = [call[3] for call in second.calls if call[0] == "post"]
    assert first_keys == second_keys
    assert len(set(first_keys)) == 3, "each backend command owns its own identity"


def test_a_different_package_derives_different_command_identities() -> None:
    first = _sealed_export()
    second = _sealed_export(revision=1)
    first_transport = _FakeTransport(object_key=payload_object_key(first))
    second_transport = _FakeTransport(object_key=payload_object_key(second))

    submit(documents=_documents(first), package=first, target=_TARGET, transport=first_transport)
    submit(documents=_documents(second), package=second, target=_TARGET, transport=second_transport)

    assert first_transport.calls[0][3] != second_transport.calls[0][3]


def test_an_upload_authorization_bound_to_other_content_is_refused_before_upload() -> None:
    export = _sealed_export()
    transport = _UploadHeaderTamperingTransport(
        object_key=payload_object_key(export), digest="0" * 64
    )

    with pytest.raises(SubmissionRefused) as failure:
        submit(documents=_documents(export), package=export, target=_TARGET, transport=transport)

    assert failure.value.code == "SUBMISSION_UPLOAD_DIGEST_MISMATCH"
    assert not any(call[0] == "put" for call in transport.calls)


def test_an_upload_authorization_without_signed_digest_header_is_refused() -> None:
    export = _sealed_export()
    transport = _UploadHeaderTamperingTransport(object_key=payload_object_key(export), digest=None)

    with pytest.raises(SubmissionRefused) as failure:
        submit(documents=_documents(export), package=export, target=_TARGET, transport=transport)

    assert failure.value.code == "SUBMISSION_UPLOAD_HEADERS_MISSING"
    assert not any(call[0] == "put" for call in transport.calls)


def test_a_research_only_policy_is_refused_before_any_network_call() -> None:
    export = _sealed_export(legal_status="research_only")

    with pytest.raises(SubmissionRefused) as captured:
        verify_source_rights(export)

    assert captured.value.code == "SUBMISSION_SOURCE_RIGHTS_BLOCKED"
    assert captured.value.context["sourceKey"] == _SOURCE_KEY
    assert captured.value.context["legalStatus"] == "research_only"


def test_link_only_evidence_is_refused_rather_than_uploaded() -> None:
    export = _sealed_export(usage_policy="link_only")

    with pytest.raises(SubmissionRefused) as captured:
        verify_source_rights(export)

    assert captured.value.code == "SUBMISSION_SOURCE_RIGHTS_BLOCKED"


def test_a_candidate_without_an_accepting_decision_is_refused() -> None:
    export = _sealed_export(decision_kind="request_changes")

    with pytest.raises(SubmissionRefused) as captured:
        verify_source_rights(export)

    assert captured.value.code == "SUBMISSION_CANDIDATE_NOT_ACCEPTED"


def test_a_selection_the_package_does_not_cover_is_refused() -> None:
    export = _sealed_export()

    with pytest.raises(SubmissionRefused) as captured:
        verify_selected_scope(export, ((_CANDIDATE_ID, 1),))

    assert captured.value.code == "SUBMISSION_SELECTION_MISMATCH"
    assert _CANDIDATE_ID.__str__() in captured.value.context["missingFromPackage"]


def test_an_empty_selection_cannot_submit_anything() -> None:
    export = _sealed_export()

    with pytest.raises(SubmissionRefused) as captured:
        verify_selected_scope(export, ())

    assert captured.value.code == "SUBMISSION_SELECTION_EMPTY"


def test_a_package_for_another_run_is_refused() -> None:
    export = _sealed_export()

    with pytest.raises(SubmissionRefused) as captured:
        build_submission(
            export,
            target=_TARGET,
            campaign_key="berlin_recording_services",
            run_id=UUID("00000000-0000-0000-0000-00000000d009"),
        )

    assert captured.value.code == "SUBMISSION_RUN_MISMATCH"


def test_sealed_bytes_that_are_not_canonical_are_refused(tmp_path: Path) -> None:
    export = _sealed_export()
    rewritten = tmp_path / "export.json"
    rewritten.write_text(
        json.dumps(json.loads(canonical_export_package_json(export)), indent=2), encoding="utf-8"
    )

    with pytest.raises(SubmissionRefused) as captured:
        read_sealed_export(rewritten)

    assert captured.value.code == "SUBMISSION_EXPORT_NOT_CANONICAL"


def test_the_cli_submit_command_refuses_without_workload_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    export = _sealed_export()
    artifact = tmp_path / "export.json"
    artifact.write_text(canonical_export_package_json(export), encoding="utf-8")
    monkeypatch.delenv("COLLECTOR_INGESTION_CLIENT_SECRET", raising=False)

    exit_code = run(
        [
            "ingestion",
            "submit",
            "--export-path",
            str(artifact),
            "--campaign-key",
            "berlin_recording_services",
            "--run-id",
            str(_RUN_ID),
            "--candidate-revision",
            f"{_CANDIDATE_ID}:0",
            "--target-site-key",
            "berlin-recording",
            "--target-catalog-key",
            "berlin-recording-services",
            "--target-configuration-revision-id",
            _TARGET.target_configuration_revision_id,
            "--producer-identity",
            "data-collection-platform",
            "--producer-build",
            "test",
            "--api-base-url",
            "https://ingestion.invalid",
            "--token-endpoint",
            "https://identity.invalid/token",
            "--client-id",
            "collector",
            "--correlation-id",
            "corr-cli",
        ]
    )
    payload = json.loads(capsys.readouterr().err)

    assert exit_code == 2
    assert payload["code"] == "SUBMISSION_CLIENT_SECRET_MISSING"
    assert payload["owner"] == "CollectorCli"
