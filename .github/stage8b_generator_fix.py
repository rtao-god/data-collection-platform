from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def main() -> int:
    path = ROOT / "tools/control_api_contract_generation/generate.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            '''\
            from __future__ import annotations

            import argparse
            import json
            from hashlib import sha256
            from pathlib import Path
            from typing import cast

            from control_api.app import create_app
            from control_api.auth import TokenAuthenticator
            from review_application import ReviewService

            ROOT = Path(__file__).resolve().parents[2]
            OUTPUT = ROOT / "contracts/control_api"


            class _Service:
                pass


            def render() -> dict[str, str]:
                authenticator = TokenAuthenticator.from_json(
                    '{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":'
                    '{"actorId":"contract-generator",'
                    '"permissions":["review:read"]}}'
                )
                app = create_app(
                    service=cast(ReviewService, _Service()),
                    authenticator=authenticator,
                    readiness_probe=lambda: True,
                )
                schema = app.openapi()
                openapi = (
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\\n"
                )
                operations: list[dict[str, str]] = []
                for path, path_item in sorted(schema["paths"].items()):
                    for method, operation in sorted(path_item.items()):
                        if method.lower() not in {
                            "get",
                            "post",
                            "put",
                            "patch",
                            "delete",
                        }:
                            continue
                        operation_id = operation.get("operationId")
                        if not isinstance(operation_id, str) or not operation_id:
                            raise RuntimeError(
                                f"Control API operation {method.upper()} {path} "
                                "does not have a stable operationId"
                            )
                        operations.append(
                            {
                                "method": method.upper(),
                                "operationId": operation_id,
                                "path": path,
                            }
                        )
                inventory = (
                    json.dumps(
                        {
                            "contract": (
                                "collector-control-api-operation-inventory"
                            ),
                            "contractRevision": (
                                "control-api-operation-inventory-v1"
                            ),
                            "operations": operations,
                            "openapiDigest": (
                                "sha256:"
                                + sha256(openapi.encode("utf-8")).hexdigest()
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\\n"
                )
                return {
                    "openapi.json": openapi,
                    "operation-inventory.json": inventory,
                }


            def main() -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("--check", action="store_true")
                args = parser.parse_args()
                expected = render()
                if args.check:
                    drift = [
                        name
                        for name, content in expected.items()
                        if not (OUTPUT / name).exists()
                        or (OUTPUT / name).read_text(encoding="utf-8")
                        != content
                    ]
                    if drift:
                        raise SystemExit(
                            "Control API contract drift: " + ", ".join(drift)
                        )
                    return 0
                OUTPUT.mkdir(parents=True, exist_ok=True)
                for name, content in expected.items():
                    (OUTPUT / name).write_text(content, encoding="utf-8")
                return 0


            if __name__ == "__main__":
                raise SystemExit(main())
            '''
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
