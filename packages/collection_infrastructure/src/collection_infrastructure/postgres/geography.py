from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from collection_application.geography import (
    GeographyBoundaryArtifact,
    GeographyCoverage,
    GeographyCoverageKind,
    GeographyEvaluationError,
    GeographyPoint,
)

_COVERAGE_QUERY = sa.text(
    """
    WITH raw_boundary AS (
        SELECT ST_SetSRID(
            ST_GeomFromGeoJSON(CAST(:boundary_geojson AS text)),
            4326
        ) AS geom
    ),
    boundary AS (
        SELECT ST_Multi(geom) AS geom
        FROM raw_boundary
        WHERE geom IS NOT NULL
          AND NOT ST_IsEmpty(geom)
          AND ST_IsValid(geom)
          AND ST_SRID(geom) = 4326
          AND ST_GeometryType(geom) IN ('ST_Polygon', 'ST_MultiPolygon')
    ),
    input_points AS (
        SELECT
            item.position,
            item.point_key,
            ST_SetSRID(
                ST_MakePoint(item.longitude, item.latitude),
                4326
            ) AS geom
        FROM jsonb_to_recordset(CAST(:points_json AS jsonb)) AS item(
            position integer,
            point_key text,
            latitude double precision,
            longitude double precision
        )
    )
    SELECT
        input_points.position,
        input_points.point_key,
        CASE
            WHEN ST_Covers(boundary.geom, input_points.geom) THEN
                CASE
                    WHEN ST_Touches(boundary.geom, input_points.geom)
                        THEN 'boundary'
                    ELSE 'inside'
                END
            ELSE 'outside'
        END AS coverage
    FROM boundary
    CROSS JOIN input_points
    ORDER BY input_points.position
    """
)


class PostgresGeographyCoverage:
    """PostGIS adapter for exact immutable boundary artifacts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def evaluate(
        self,
        boundary: GeographyBoundaryArtifact,
        points: Sequence[GeographyPoint],
    ) -> Sequence[GeographyCoverage]:
        point_tuple = tuple(points)
        if not point_tuple:
            return ()
        points_json = json.dumps(
            [
                {
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "point_key": point.point_key,
                    "position": position,
                }
                for position, point in enumerate(point_tuple)
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        _COVERAGE_QUERY,
                        {
                            "boundary_geojson": boundary.canonical_geojson.decode("utf-8"),
                            "points_json": points_json,
                        },
                    )
                    .mappings()
                    .all()
                )
        except DBAPIError as exc:
            raise GeographyEvaluationError(
                code="GEOGRAPHY_POSTGIS_EVALUATION_FAILED",
                message="PostGIS could not evaluate the geography boundary.",
            ) from exc
        if len(rows) != len(point_tuple):
            raise GeographyEvaluationError(
                code="GEOGRAPHY_BOUNDARY_TOPOLOGY_INVALID",
                message=(
                    "The geography boundary is empty, invalid, or not a polygonal WGS84 geometry."
                ),
            )
        results: list[GeographyCoverage] = []
        for expected_position, row in enumerate(rows):
            position = row["position"]
            point_key = row["point_key"]
            coverage = row["coverage"]
            if (
                position != expected_position
                or point_key != point_tuple[expected_position].point_key
            ):
                raise GeographyEvaluationError(
                    code="GEOGRAPHY_POSTGIS_RESULT_IDENTITY_INVALID",
                    message="PostGIS returned a geography result with invalid identity.",
                )
            if coverage not in {"inside", "boundary", "outside"}:
                raise GeographyEvaluationError(
                    code="GEOGRAPHY_POSTGIS_RESULT_VALUE_INVALID",
                    message="PostGIS returned an unsupported geography coverage value.",
                )
            results.append(
                GeographyCoverage(
                    point_key=cast(str, point_key),
                    boundary_digest=boundary.geometry_digest,
                    coverage=cast(GeographyCoverageKind, coverage),
                )
            )
        return tuple(results)
