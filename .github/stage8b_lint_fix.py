from __future__ import annotations

from pathlib import Path
from textwrap import dedent, indent

ROOT = Path.cwd()


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one source fragment")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _method(source: str) -> str:
    return indent(dedent(source).strip(), "    ") + "\n\n"


def _patch_temporary_tooling() -> None:
    path = ROOT / ".github/stage8b_materializer.py"
    _replace_once(
        path,
        "    policy = subprocess.check_output(\n",
        "    policy = subprocess.check_output(  # noqa: S603\n",
    )


def _patch_control_api_runtime() -> None:
    main_path = ROOT / "apps/control_api/src/control_api/main.py"
    _replace_once(
        main_path,
        '        host="0.0.0.0",\n',
        '        # Container ingress is constrained by the deployment network.\n'
        '        host="0.0.0.0",  # noqa: S104\n',
    )

    test_path = ROOT / "apps/control_api/tests/test_app.py"
    text = test_path.read_text(encoding="utf-8")
    if "import json\n" not in text:
        text = text.replace(
            "from __future__ import annotations\n\n",
            "from __future__ import annotations\n\nimport json\n",
            1,
        )
    client_start = text.index("def client(service: Service) -> TestClient:\n")
    auth_start = text.index("    auth = TokenAuthenticator.from_json(\n", client_start)
    auth_end = text.index("    return TestClient(\n", auth_start)
    auth_block = dedent(
        '''\
            auth = TokenAuthenticator.from_json(
                json.dumps(
                    {
                        TOKEN: {
                            "actorId": "reviewer-1",
                            "permissions": [
                                "review:read",
                                "review:decide",
                                "review:observe",
                                "review:suppress",
                            ],
                        }
                    }
                )
            )
        '''
    )
    text = text[:auth_start] + auth_block + text[auth_end:]
    test_path.write_text(text, encoding="utf-8")


def _patch_postgres_exports() -> None:
    path = ROOT / (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/__init__.py"
    )
    text = path.read_text(encoding="utf-8")
    if '    "PostgresReviewRepository",\n' not in text:
        start = text.index("__all__ = [\n")
        end = text.index("]\n", start)
        text = (
            text[:end]
            + '    "PostgresReviewRepository",\n'
            + text[end:]
        )
    path.write_text(text, encoding="utf-8")


def _patch_review_repository_sql() -> None:
    path = ROOT / (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/review_repository.py"
    )
    text = path.read_text(encoding="utf-8")

    load_case_start = text.index("    def _load_case(\n")
    load_case_end = text.index("    def _load_candidate(\n", load_case_start)
    load_case = _method(
        '''
        def _load_case(
            self,
            connection: sa.Connection,
            case_id: UUID,
            *,
            for_update: bool,
        ) -> ReviewCase:
            if for_update:
                statement = sa.text(
                    """
                    SELECT
                        c.case_id,
                        c.candidate_id,
                        c.candidate_revision,
                        c.opened_at_utc,
                        r.revision,
                        r.state,
                        r.reason_codes,
                        r.current_decision_id,
                        r.recorded_at_utc,
                        r.correlation_id
                    FROM review.review_cases AS c
                    JOIN review.review_case_revisions AS r
                      ON r.case_id = c.case_id
                    WHERE c.case_id = :case_id
                    ORDER BY r.revision DESC
                    LIMIT 1
                    FOR UPDATE OF r
                    """
                )
            else:
                statement = sa.text(
                    """
                    SELECT
                        c.case_id,
                        c.candidate_id,
                        c.candidate_revision,
                        c.opened_at_utc,
                        r.revision,
                        r.state,
                        r.reason_codes,
                        r.current_decision_id,
                        r.recorded_at_utc,
                        r.correlation_id
                    FROM review.review_cases AS c
                    JOIN review.review_case_revisions AS r
                      ON r.case_id = c.case_id
                    WHERE c.case_id = :case_id
                    ORDER BY r.revision DESC
                    LIMIT 1
                    """
                )
            row = connection.execute(
                statement,
                {"case_id": case_id},
            ).mappings().one_or_none()
            if row is None:
                raise ReviewNotFound(
                    f"Review case {case_id} does not exist.",
                    "Refresh the review queue and select an existing case.",
                )
            return _case(row)
        '''
    )
    text = text[:load_case_start] + load_case + text[load_case_end:]

    require_start = text.index("    def _require_candidate_revision(\n")
    require_end = text.index("    def _load_suppression(\n", require_start)
    require_candidate = _method(
        '''
        def _require_candidate_revision(
            self,
            connection: sa.Connection,
            candidate_id: UUID,
            revision: int,
            *,
            for_update: bool,
        ) -> None:
            if for_update:
                statement = sa.text(
                    """
                    SELECT candidate_id
                    FROM candidates.candidate_revisions
                    WHERE candidate_id = :candidate_id AND revision = :revision
                    FOR SHARE
                    """
                )
            else:
                statement = sa.text(
                    """
                    SELECT candidate_id
                    FROM candidates.candidate_revisions
                    WHERE candidate_id = :candidate_id AND revision = :revision
                    """
                )
            exists = connection.execute(
                statement,
                {"candidate_id": candidate_id, "revision": revision},
            ).scalar_one_or_none()
            if exists is None:
                raise ReviewNotFound(
                    f"Candidate revision {candidate_id}/{revision} does not exist.",
                    "Reload candidate state before adding manual evidence.",
                )
        '''
    )
    text = text[:require_start] + require_candidate + text[require_end:]

    suppression_start = text.index("    def _load_suppression(\n")
    suppression_end = text.index("    def _decision_replay(\n", suppression_start)
    load_suppression = _method(
        '''
        def _load_suppression(
            self,
            connection: sa.Connection,
            suppression_id: UUID,
            *,
            for_update: bool,
        ) -> SuppressionRevision:
            if for_update:
                statement = sa.text(
                    """
                    SELECT *
                    FROM review.suppression_revisions
                    WHERE suppression_id = :suppression_id
                    ORDER BY revision DESC
                    LIMIT 1
                    FOR UPDATE
                    """
                )
            else:
                statement = sa.text(
                    """
                    SELECT *
                    FROM review.suppression_revisions
                    WHERE suppression_id = :suppression_id
                    ORDER BY revision DESC
                    LIMIT 1
                    """
                )
            row = connection.execute(
                statement,
                {"suppression_id": suppression_id},
            ).mappings().one_or_none()
            if row is None:
                raise ReviewNotFound(
                    f"Suppression {suppression_id} does not exist.",
                    "Refresh suppression state and select an existing suppression.",
                )
            return _suppression(row)
        '''
    )
    text = text[:suppression_start] + load_suppression + text[suppression_end:]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    _patch_temporary_tooling()
    _patch_control_api_runtime()
    _patch_postgres_exports()
    _patch_review_repository_sql()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
