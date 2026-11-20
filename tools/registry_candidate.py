"""Turn one verified registry record into the canonical candidate revision the owner accepts.

The Berlin studio registry is a research artifact; the collector owns candidates. This module
is the single translation point between them: it builds the owner's `CandidateRevision`
contract from a registry record plus its district evidence. It performs no storage, no policy
decision of its own and no digest of its own vocabulary - `collection_contracts` owns the
contract, `canonical_digest` owns the digests.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from collection_contracts.candidate import (
    CandidateEvidence,
    CandidateRevision,
    candidate_snapshot_digest,
)
from collection_contracts.canonical import canonical_digest

ENTITY_KIND = "place"
CANDIDATE_NAMESPACE = uuid.UUID("6f0f1f0e-1f5f-4b0a-9d2f-8b1f1d0b6a10")
CLUSTER_NAMESPACE = uuid.UUID("6f0f1f0e-1f5f-4b0a-9d2f-8b1f1d0b6a11")
REGISTRY = Path("campaigns") / "berlin_recording_services" / "discovery" / "studios.json"


class RegistryCandidateError(Exception):
    """A registry record cannot become a candidate until the named fact is present."""


def _catalog_categories(record: Mapping[str, Any]) -> list[str]:
    keys = []
    for claim in record.get("service_claims") or []:
        key = claim.get("catalog_category_key")
        if key and key not in keys:
            keys.append(key)
    return keys


def _coordinate(address: Mapping[str, Any]) -> dict[str, float] | None:
    coordinate = address.get("coordinate")
    if isinstance(coordinate, Mapping) and "lat" in coordinate and "lon" in coordinate:
        return {"lat": float(coordinate["lat"]), "lon": float(coordinate["lon"])}
    return None


def _evidence_entries(record: Mapping[str, Any]) -> list[CandidateEvidence]:
    entries: list[CandidateEvidence] = []
    for position, item in enumerate(record.get("field_evidence") or []):
        entries.append(
            CandidateEvidence(
                position=position,
                evidence_kind="source_observation",
                evidence_digest=canonical_digest(item),
            )
        )
    if not entries:
        raise RegistryCandidateError("record has no field evidence; a candidate needs provenance")
    return entries


def build_candidate_revision(
    record: Mapping[str, Any],
    district_evidence: Mapping[str, Any],
    *,
    revision: int = 0,
    recorded_at_utc: str,
    correlation_id: str,
) -> CandidateRevision:
    address = record.get("address") or {}
    district_basis = address.get("district_basis")
    if district_basis != "address_source_verified":
        raise RegistryCandidateError(
            f"district_basis is {district_basis!r}; only an official address record may certify a Bezirk"
        )
    expected = (district_evidence or {}).get("bezirk")
    if not expected:
        raise RegistryCandidateError("district evidence is missing the official Bezirk")
    categories = _catalog_categories(record)
    if not categories:
        raise RegistryCandidateError("record has no catalog category; a candidate must be classifiable")
    domain = (record.get("official_domains") or {}).get("primary")
    if not domain:
        raise RegistryCandidateError("record has no official domain; identity would not be stable")

    payload: dict[str, object] = {
        "entity_kind": ENTITY_KIND,
        "name": record.get("canonical_name"),
        "aliases": list(record.get("aliases") or []),
        "official_domain": domain,
        "address": {
            "street": address.get("street"),
            "street_number": address.get("street_number"),
            "postal_code": address.get("postal_code"),
            "city": address.get("city") or "Berlin",
            "country_code": address.get("country_code") or "DE",
            "district_key": (address.get("district_key") or "").replace("-", "_"),
            "coordinate": _coordinate(address),
        },
        "categories": categories,
        "district_provenance": {
            "method": district_evidence.get("method"),
            "dataset_identifier": district_evidence.get("dataset_identifier"),
            "dataset_version": district_evidence.get("dataset_version"),
            "address_record_id": district_evidence.get("address_record_id"),
            "source_url": district_evidence.get("source_url"),
            "source_digest": district_evidence.get("sha256"),
        },
        "provenance": {
            "registry_id": record.get("registry_id"),
            "import_batch": (record.get("collector") or {}).get("import_batch"),
        },
    }
    evidence = tuple(_evidence_entries(record))
    candidate_id = uuid.uuid5(CANDIDATE_NAMESPACE, domain.strip().lower())
    cluster_id = uuid.uuid5(CLUSTER_NAMESPACE, domain.strip().lower())
    lineage = canonical_digest([entry.evidence_digest for entry in evidence])
    snapshot = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=revision,
        entity_kind=ENTITY_KIND,
        cluster_id=cluster_id,
        resolution_state="resolved",
        normalized_payload=payload,
        evidence=evidence,
        source_lineage_digest=lineage,
    )
    return CandidateRevision(
        candidate_id=candidate_id,
        revision=revision,
        entity_kind=ENTITY_KIND,
        cluster_id=cluster_id,
        resolution_state="resolved",
        snapshot_digest=snapshot,
        source_lineage_digest=lineage,
        normalized_payload=payload,
        evidence=evidence,
        recorded_at_utc=recorded_at_utc,
        correlation_id=correlation_id,
    )


def load_record(registry_path: Path, registry_id: str) -> dict[str, Any]:
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    for record in document.get("records") or []:
        if record.get("registry_id") == registry_id:
            return record
    raise RegistryCandidateError(f"registry record not found: {registry_id}")


def load_district_evidence(path: Path, registry_id: str) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    evidence = (document.get("records") or {}).get(registry_id)
    if not evidence:
        raise RegistryCandidateError(f"no district evidence for {registry_id}")
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a canonical candidate revision from a registry record.")
    parser.add_argument("--registry-id", required=True)
    parser.add_argument("--district-evidence", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--recorded-at-utc", required=True)
    parser.add_argument("--correlation-id", default="registry-candidate")
    parser.add_argument("--revision", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    record = load_record(args.registry, args.registry_id)
    evidence = load_district_evidence(args.district_evidence, args.registry_id)
    revision = build_candidate_revision(
        record,
        evidence,
        revision=args.revision,
        recorded_at_utc=args.recorded_at_utc,
        correlation_id=args.correlation_id,
    )
    args.out.write_text(revision.model_dump_json(indent=1), encoding="utf-8")
    print(f"candidate {revision.candidate_id} revision {revision.revision} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
