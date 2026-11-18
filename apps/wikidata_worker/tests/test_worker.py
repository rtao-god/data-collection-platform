from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from collection_contracts import (
    WikidataClassificationSpec,
    WikidataEntityQuerySpec,
    canonical_wikidata_query_spec_document,
    decode_wikidata_entity_result,
)
from wikidata_studios import WikidataFetchFailure, WikidataFetchResult
from wikidata_worker.gateway import SdkWikidataWorkerGateway
from wikidata_worker.worker import WikidataWorker, WikidataWorkerPolicy

_RUN_ID = UUID("77777777-7777-5777-8777-777777777777")
_NOW = datetime(2026, 2, 22, 15, 0, tzinfo=UTC)
_SPEC_ARTIFACT_ID = UUID("88888888-8888-5888-8888-888888888888")

# A response shaped exactly like `action=wbgetentities&ids=Q667023`.
_RESPONSE = {
    "entities": {
        "Q667023": {
            "type": "item",
            "id": "Q667023",
            "lastrevid": 2465661990,
            "labels": {"de": {"language": "de", "value": "Hansa Studios"}},
            "claims": {
                "P31": [
                    {
                        "id": "Q667023$30d3e507-46e6-72b2-a4b6-5f3398a53f13",
                        "mainsnak": {
                            "snaktype": "value",
                            "datavalue": {
                                "type": "wikibase-entityid",
                                "value": {"id": "Q746369"},
                            },
                        },
                    }
                ]
            },
        }
    },
    "success": 1,
}


def _spec_bytes() -> bytes:
    spec = WikidataEntityQuerySpec(
        run_id=_RUN_ID,
        binding_key="wikidata_studios_berlin",
        entity_ids=("Q667023",),
        label_languages=("de", "en"),
        classification=WikidataClassificationSpec(
            studio_class_id="Q746369",
            campaign_category="recording_studio",
        ),
        source_policy_digest=f"sha256:{'a' * 64}",
    )
    return canonical_wikidata_query_spec_document(spec)


def _digest(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


class _Artifact:
    def __init__(self, artifact_id: UUID) -> None:
        self.artifact_id = artifact_id


class _Lease:
    def __init__(self) -> None:
        self.work_id = UUID("bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb")
        self.expected_output_contract = "wikidata-entity-result@2"
        self._artifacts = {"wikidata_entity_query_spec": _Artifact(_SPEC_ARTIFACT_ID)}

    def artifact(self, role: str) -> _Artifact:
        return self._artifacts[role]


class _Gateway:
    def __init__(self, *, spec: bytes | None = None, lease: _Lease | None = None) -> None:
        self._spec = spec
        self._lease = lease
        self.published: list[tuple[bytes, bytes, str]] = []
        self.failures: list[dict[str, object]] = []

    def acquire(self, *, lease_duration_seconds: int, heartbeat_interval_seconds: int):
        del lease_duration_seconds, heartbeat_interval_seconds
        lease, self._lease = self._lease, None
        return lease

    def heartbeat(self, lease, *, lease_duration_seconds: int, heartbeat_interval_seconds: int):
        del lease_duration_seconds, heartbeat_interval_seconds
        return lease

    def read_artifact(self, lease, *, role: str, maximum_bytes: int) -> bytes:
        del maximum_bytes
        assert role == "wikidata_entity_query_spec"
        assert self._spec is not None
        return self._spec

    def publish_result(self, lease, *, raw_response: bytes, result_envelope: bytes) -> None:
        self.published.append((raw_response, result_envelope, lease.expected_output_contract))

    def fail(self, lease, *, failure_kind, error_code, message, retry_after_seconds=None):
        self.failures.append(
            {
                "kind": failure_kind,
                "code": error_code,
                "message": message,
                "retryAfterSeconds": retry_after_seconds,
            }
        )


class _Fetcher:
    def __init__(self, result: WikidataFetchResult | Exception) -> None:
        self._result = result
        self.requests: list[object] = []

    def fetch(self, request):
        self.requests.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _fetch_result(body: bytes | None = None) -> WikidataFetchResult:
    payload = body if body is not None else json.dumps(_RESPONSE).encode("utf-8")
    return WikidataFetchResult(
        body=payload,
        content_digest=_digest(payload),
        content_type="application/json",
    )


def _worker(gateway: _Gateway, fetcher: _Fetcher) -> WikidataWorker:
    return WikidataWorker(
        gateway,
        fetcher,
        policy=WikidataWorkerPolicy(lease_duration_seconds=60, heartbeat_interval_seconds=20),
        clock=lambda: _NOW,
    )


def test_worker_publishes_the_exact_raw_response_and_its_result_envelope() -> None:
    gateway = _Gateway(spec=_spec_bytes(), lease=_Lease())
    fetcher = _Fetcher(_fetch_result())

    assert _worker(gateway, fetcher).run_once() is True

    assert len(gateway.published) == 1
    raw_response, envelope_content, contract = gateway.published[0]
    assert contract == "wikidata-entity-result@2"
    assert raw_response == _fetch_result().body
    envelope = decode_wikidata_entity_result(envelope_content)
    assert envelope.requested_entity_ids == ("Q667023",)
    assert envelope.retrieved_at_utc == _NOW
    assert envelope.response_digest == _fetch_result().content_digest
    # The published envelope already carries the adapted record, so the planner never
    # has to re-run source semantics to know what was accepted.
    assert envelope.accepted_entity_ids == ("Q667023",)
    assert envelope.records[0].source_record_id == "wikidata:Q667023"
    assert envelope.records[0].campaign_category == "recording_studio"


def _catalog_result_digest(*, contract: str, raw: bytes, envelope: bytes) -> str:
    """The digest the collector re-derives from the bound output artifacts."""

    return _digest(
        json.dumps(
            {
                "contract": contract,
                "envelopeDigest": _digest(envelope),
                "rawResponseDigest": _digest(raw),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


class _SdkClient:
    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []
        self.uploads: list[dict[str, object]] = []
        self.completions: list[dict[str, object]] = []

    def register(self, **kwargs: object) -> None:
        self.registrations.append(kwargs)

    def upload_bytes(self, lease, *, content: bytes, artifact_kind: str, content_type: str):
        upload_id = UUID(f"00000000-0000-5000-8000-{len(self.uploads):012d}")
        self.uploads.append(
            {
                "kind": artifact_kind,
                "contentType": content_type,
                "content": content,
                "uploadId": upload_id,
            }
        )
        return type(
            "Upload",
            (),
            {"upload_id": upload_id, "content_digest": _digest(content)},
        )()

    def complete(self, lease, **kwargs: object) -> None:
        self.completions.append(kwargs)


def test_gateway_completes_with_the_digest_the_catalog_recomputes() -> None:
    client = _SdkClient()
    gateway = SdkWikidataWorkerGateway(client)  # type: ignore[arg-type]
    gateway.register(build_identity="wikidata-worker@test")
    lease = _Lease()
    raw = json.dumps(_RESPONSE).encode("utf-8")
    envelope = b'{"contract":"wikidata-entity-result@2"}'

    gateway.publish_result(lease, raw_response=raw, result_envelope=envelope)

    completion = client.completions[0]
    assert completion["output_contract"] == "wikidata-entity-result@2"
    assert completion["output_digest"] == _catalog_result_digest(
        contract="wikidata-entity-result@2",
        raw=raw,
        envelope=envelope,
    )
    assert [item["kind"] for item in client.uploads] == ["raw_artifact", "diagnostic_artifact"]
    assert completion["output_artifacts"] == (
        (client.uploads[0]["uploadId"], "wikidata_raw_response"),
        (client.uploads[1]["uploadId"], "wikidata_entity_result"),
    )


def test_worker_requests_exactly_the_declared_entity_set_at_its_read_time() -> None:
    gateway = _Gateway(spec=_spec_bytes(), lease=_Lease())
    fetcher = _Fetcher(_fetch_result())

    _worker(gateway, fetcher).run_once()

    request = fetcher.requests[0]
    assert request.entity_ids == ("Q667023",)
    assert request.retrieved_at_utc == _NOW
    assert request.run_id == _RUN_ID


def test_worker_reports_a_transient_fetch_failure_with_its_retry_after() -> None:
    gateway = _Gateway(spec=_spec_bytes(), lease=_Lease())
    fetcher = _Fetcher(
        WikidataFetchFailure(
            kind="transient",
            code="WIKIDATA_ENDPOINT_RATE_LIMITED",
            message="The Wikidata endpoint asked the caller to slow down.",
            status_code=429,
            retry_after_seconds=30,
        )
    )

    assert _worker(gateway, fetcher).run_once() is True

    assert gateway.published == []
    assert gateway.failures == [
        {
            "kind": "transient",
            "code": "WIKIDATA_ENDPOINT_RATE_LIMITED",
            "message": "The Wikidata endpoint asked the caller to slow down.",
            "retryAfterSeconds": 30,
        }
    ]


def test_worker_refuses_a_result_that_answers_a_different_entity_set() -> None:
    gateway = _Gateway(spec=_spec_bytes(), lease=_Lease())
    body = json.dumps({"entities": {}, "success": 1}).encode("utf-8")

    _worker(gateway, _Fetcher(_fetch_result(body))).run_once()

    assert gateway.published == []
    assert gateway.failures[0]["kind"] == "contract_invalid"
    assert gateway.failures[0]["code"] == "WIKIDATA_WORK_INPUT_INVALID"


def test_worker_refuses_a_query_spec_that_is_not_the_published_bytes() -> None:
    spec = canonical_wikidata_query_spec_document(
        WikidataEntityQuerySpec(
            run_id=_RUN_ID,
            binding_key="wikidata_studios_berlin",
            entity_ids=("Q667023",),
            label_languages=("de", "en"),
            classification=WikidataClassificationSpec(
                studio_class_id="Q746369",
                campaign_category="recording_studio",
            ),
            source_policy_digest=f"sha256:{'a' * 64}",
        )
    )
    gateway = _Gateway(spec=spec + b"\n", lease=_Lease())
    fetcher = _Fetcher(_fetch_result())

    _worker(gateway, fetcher).run_once()

    assert fetcher.requests == []
    assert gateway.failures[0]["code"] == "WIKIDATA_WORK_INPUT_INVALID"
