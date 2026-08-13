# OSM Overpass worker

The OSM worker is a capability-isolated deployable for `osm_query` work. It has no PostgreSQL credentials, no S3 credentials, and no browser or crawler stack.

For each lease it:

1. reads exactly one scoped `overpass_query_spec` artifact through Worker Gateway;
2. validates the versioned query contract and approved polygon supplied by configuration;
3. performs one hardened Overpass request against the operator allowlist;
4. keeps the lease heartbeat active across read, acquisition, parsing, upload, and completion;
5. uploads the exact Overpass JSON response as `raw_artifact` with role `overpass_raw_response`;
6. uploads the deterministic observation batch as `derived_artifact` with role `osm_observations`;
7. completes only the output contract `osm-overpass-result/1`.

The observation batch preserves OSM element type and ID, coordinates, structured address tags, source URL, raw-response digest, query digest, base timestamp when supplied by Overpass, and canonical OpenStreetMap attribution.

Source pacing, permits, retries, and lease ownership remain Work Engine responsibilities. No Nominatim fallback exists. The worker does not contain a hard-coded Berlin polygon or recording-studio taxonomy; the Berlin campaign remains blocked until an approved geography artifact is provided.
