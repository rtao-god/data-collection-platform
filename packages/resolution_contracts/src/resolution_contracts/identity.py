from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: object) -> str:
    content = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def candidate_pair_id(left: UUID, right: UUID) -> str:
    if left == right:
        raise ValueError("candidate pair requires two distinct IDs")
    ordered = tuple(sorted((left, right), key=lambda value: value.hex))
    return canonical_digest([str(ordered[0]), str(ordered[1])])


def deterministic_cluster_id(member_candidate_ids: Sequence[UUID]) -> UUID:
    members = tuple(sorted(set(member_candidate_ids), key=lambda value: value.hex))
    if not members:
        raise ValueError("cluster membership cannot be empty")
    identity = "entity-resolution-cluster:" + ",".join(str(value) for value in members)
    return uuid5(NAMESPACE_URL, identity)
