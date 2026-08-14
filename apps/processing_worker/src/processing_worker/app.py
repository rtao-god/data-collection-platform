from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from processing_worker.contracts import ProcessingWorkerSettings
from processing_worker.gateway import SdkProcessingGateway
from processing_worker.worker import ProcessingWorker
from source_connector_sdk import SourceWorkerGateway, WorkerGatewayFailure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="processing-worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Acquire at most one capability-scoped work unit and then exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        settings = ProcessingWorkerSettings.from_environment()
        with SourceWorkerGateway(
            base_url=settings.gateway_url,
            token=settings.gateway_token,
            timeout_seconds=settings.gateway_timeout_seconds,
        ) as client:
            worker = ProcessingWorker(SdkProcessingGateway(client), settings)
            worker.register()
            if arguments.once:
                result = worker.run_once()
                print(
                    json.dumps(
                        {
                            "acquired": result.acquired,
                            "workId": result.work_id,
                            "outputDigest": result.output_digest,
                            "capability": settings.capability,
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
                    "code": "PROCESSING_WORKER_FAILED",
                    "message": str(exc),
                    "causeType": type(exc).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    return 0
