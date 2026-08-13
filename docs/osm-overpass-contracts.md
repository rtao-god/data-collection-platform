# OSM Overpass contracts

The OSM connector is split into deterministic core contracts and a later network worker.

`connectors/osm_overpass` owns:

- an allowlisted `OverpassQuerySpec` built from approved polygon points, OSM element types, and exact tag filters;
- deterministic Overpass QL generation with escaped values and bounded query size;
- strict UTF-8 and JSON parsing with duplicate-key and non-finite-number rejection;
- OSM identity as `(element type, OSM ID)`;
- WGS84 coordinates from node coordinates or way/relation centers;
- structured address tags, canonical source URL, and mandatory OpenStreetMap attribution;
- deterministic observation serialization and SHA-256 identities.

The core package performs no HTTP, Worker Gateway, object-store, or PostgreSQL I/O. It does not contain a hard-coded Berlin taxonomy or boundary. An approved geography artifact must supply the polygon; until that artifact exists, the Berlin campaign remains blocked.
