from __future__ import annotations

from collection_contracts import owner_error


def test_error_envelope_serializes_transport_aliases() -> None:
    error = owner_error(
        error_type="collection/example-failure",
        owner="ExampleOwner",
        code="EXAMPLE_FAILURE",
        message="The example failed.",
        context={"expected": "valid", "actual": "invalid"},
        required_action="Correct the example.",
        correlation_id="correlation-1",
    )

    payload = error.envelope.model_dump(mode="json", by_alias=True)

    assert payload["requiredAction"] == "Correct the example."
    assert payload["correlationId"] == "correlation-1"
    assert "required_action" not in payload
    assert "correlation_id" not in payload
