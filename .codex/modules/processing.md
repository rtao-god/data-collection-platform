# Processing owner

`collection_contracts.observations` owns extraction requests, extracted records, normalization
profiles, field observations, observation batches, evidence locators, explicit states, and canonical
digests.

`extraction_core` owns deterministic JSON-LD/microdata/RDFa and bounded HTML evidence extraction
from one exact raw artifact. `normalization_core` owns typed value normalization and explicit
missing/prohibited/invalid outcomes. Neither package owns candidates, review, quality, or export.

`processing-worker` runs exactly one configured capability (`extraction` or `normalization`) and
uses only `SourceWorkerGateway`. Inputs are scoped artifact roles; output is one verified
`derived_artifact`. The worker must not receive PostgreSQL or S3 account credentials.

Owner checks:

```text
uv run pytest packages/collection_contracts/tests/test_observations.py \
  packages/extraction_core/tests \
  packages/normalization_core/tests \
  apps/processing_worker/tests
uv run mypy
uv run python tools/architecture_checks/check_dependencies.py
docker build --file deploy/docker/processing-worker.Dockerfile .
```
