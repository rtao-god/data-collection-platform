from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from manual_import_worker.contracts import ManualImportWorkerSettings
from manual_import_worker.gateway import SourceWorkerGatewayAdapter
from manual_import_worker.worker import ManualImportWorker
from source_connector_sdk import SourceWorkerGateway, WorkerGatewayFailure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manual-import-worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Acquire at most one work unit and then exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        settings = ManualImportWorkerSettings.from_environment()
        with SourceWorkerGateway(
            base_url=settings.gateway_url,
            token=settings.gateway_token,
            timeout_seconds=settings.transfer_timeout_seconds,
        ) as client:
            worker = ManualImportWorker(SourceWorkerGatewayAdapter(client), settings)
            worker.register()
            if arguments.once:
                result = worker.run_once()
                print(
                    json.dumps(
                        {
                            "acquired": result.acquired,
                            "workId": result.work_id,
                            "planDigest": result.plan_digest,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                return 0
            worker.run_forever()
    except WorkerGatewayFailure as exc:
        print(exc.envelope.model_dump_json(by_alias=True))
        return 2
    except (RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "code": "MANUAL_IMPORT_WORKER_FAILED",
                    "message": str(exc),
                    "causeType": type(exc).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    return 0
