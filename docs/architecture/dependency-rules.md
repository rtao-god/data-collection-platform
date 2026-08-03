# Dependency rules

Allowed internal imports:

```text
collector_cli
  -> collection_application
  -> collection_infrastructure
  -> collection_contracts

collection_infrastructure
  -> collection_application ports
  -> collection_contracts

collection_application
  -> collection_domain
  -> collection_contracts

collection_domain
  -> Python standard library only

collection_contracts
  -> Pydantic and Python standard library
```

Forbidden examples:

- domain importing FastAPI, SQLAlchemy, Scrapy, Playwright, Dagster, infrastructure, or apps;
- connector or worker code importing SQLAlchemy models;
- infrastructure importing an application composition root;
- generic production packages named `utils`, `common`, `helpers`, or `shared_domain`;
- a concrete app imported by another owner package.

`tools/architecture_checks/check_dependencies.py` enforces the current graph in CI.
