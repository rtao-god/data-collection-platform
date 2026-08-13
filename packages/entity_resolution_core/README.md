# Entity resolution core

`entity_resolution_core` owns deterministic blocking, explicit pair features and dispositions, separation-constrained clustering, and immutable cluster lineage.

A normalized name may create a candidate pair, but name-only evidence cannot produce `auto_match`. Fuzzy pairs whose candidates are classified `inside` or `boundary` for the exact Berlin market-area identity require review. Explicit `separate` decisions prevent both direct and transitive joins. Cluster IDs derive only from sorted member candidate IDs, so reversing a split decision in a new immutable batch restores the original membership identity without mutating prior snapshots.

The core consumes geography classifications produced by the existing campaign-geography owner. It does not geocode, calculate polygons, access persistence, own review decisions, or decide export eligibility; `quality_core` owns the fail-closed quality verdict.
