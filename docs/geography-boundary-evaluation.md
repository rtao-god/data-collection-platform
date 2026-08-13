# Geography boundary evaluation

Geography boundaries remain immutable campaign-owned artifacts. The evaluation layer consumes the exact canonical GeoJSON and its SHA-256 identity; it does not create a second mutable boundary registry.

Supported boundary geometry is limited to WGS84 `Polygon` and `MultiPolygon`. Input decoding rejects invalid UTF-8 or JSON, duplicate keys, non-finite coordinates, open or degenerate rings, out-of-range coordinates, excessive size, and excessive position count.

PostGIS evaluation uses:

- `ST_Covers(boundary, point)` for inclusion, so an edge or vertex is included;
- `ST_Touches(boundary, point)` to distinguish the included `boundary` result from `inside`;
- `ST_IsValid`, `ST_IsEmpty`, SRID, and geometry-type checks before evaluation.

The adapter does not call `ST_MakeValid` and does not silently repair source evidence. Results preserve input point order and the exact boundary digest.

Synthetic polygons exist only in tests. No Berlin geometry is checked into production code; the Berlin campaign remains blocked until an approved source artifact is supplied.
