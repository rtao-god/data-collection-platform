from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from collection_application.ports import RawCampaignBundle
from collection_contracts import (
    AttributesDocument,
    CampaignDocument,
    EntityKindsDocument,
    SourceBindingsDocument,
    SourcePolicy,
    TaxonomyDocument,
    owner_error,
)

CAMPAIGN_DOCUMENT = "campaign.yaml"
ENTITY_KINDS_DOCUMENT = "entity_kinds.yaml"
TAXONOMY_DOCUMENT = "taxonomy.yaml"
ATTRIBUTES_DOCUMENT = "attributes.yaml"
SOURCE_BINDINGS_DOCUMENT = "source_bindings.yaml"
REQUIRED_DOCUMENTS = frozenset(
    {
        CAMPAIGN_DOCUMENT,
        ENTITY_KINDS_DOCUMENT,
        TAXONOMY_DOCUMENT,
        ATTRIBUTES_DOCUMENT,
        SOURCE_BINDINGS_DOCUMENT,
    }
)
ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ParsedCampaignDocuments:
    campaign: CampaignDocument
    entity_kinds: EntityKindsDocument
    taxonomy: TaxonomyDocument
    attributes: AttributesDocument
    source_bindings: SourceBindingsDocument
    source_policies: dict[str, SourcePolicy]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def parse_campaign_documents(
    bundle: RawCampaignBundle,
    correlation_id: str,
) -> ParsedCampaignDocuments:
    _require_documents(bundle, correlation_id)
    return ParsedCampaignDocuments(
        campaign=_load_model(bundle, CAMPAIGN_DOCUMENT, CampaignDocument, correlation_id),
        entity_kinds=_load_model(
            bundle,
            ENTITY_KINDS_DOCUMENT,
            EntityKindsDocument,
            correlation_id,
        ),
        taxonomy=_load_model(bundle, TAXONOMY_DOCUMENT, TaxonomyDocument, correlation_id),
        attributes=_load_model(bundle, ATTRIBUTES_DOCUMENT, AttributesDocument, correlation_id),
        source_bindings=_load_model(
            bundle,
            SOURCE_BINDINGS_DOCUMENT,
            SourceBindingsDocument,
            correlation_id,
        ),
        source_policies=_load_source_policies(bundle, correlation_id),
    )


def _require_documents(bundle: RawCampaignBundle, correlation_id: str) -> None:
    missing = sorted(REQUIRED_DOCUMENTS.difference(bundle.files))
    if not missing:
        return
    raise owner_error(
        error_type="collection/campaign-bundle-incomplete",
        owner="CampaignConfiguration",
        code="CAMPAIGN_BUNDLE_INCOMPLETE",
        message="Campaign bundle is missing required documents.",
        context={"campaignKey": bundle.campaign_key, "missingDocuments": missing},
        required_action="Add the required campaign documents and validate the bundle again.",
        correlation_id=correlation_id,
    )


def _load_model(
    bundle: RawCampaignBundle,
    path: str,
    model_type: type[ModelT],
    correlation_id: str,
) -> ModelT:
    payload = _load_yaml(bundle.files[path], path, correlation_id)
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise owner_error(
            error_type="collection/campaign-contract-invalid",
            owner="CampaignConfiguration",
            code="CAMPAIGN_CONTRACT_INVALID",
            message="Campaign document does not satisfy its owner contract.",
            context={
                "campaignKey": bundle.campaign_key,
                "document": path,
                "errors": exc.errors(include_input=False, include_url=False),
            },
            required_action="Correct the named document and validate the campaign again.",
            correlation_id=correlation_id,
        ) from exc


def _load_source_policies(
    bundle: RawCampaignBundle,
    correlation_id: str,
) -> dict[str, SourcePolicy]:
    paths = sorted(
        path
        for path in bundle.files
        if path.startswith("source_policies/") and path.endswith(".yaml")
    )
    if not paths:
        raise owner_error(
            error_type="collection/source-policy-missing",
            owner="SourcePolicy",
            code="SOURCE_POLICY_MISSING",
            message="Campaign bundle has no source policy documents.",
            context={"campaignKey": bundle.campaign_key},
            required_action="Add an explicit source policy for every enabled source binding.",
            correlation_id=correlation_id,
        )

    policies: dict[str, SourcePolicy] = {}
    for path in paths:
        policy = _load_model(bundle, path, SourcePolicy, correlation_id)
        expected_path = f"source_policies/{policy.policy_key}.yaml"
        if path != expected_path:
            raise owner_error(
                error_type="collection/source-policy-path-invalid",
                owner="SourcePolicy",
                code="SOURCE_POLICY_PATH_INVALID",
                message="Source policy file path does not match its owned policy key.",
                context={
                    "campaignKey": bundle.campaign_key,
                    "actualPath": path,
                    "expectedPath": expected_path,
                    "policyKey": policy.policy_key,
                },
                required_action="Rename the source policy file to the exact policy-key path.",
                correlation_id=correlation_id,
            )
        if policy.policy_key in policies:
            raise owner_error(
                error_type="collection/source-policy-duplicate",
                owner="SourcePolicy",
                code="SOURCE_POLICY_DUPLICATE",
                message="Campaign bundle defines a source policy key more than once.",
                context={"campaignKey": bundle.campaign_key, "policyKey": policy.policy_key},
                required_action="Keep one policy document for each policy key in the bundle.",
                correlation_id=correlation_id,
            )
        policies[policy.policy_key] = policy
    return policies


def _load_yaml(raw: bytes, path: str, correlation_id: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise owner_error(
            error_type="collection/campaign-encoding-invalid",
            owner="CampaignConfiguration",
            code="CAMPAIGN_ENCODING_INVALID",
            message="Campaign YAML document is not valid UTF-8.",
            context={"document": path, "byteOffset": exc.start},
            required_action="Encode the document as UTF-8 and validate it again.",
            correlation_id=correlation_id,
        ) from exc
    try:
        payload = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise owner_error(
            error_type="collection/campaign-yaml-invalid",
            owner="CampaignConfiguration",
            code="CAMPAIGN_YAML_INVALID",
            message="Campaign YAML document cannot be parsed safely.",
            context={"document": path, "detail": str(exc)},
            required_action="Correct the YAML syntax or duplicate key and validate it again.",
            correlation_id=correlation_id,
        ) from exc
    if payload is None:
        raise owner_error(
            error_type="collection/campaign-document-empty",
            owner="CampaignConfiguration",
            code="CAMPAIGN_DOCUMENT_EMPTY",
            message="Campaign YAML document is empty.",
            context={"document": path},
            required_action="Provide the complete typed document and validate it again.",
            correlation_id=correlation_id,
        )
    return payload
