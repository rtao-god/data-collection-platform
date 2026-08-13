# OSM query planning

OSM discovery query planning is a pure deterministic boundary. It accepts only an approved polygon, sorted OSM element types, sorted exact tag filters, and explicit timeout/element limits.

The planner:

- partitions tag filters into bounded work-sized chunks without dropping or duplicating filters;
- emits versioned `osm-overpass-query/1` JSON artifacts accepted by the OSM worker;
- assigns contiguous positions;
- records both query-spec SHA-256 and generated Overpass QL SHA-256;
- emits a deterministic plan artifact and plan digest;
- changes identity when the polygon, filters, element types, timeout, or element limit changes.

The generated query artifact contains no endpoint URL, Worker Gateway identity, campaign name, city name, or domain-specific taxonomy. Endpoint selection remains operator configuration, while campaign-specific tag mappings and the approved boundary remain configuration-owned inputs.
