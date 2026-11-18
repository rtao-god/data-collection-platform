# PostgreSQL/PostGIS integration database

`deploy/compose/integration.yaml` starts a throwaway PostGIS database that is completely
separate from `deploy/compose/infrastructure.yaml`: its own Compose project name, its own
container, its own host port, and tmpfs storage instead of a named volume. It cannot read
or modify development data.

```
pwsh -NoProfile -File tools/integration-db.ps1 up       # start + migrate to head
pwsh -NoProfile -File tools/integration-db.ps1 test     # run pytest -m integration database/tests
pwsh -NoProfile -File tools/integration-db.ps1 down     # remove container and tmpfs data
```

`up` prints the `COLLECTOR_DATABASE_URL` it exported. The database lives in tmpfs, so
`down` (or a container restart) discards everything; each `up` starts from migrations.

The default host port is 55432. Override it with `-PostgresPort` when that port is busy.
