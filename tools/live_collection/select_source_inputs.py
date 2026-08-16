from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

_CAMPAIGN = PurePosixPath("campaigns/berlin_recording_services")
_PROTECTED_PATHS = frozenset(
    {
        "campaign.yaml",
        "geography.yaml",
        "discovery/manual_seeds.csv",
        "geography/berlin-boundary.geojson",
        "geography/berlin-boundary.provenance.json",
    }
)
_REQUIRED_TOKENS = ("osm", "http")
_FORBIDDEN_TEXT = (
    "example.invalid",
    "example.com",
    "localhost",
    "127.0.0.1",
    "mock-server",
)
_MAX_HISTORY = 2_000


class SourceInputSelectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Candidate:
    commit_sha: str
    files: Mapping[str, bytes]
    score: int


@dataclass(frozen=True, slots=True)
class SelectedInputs:
    commit_sha: str
    binding_keys: tuple[str, ...]
    files: tuple[str, ...]
    content_digest: str


def _git(repository_root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.check_output(
            ("git", "-C", str(repository_root), *arguments),
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise SourceInputSelectionError(
            f"git command failed: {' '.join(arguments)}"
        ) from exc


def _try_git(repository_root: Path, *arguments: str) -> bytes | None:
    try:
        return subprocess.check_output(
            ("git", "-C", str(repository_root), *arguments),
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None


def _history(repository_root: Path) -> tuple[str, ...]:
    output = _git(
        repository_root,
        "rev-list",
        "--all",
        "--date-order",
        f"--max-count={_MAX_HISTORY}",
        "--",
        str(_CAMPAIGN / "source_bindings.yaml"),
        str(_CAMPAIGN / "source_policies.yaml"),
    ).decode("utf-8")
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _tree_files(repository_root: Path, commit_sha: str) -> tuple[str, ...]:
    output = _try_git(
        repository_root,
        "ls-tree",
        "-r",
        "--name-only",
        commit_sha,
        str(_CAMPAIGN),
    )
    if output is None:
        return ()
    prefix = str(_CAMPAIGN) + "/"
    return tuple(
        line[len(prefix) :]
        for line in output.decode("utf-8").splitlines()
        if line.startswith(prefix)
    )


def _candidate_files(repository_root: Path, commit_sha: str) -> Mapping[str, bytes]:
    result: dict[str, bytes] = {}
    for relative in _tree_files(repository_root, commit_sha):
        if relative in _PROTECTED_PATHS:
            continue
        if relative.startswith("geography/"):
            continue
        if not relative.endswith((".yaml", ".yml", ".json", ".csv", ".txt")):
            continue
        payload = _try_git(
            repository_root,
            "show",
            f"{commit_sha}:{_CAMPAIGN / relative}",
        )
        if payload is not None:
            result[relative] = payload
    return result


def _candidate_score(files: Mapping[str, bytes]) -> int:
    bindings = files.get("source_bindings.yaml", b"").decode("utf-8", errors="ignore")
    policies = files.get("source_policies.yaml", b"").decode("utf-8", errors="ignore")
    combined = (bindings + "\n" + policies).lower()
    if not all(token in combined for token in _REQUIRED_TOKENS):
        return -1
    if any(token in combined for token in _FORBIDDEN_TEXT):
        return -1
    score = 0
    for token in ("osm_overpass", "overpass", "official_http", "robots", "sitemap"):
        if token in combined:
            score += 40
    score += min(len(files), 20)
    return score


def candidates(repository_root: Path) -> tuple[Candidate, ...]:
    values: list[Candidate] = []
    for commit_sha in _history(repository_root):
        files = _candidate_files(repository_root, commit_sha)
        score = _candidate_score(files)
        if score >= 0:
            values.append(Candidate(commit_sha=commit_sha, files=files, score=score))
    return tuple(sorted(values, key=lambda item: (-item.score, item.commit_sha)))


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk(item)


def _validate_urls(value: object) -> None:
    for item in _walk(value):
        if not isinstance(item, str) or not item.startswith(("http://", "https://")):
            continue
        parsed = urlparse(item)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SourceInputSelectionError(f"source URL must use HTTPS: {item}")
        if any(token in parsed.hostname.lower() for token in _FORBIDDEN_TEXT):
            raise SourceInputSelectionError(f"source URL uses a forbidden host: {item}")


def _bounded_value(key: str, value: object) -> bool:
    lowered = key.lower()
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    if "max_active" in lowered or "max_concurrency" in lowered:
        return 1 <= value <= 4
    if "requests_per_second" in lowered or "rate_per_second" in lowered:
        return 0 < value <= 2
    if "minimum_interval" in lowered and "millisecond" in lowered:
        return 500 <= value <= 120_000
    if "timeout" in lowered and "second" in lowered:
        return 1 <= value <= 120
    return True


def _validate_budgets(value: object) -> int:
    bounded = 0
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not _bounded_value(str(key), item):
                raise SourceInputSelectionError(
                    f"source budget is outside the bounded live-run envelope: {key}={item}"
                )
            lowered = str(key).lower()
            if any(
                token in lowered
                for token in (
                    "max_active",
                    "max_concurrency",
                    "requests_per_second",
                    "rate_per_second",
                    "minimum_interval",
                )
            ):
                bounded += 1
            bounded += _validate_budgets(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        bounded += sum(_validate_budgets(item) for item in value)
    return bounded


def _binding_keys(value: object) -> tuple[str, ...]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "binding_key",
                "source_binding_key",
                "source_key",
                "key",
                "id",
            } and isinstance(item, str):
                text = item.lower()
                if "osm" in text or "http" in text or "website" in text:
                    result.add(item)
            result.update(_binding_keys(item))
        if all(isinstance(key, str) for key in value):
            for key in value:
                lowered = key.lower()
                if "osm" in lowered or "http" in lowered or "website" in lowered:
                    result.add(key)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            result.update(_binding_keys(item))
    return tuple(sorted(result))


def _disable_schedules(value: object) -> object:
    if isinstance(value, list):
        return [_disable_schedules(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result: dict[object, object] = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if lowered in {"schedule_enabled", "scheduled", "auto_start", "automatic"}:
            result[key] = False
        else:
            result[key] = _disable_schedules(item)
    return result


def _enable_bindings(campaign_path: Path, binding_keys: Sequence[str]) -> None:
    value = yaml.safe_load(campaign_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise SourceInputSelectionError("campaign.yaml root must be an object")
    result = dict(value)
    existing = result.get("enabled_source_bindings")
    if existing is None:
        raise SourceInputSelectionError(
            "campaign.yaml has no explicit enabled_source_bindings owner"
        )
    if not isinstance(existing, Sequence) or isinstance(existing, (str, bytes, bytearray)):
        raise SourceInputSelectionError("enabled_source_bindings must be an array")
    result["enabled_source_bindings"] = list(dict.fromkeys((*existing, *binding_keys)))
    campaign_path.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def _write_candidate(campaign_root: Path, candidate: Candidate) -> tuple[str, ...]:
    written: list[str] = []
    for relative, payload in candidate.files.items():
        path = campaign_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith((".yaml", ".yml")):
            parsed = yaml.safe_load(payload)
            parsed = _disable_schedules(parsed)
            _validate_urls(parsed)
            payload = yaml.safe_dump(
                parsed,
                allow_unicode=True,
                sort_keys=False,
                width=100,
            ).encode("utf-8")
        path.write_bytes(payload)
        written.append(relative)
    return tuple(sorted(written))


def _validate_candidate(campaign_root: Path) -> tuple[str, ...]:
    bindings_path = campaign_root / "source_bindings.yaml"
    policies_path = campaign_root / "source_policies.yaml"
    if not bindings_path.is_file() or not policies_path.is_file():
        raise SourceInputSelectionError("candidate lacks source bindings or policies")
    bindings = yaml.safe_load(bindings_path.read_text(encoding="utf-8"))
    policies = yaml.safe_load(policies_path.read_text(encoding="utf-8"))
    _validate_urls(bindings)
    _validate_urls(policies)
    if _validate_budgets(policies) == 0:
        raise SourceInputSelectionError("source policies declare no bounded request controls")
    keys = _binding_keys(bindings)
    lowered = " ".join(keys).lower()
    if "osm" not in lowered or not any(token in lowered for token in ("http", "website")):
        raise SourceInputSelectionError(
            "candidate does not expose distinct OSM and official HTTP binding identities"
        )
    return keys


def _run_validator(repository_root: Path) -> bool:
    result = subprocess.run(
        (
            "uv",
            "run",
            "collector",
            "config",
            "validate",
            "berlin_recording_services",
        ),
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        sys.stderr.buffer.write(result.stdout)
    return result.returncode == 0


def select(repository_root: Path) -> SelectedInputs:
    campaign_root = repository_root / _CAMPAIGN
    if not (campaign_root / "geography/berlin-boundary.geojson").is_file():
        raise SourceInputSelectionError("authoritative Berlin boundary must be materialized first")
    with tempfile.TemporaryDirectory(prefix="berlin-source-inputs-") as temp:
        original = Path(temp) / "original"
        shutil.copytree(campaign_root, original)
        failures: list[str] = []
        for candidate in candidates(repository_root):
            shutil.rmtree(campaign_root)
            shutil.copytree(original, campaign_root)
            try:
                written = _write_candidate(campaign_root, candidate)
                keys = _validate_candidate(campaign_root)
                _enable_bindings(campaign_root / "campaign.yaml", keys)
                if not _run_validator(repository_root):
                    raise SourceInputSelectionError("campaign validator rejected candidate")
            except (OSError, ValueError, SourceInputSelectionError) as exc:
                failures.append(f"{candidate.commit_sha}: {exc}")
                continue
            payloads = [
                relative.encode("utf-8") + b"\0" + (campaign_root / relative).read_bytes()
                for relative in written
            ]
            digest = "sha256:" + hashlib.sha256(b"\n".join(payloads)).hexdigest()
            provenance = {
                "contract": "berlin-source-input-selection",
                "contractRevision": "1",
                "historicalOwnerCommit": candidate.commit_sha,
                "bindingKeys": list(keys),
                "files": list(written),
                "contentDigest": digest,
                "automaticSchedulesEnabled": False,
            }
            provenance_path = campaign_root / "source-inputs.provenance.json"
            provenance_path.write_text(
                json.dumps(
                    provenance,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            return SelectedInputs(
                commit_sha=candidate.commit_sha,
                binding_keys=keys,
                files=(*written, provenance_path.name, "campaign.yaml"),
                content_digest=digest,
            )
        shutil.rmtree(campaign_root)
        shutil.copytree(original, campaign_root)
    raise SourceInputSelectionError(
        "no historical OSM/official-HTTP owner is valid against the current campaign: "
        + "; ".join(failures[:20])
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        selected = select(args.repository_root.resolve())
    except SourceInputSelectionError as exc:
        print(f"Berlin source input selection failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "commitSha": selected.commit_sha,
                "bindingKeys": selected.binding_keys,
                "files": selected.files,
                "contentDigest": selected.content_digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
