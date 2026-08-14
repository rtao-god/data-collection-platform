from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def main() -> int:
    path = ROOT / "apps/control_api/tests/test_app.py"
    text = path.read_text(encoding="utf-8")
    if "import json\n" not in text:
        text = text.replace(
            "from __future__ import annotations\n\n",
            "from __future__ import annotations\n\nimport json\n",
            1,
        )
    start = text.index("def client(service: Service) -> TestClient:\n")
    end = text.index("\n\ndef test_", start)
    replacement = dedent(
        '''\
        def client(service: Service) -> TestClient:
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
            return TestClient(
                create_app(
                    service=service,
                    authenticator=auth,
                    readiness_probe=lambda: True,
                )
            )
        '''
    ).rstrip()
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
