# Manual import plan admission

`ManualImportPlanAdmission` is the privileged control-plane boundary between a verified manual-import plan artifact and durable row-level work.

## Ownership

- `manual_import_worker` reads one scoped source artifact, produces one immutable plan artifact, and completes its parent work through Worker Gateway.
- `collection_application.manual_import_admission` validates the plan contract and derives deterministic child identities and canonical child input payloads.
- `collection_infrastructure.postgres.manual_import_admission` owns the single PostgreSQL transaction that seals the admission record, enqueues children, and binds their exact input artifacts.
- Connector workers never receive PostgreSQL credentials and cannot create queue rows directly.

## Admission invariants

- The parent work belongs to the requested run and has capability `manual_import`.
- The plan and source artifacts exist, have the expected digests, and have the required lineage to the parent work.
- A blocked plan creates no child work.
- A ready plan creates exactly one child work unit per accepted record.
- Child identities include the parent, plan digest, source position, and record digest, so equal records at different positions remain distinct observations.
- The admission row, child work units, and ordered artifact bindings commit atomically.
- Exact replay returns the existing child identities; any replay with different immutable inputs fails as a conflict.
- No read path repairs or mutates admission state.
