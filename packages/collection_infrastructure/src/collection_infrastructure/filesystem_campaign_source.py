from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Never

from collection_application import RawCampaignBundle
from collection_contracts import owner_error

_CAMPAIGN_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_ROOT_FILES = frozenset(
    {
        "attributes.yaml",
        "campaign.yaml",
        "entity_kinds.yaml",
        "geography.yaml",
        "source_bindings.yaml",
        "taxonomy.yaml",
    }
)
_MAX_YAML_BYTES = 1_048_576
_MAX_SEED_BYTES = 10_485_760
_MAX_GEOGRAPHY_BYTES = 16 * 1024 * 1024
_MAX_PROVENANCE_BYTES = 262_144


class FilesystemCampaignBundleSource:
    def __init__(self, campaigns_root: Path) -> None:
        root = campaigns_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"campaigns root is not a directory: {root}")
        self._root = root

    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
        if not _CAMPAIGN_KEY_PATTERN.fullmatch(campaign_key):
            raise owner_error(
                error_type="collection/campaign-key-invalid",
                owner="CampaignConfiguration",
                code="CAMPAIGN_KEY_INVALID",
                message="Campaign key does not satisfy the repository path contract.",
                context={"campaignKey": campaign_key},
                required_action="Use a lower-snake-case campaign key from the configured root.",
                correlation_id=correlation_id,
            )

        candidate = self._root / campaign_key
        if candidate.is_symlink():
            self._raise_boundary_error(
                campaign_key,
                campaign_key,
                "campaign_directory_is_symlink",
                correlation_id,
            )
        try:
            campaign_dir = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise owner_error(
                error_type="collection/campaign-not-found",
                owner="CampaignConfiguration",
                code="CAMPAIGN_NOT_FOUND",
                message="Requested campaign directory does not exist.",
                context={"campaignKey": campaign_key},
                required_action="Create the versioned campaign directory or use an existing key.",
                correlation_id=correlation_id,
            ) from exc
        if campaign_dir.parent != self._root or not campaign_dir.is_dir():
            self._raise_boundary_error(
                campaign_key,
                str(candidate),
                "campaign_path_escaped_root",
                correlation_id,
            )

        files: dict[str, bytes] = {}
        for entry in sorted(campaign_dir.rglob("*")):
            relative = entry.relative_to(campaign_dir).as_posix()
            if entry.is_symlink():
                self._raise_boundary_error(
                    campaign_key,
                    relative,
                    "symlink_not_allowed",
                    correlation_id,
                )
            if entry.is_dir():
                continue
            if not entry.is_file():
                self._raise_boundary_error(
                    campaign_key,
                    relative,
                    "non_regular_file",
                    correlation_id,
                )
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(campaign_dir):
                self._raise_boundary_error(
                    campaign_key,
                    relative,
                    "file_path_escaped_campaign",
                    correlation_id,
                )
            max_bytes = self._validate_allowed_path(campaign_key, relative, correlation_id)
            size = entry.stat().st_size
            if size > max_bytes:
                raise owner_error(
                    error_type="collection/campaign-file-too-large",
                    owner="CampaignConfiguration",
                    code="CAMPAIGN_FILE_TOO_LARGE",
                    message="Campaign file exceeds its repository safety limit.",
                    context={"campaignKey": campaign_key, "path": relative, "sizeBytes": size},
                    required_action=(
                        "Reduce the file or define a reviewed owner-specific import path."
                    ),
                    correlation_id=correlation_id,
                )
            files[relative] = entry.read_bytes()

        return RawCampaignBundle(campaign_key=campaign_key, files=MappingProxyType(files))

    @staticmethod
    def _validate_allowed_path(campaign_key: str, relative: str, correlation_id: str) -> int:
        if relative in _ROOT_FILES:
            return _MAX_YAML_BYTES
        if (
            relative.startswith("source_policies/")
            and relative.count("/") == 1
            and relative.endswith(".yaml")
        ):
            return _MAX_YAML_BYTES
        if relative == "discovery/manual_seeds.csv":
            return _MAX_SEED_BYTES
        if (
            relative.startswith("geography/")
            and relative.count("/") == 1
            and relative.endswith(".geojson")
        ):
            return _MAX_GEOGRAPHY_BYTES
        if (
            relative.startswith("geography/")
            and relative.count("/") == 1
            and relative.endswith(".provenance.json")
        ):
            return _MAX_PROVENANCE_BYTES
        raise owner_error(
            error_type="collection/campaign-file-unexpected",
            owner="CampaignConfiguration",
            code="CAMPAIGN_FILE_UNEXPECTED",
            message="Campaign bundle contains a file outside the current allowlist.",
            context={"campaignKey": campaign_key, "path": relative},
            required_action=(
                "Remove the file or add a typed owner and allowlist rule in the same change."
            ),
            correlation_id=correlation_id,
        )

    @staticmethod
    def _raise_boundary_error(
        campaign_key: str,
        path: str,
        reason: str,
        correlation_id: str,
    ) -> Never:
        raise owner_error(
            error_type="collection/campaign-path-boundary-violation",
            owner="CampaignConfiguration",
            code="CAMPAIGN_PATH_BOUNDARY_VIOLATION",
            message="Campaign filesystem boundary validation failed.",
            context={"campaignKey": campaign_key, "path": path, "reason": reason},
            required_action=(
                "Keep regular campaign files inside the allowlisted campaign directory."
            ),
            correlation_id=correlation_id,
        )
