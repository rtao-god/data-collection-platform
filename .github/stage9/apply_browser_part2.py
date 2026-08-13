from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip(), encoding="utf-8")


def transformed(path: str, replacements: tuple[tuple[str, str], ...]) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    return text


write(
    "connectors/playwright_browser/src/playwright_browser/executor.py",
    r'''
    from __future__ import annotations

    from collections.abc import Callable
    from typing import cast

    from browser_contracts import (
        BlockedRequestDiagnostic,
        BrowserAcquisitionRequest,
        BrowserRuntimeResult,
        canonical_http_url,
    )
    from browser_security import (
        BrowserDnsUnavailable,
        BrowserPolicyBlocked,
        BrowserRequestGuard,
        BrowserSecurityError,
        NetworkResolver,
        SystemNetworkResolver,
        canonical_blocked_diagnostics,
        validate_launch_args,
    )
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Download,
        Error as PlaywrightError,
        Page,
        Playwright,
        Request,
        Route,
        TimeoutError as PlaywrightTimeoutError,
        WebSocketRoute,
        sync_playwright,
    )


    class BrowserRuntimeTransientError(RuntimeError):
        code = "BROWSER_RUNTIME_TRANSIENT"


    class RenderedDocumentTooLarge(RuntimeError):
        code = "BROWSER_RENDERED_DOCUMENT_TOO_LARGE"


    class PlaywrightBrowserExecutor:
        def __init__(
            self,
            resolver: NetworkResolver | None = None,
            *,
            launch_args: tuple[str, ...] = (),
            playwright_factory: Callable[[], Playwright] | None = None,
        ) -> None:
            self._resolver = resolver or SystemNetworkResolver()
            self._launch_args = validate_launch_args(launch_args)
            self._playwright_factory = playwright_factory
            self._playwright: Playwright | None = None
            self._browser: Browser | None = None

        def execute(self, request: BrowserAcquisitionRequest) -> BrowserRuntimeResult:
            browser = self._ensure_browser()
            guard = BrowserRequestGuard(request, self._resolver)
            context: BrowserContext | None = None
            page: Page | None = None
            blocked: list[BlockedRequestDiagnostic] = []
            fatal: BrowserSecurityError | None = None
            closed_popup_count = 0
            cancelled_download_count = 0
            main_navigation_count = 0

            def record(error: BrowserPolicyBlocked) -> None:
                nonlocal fatal
                blocked.append(error.diagnostic())
                if error.main_document or error.code in {
                    "BROWSER_REQUEST_LIMIT_EXCEEDED",
                    "BROWSER_DNS_ADDRESS_LIMIT_EXCEEDED",
                }:
                    fatal = error

            def route_handler(route: Route, intercepted: Request) -> None:
                nonlocal fatal, main_navigation_count
                main_document = bool(
                    page is not None
                    and intercepted.is_navigation_request()
                    and intercepted.frame == page.main_frame
                )
                if main_document:
                    main_navigation_count += 1
                try:
                    guard.authorize(
                        intercepted.url,
                        intercepted.resource_type,
                        main_document=main_document,
                    )
                except BrowserPolicyBlocked as error:
                    record(error)
                    route.abort("blockedbyclient")
                except BrowserDnsUnavailable as error:
                    fatal = error
                    route.abort("internetdisconnected")
                else:
                    route.continue_()

            def web_socket_handler(socket_route: WebSocketRoute) -> None:
                nonlocal fatal
                try:
                    guard.authorize(
                        socket_route.url,
                        "websocket",
                        main_document=False,
                    )
                except BrowserPolicyBlocked as error:
                    record(error)
                    socket_route.close(code=1008, reason="blocked by browser policy")
                except BrowserDnsUnavailable as error:
                    fatal = error
                    socket_route.close(code=1011, reason="DNS unavailable")
                else:
                    socket_route.connect_to_server()

            def close_popup(popup: Page) -> None:
                nonlocal closed_popup_count
                closed_popup_count += 1
                popup.close()

            def cancel_download(download: Download) -> None:
                nonlocal cancelled_download_count
                cancelled_download_count += 1
                download.cancel()

            try:
                context = browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    java_script_enabled=True,
                )
                context.clear_permissions()
                context.set_default_navigation_timeout(
                    request.policy.navigation_timeout_milliseconds
                )
                context.route("**/*", route_handler)
                context.route_web_socket("**/*", web_socket_handler)
                page = context.new_page()
                page.on("popup", close_popup)
                page.on("download", cancel_download)
                response = page.goto(
                    request.target_url,
                    wait_until=request.policy.wait_until,
                    timeout=request.policy.navigation_timeout_milliseconds,
                )
                if fatal is not None:
                    raise fatal
                redirect_count = max(0, main_navigation_count - 1)
                if redirect_count > request.policy.maximum_redirects:
                    raise BrowserPolicyBlocked(
                        "BROWSER_REDIRECT_LIMIT_EXCEEDED",
                        page.url,
                        "document",
                        main_document=True,
                    )
                if request.policy.post_load_wait_milliseconds:
                    page.wait_for_timeout(request.policy.post_load_wait_milliseconds)
                if fatal is not None:
                    raise fatal
                final_url = canonical_http_url(page.url)
                rendered = page.content().encode("utf-8")
                if len(rendered) > request.policy.maximum_rendered_document_bytes:
                    raise RenderedDocumentTooLarge(
                        "rendered browser document exceeds the configured byte limit"
                    )
                diagnostics = canonical_blocked_diagnostics(
                    (item for item in blocked if not item.main_document),
                    maximum=request.policy.maximum_blocked_diagnostics,
                )
                return BrowserRuntimeResult(
                    renderedDocument=rendered,
                    finalUrl=final_url,
                    finalHttpStatus=response.status if response is not None else None,
                    redirectCount=redirect_count,
                    interceptedRequestCount=guard.request_count,
                    blockedRequests=diagnostics,
                    closedPopupCount=closed_popup_count,
                    cancelledDownloadCount=cancelled_download_count,
                    browserEngine="chromium",
                    browserVersion=browser.version,
                )
            except (BrowserPolicyBlocked, BrowserDnsUnavailable, RenderedDocumentTooLarge):
                raise
            except PlaywrightTimeoutError as exc:
                raise BrowserRuntimeTransientError("browser navigation timed out") from exc
            except PlaywrightError as exc:
                if fatal is not None:
                    raise fatal from exc
                raise BrowserRuntimeTransientError("Playwright browser execution failed") from exc
            finally:
                if context is not None:
                    try:
                        context.clear_cookies()
                    finally:
                        context.close()

        def close(self) -> None:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

        def _ensure_browser(self) -> Browser:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                playwright = (
                    self._playwright_factory()
                    if self._playwright_factory is not None
                    else sync_playwright().start()
                )
                browser = playwright.chromium.launch(
                    headless=True,
                    args=list(self._launch_args),
                )
            except PlaywrightError as exc:
                raise BrowserRuntimeTransientError(
                    "Playwright Chromium could not start"
                ) from exc
            self._playwright = playwright
            self._browser = browser
            return browser
    ''',
)

write(
    "connectors/playwright_browser/src/playwright_browser/__init__.py",
    r'''
    from playwright_browser.executor import (
        BrowserRuntimeTransientError,
        PlaywrightBrowserExecutor,
        RenderedDocumentTooLarge,
    )

    __all__ = [
        "BrowserRuntimeTransientError",
        "PlaywrightBrowserExecutor",
        "RenderedDocumentTooLarge",
    ]
    ''',
)

write(
    "apps/browser_worker/src/browser_worker/gateway.py",
    r'''
    from __future__ import annotations

    from hashlib import sha256

    from browser_contracts import (
        BrowserAcquisitionMetadata,
        browser_output_digest,
    )
    from source_connector_sdk import SourceWorkerGateway, WorkerLease, WorkFailureKind

    _OUTPUT_CONTRACTS = frozenset({"browser-acquisition-result@1"})
    _DOCUMENT_ROLE = "browser_rendered_document"
    _METADATA_ROLE = "browser_acquisition_metadata"
    _DOCUMENT_CONTENT_TYPE = "text/html; charset=utf-8"
    _METADATA_CONTENT_TYPE = (
        "application/vnd.collection.browser-acquisition-metadata+json"
    )


    class SdkBrowserWorkerGateway:
        def __init__(self, client: SourceWorkerGateway) -> None:
            self._client = client
            self._build_identity: str | None = None

        def register(self, *, build_identity: str) -> None:
            self._client.register(
                build_identity=build_identity,
                capabilities={"browser_fetch"},
                supported_output_contracts=_OUTPUT_CONTRACTS,
                max_concurrency=1,
                resource_profile="playwright-browser",
            )
            self._build_identity = build_identity

        def acquire(
            self,
            *,
            lease_duration_seconds: int,
            heartbeat_interval_seconds: int,
        ) -> WorkerLease | None:
            return self._client.acquire_lease(
                capability="browser_fetch",
                lease_duration_seconds=lease_duration_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            )

        def read_request(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes:
            artifact = lease.artifact("browser_request")
            return self._client.read_artifact(
                lease,
                artifact_id=artifact.artifact_id,
                maximum_bytes=maximum_bytes,
            )

        def publish_result(
            self,
            lease: WorkerLease,
            *,
            rendered_document: bytes,
            metadata: BrowserAcquisitionMetadata,
        ) -> None:
            document_upload = self._client.upload_bytes(
                lease,
                content=rendered_document,
                artifact_kind="raw_artifact",
                content_type=_DOCUMENT_CONTENT_TYPE,
            )
            expected_document_digest = (
                f"sha256:{sha256(rendered_document).hexdigest()}"
            )
            if document_upload.content_digest != expected_document_digest:
                raise RuntimeError("browser document upload digest mismatch")
            if metadata.rendered_document_digest != expected_document_digest:
                raise RuntimeError("browser metadata document digest mismatch")
            metadata_upload = self._client.upload_bytes(
                lease,
                content=metadata.canonical_bytes(),
                artifact_kind="diagnostic_artifact",
                content_type=_METADATA_CONTENT_TYPE,
            )
            if metadata_upload.content_digest != metadata.digest():
                raise RuntimeError("browser metadata upload digest mismatch")
            output_digest = browser_output_digest(
                output_contract=lease.expected_output_contract,
                rendered_document_digest=document_upload.content_digest,
                metadata_digest=metadata_upload.content_digest,
            )
            self._client.complete(
                lease,
                output_contract=lease.expected_output_contract,
                output_digest=output_digest,
                worker_build_identity=self._required_build_identity(),
                output_artifacts=(
                    (document_upload.upload_id, _DOCUMENT_ROLE),
                    (metadata_upload.upload_id, _METADATA_ROLE),
                ),
            )

        def fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            error_code: str,
            message: str,
            required_action: str,
        ) -> None:
            self._client.fail(
                lease,
                failure_kind=failure_kind,
                code=error_code,
                owner="BrowserWorker.Playwright",
                message=message,
                required_action=required_action,
                worker_build_identity=self._required_build_identity(),
            )

        def _required_build_identity(self) -> str:
            if self._build_identity is None:
                raise RuntimeError("browser worker must register before processing work")
            return self._build_identity
    ''',
)

write(
    "apps/browser_worker/src/browser_worker/worker.py",
    r'''
    from __future__ import annotations

    import time
    from collections.abc import Callable
    from datetime import UTC, datetime
    from typing import Protocol

    from browser_contracts import (
        BrowserAcquisitionRequest,
        BrowserRuntimeResult,
        build_metadata,
    )
    from browser_security import BrowserDnsUnavailable, BrowserPolicyBlocked
    from playwright_browser import (
        BrowserRuntimeTransientError,
        PlaywrightBrowserExecutor,
        RenderedDocumentTooLarge,
    )
    from source_connector_sdk import WorkerLease, WorkFailureKind

    _EXPECTED_OUTPUT_CONTRACT = "browser-acquisition-result@1"


    class BrowserWorkerGateway(Protocol):
        def register(self, *, build_identity: str) -> None: ...

        def acquire(
            self,
            *,
            lease_duration_seconds: int,
            heartbeat_interval_seconds: int,
        ) -> WorkerLease | None: ...

        def read_request(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes: ...

        def publish_result(
            self,
            lease: WorkerLease,
            *,
            rendered_document: bytes,
            metadata: object,
        ) -> None: ...

        def fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            error_code: str,
            message: str,
            required_action: str,
        ) -> None: ...


    class BrowserExecutor(Protocol):
        def execute(self, request: BrowserAcquisitionRequest) -> BrowserRuntimeResult: ...

        def close(self) -> None: ...


    class BrowserWorker:
        def __init__(
            self,
            gateway: BrowserWorkerGateway,
            executor: BrowserExecutor | None = None,
            *_: object,
            build_identity: str = "browser-worker-unknown",
            lease_duration_seconds: int = 300,
            heartbeat_interval_seconds: int = 30,
            request_maximum_bytes: int = 512 * 1024,
            now_utc: Callable[[], datetime] | None = None,
            connector: BrowserExecutor | None = None,
            client: BrowserExecutor | None = None,
            **__: object,
        ) -> None:
            self._gateway = gateway
            self._executor = executor or connector or client or PlaywrightBrowserExecutor()
            self._build_identity = build_identity
            self._lease_duration_seconds = lease_duration_seconds
            self._heartbeat_interval_seconds = heartbeat_interval_seconds
            self._request_maximum_bytes = request_maximum_bytes
            self._now_utc = now_utc or (lambda: datetime.now(UTC))
            self._registered = False

        def run_once(self) -> bool:
            self._ensure_registered()
            lease = self._gateway.acquire(
                lease_duration_seconds=self._lease_duration_seconds,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            )
            if lease is None:
                return False
            try:
                request = BrowserAcquisitionRequest.from_bytes(
                    self._gateway.read_request(
                        lease,
                        maximum_bytes=self._request_maximum_bytes,
                    )
                )
            except (UnicodeError, ValueError):
                self._fail(
                    lease,
                    failure_kind="permanent",
                    code="BROWSER_REQUEST_INVALID",
                    message="Browser acquisition request is invalid",
                    action=(
                        "Correct the exact browser request and authorization before "
                        "creating replacement work."
                    ),
                )
                return True

            try:
                _validate_lease_and_authorization(
                    lease,
                    request,
                    now_utc=self._now_utc(),
                )
                runtime = self._executor.execute(request)
                metadata = build_metadata(
                    request,
                    runtime,
                    worker_build_identity=self._build_identity,
                )
                self._gateway.publish_result(
                    lease,
                    rendered_document=runtime.rendered_document,
                    metadata=metadata,
                )
            except BrowserPolicyBlocked as error:
                self._fail(
                    lease,
                    failure_kind="policy_blocked",
                    code=error.code,
                    message="Browser request was blocked by the approved network policy",
                    action=(
                        "Review the exact authorization and allowlist; do not bypass the "
                        "blocked network target."
                    ),
                )
            except ValueError:
                self._fail(
                    lease,
                    failure_kind="policy_blocked",
                    code="BROWSER_AUTHORIZATION_INVALID",
                    message="Browser authorization or source ownership is invalid",
                    action=(
                        "Create a new explicit non-expired browser authorization for the "
                        "exact source, policy, robots decision, and target."
                    ),
                )
            except BrowserDnsUnavailable:
                self._fail(
                    lease,
                    failure_kind="transient",
                    code="BROWSER_DNS_UNAVAILABLE",
                    message="Approved browser origin could not be resolved",
                    action="Restore DNS availability and retry the exact work unit.",
                )
            except BrowserRuntimeTransientError:
                self._fail(
                    lease,
                    failure_kind="transient",
                    code="BROWSER_RUNTIME_TRANSIENT",
                    message="Playwright browser execution failed transiently",
                    action="Restore the browser runtime and retry the exact work unit.",
                )
            except RenderedDocumentTooLarge:
                self._fail(
                    lease,
                    failure_kind="permanent",
                    code="BROWSER_RENDERED_DOCUMENT_TOO_LARGE",
                    message="Rendered browser document exceeds the approved byte limit",
                    action=(
                        "Change the explicit acquisition policy before creating replacement "
                        "work; do not truncate evidence silently."
                    ),
                )
            except Exception:  # noqa: BLE001
                self._fail(
                    lease,
                    failure_kind="permanent",
                    code="BROWSER_WORKER_DEFECT",
                    message="Browser worker encountered an internal defect",
                    action=(
                        "Inspect the browser build and exact immutable request before "
                        "creating replacement work."
                    ),
                )
            return True

        def run_forever(
            self,
            *,
            poll_interval_seconds: float = 1.0,
            maximum_idle_cycles: int | None = None,
            **_: object,
        ) -> None:
            idle_cycles = 0
            try:
                while maximum_idle_cycles is None or idle_cycles < maximum_idle_cycles:
                    if self.run_once():
                        idle_cycles = 0
                        continue
                    idle_cycles += 1
                    time.sleep(poll_interval_seconds)
            finally:
                self._executor.close()

        def _ensure_registered(self) -> None:
            if self._registered:
                return
            self._gateway.register(build_identity=self._build_identity)
            self._registered = True

        def _fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            code: str,
            message: str,
            action: str,
        ) -> None:
            self._gateway.fail(
                lease,
                failure_kind=failure_kind,
                error_code=code,
                message=message,
                required_action=action,
            )


    def _validate_lease_and_authorization(
        lease: WorkerLease,
        request: BrowserAcquisitionRequest,
        *,
        now_utc: datetime,
    ) -> None:
        if lease.expected_output_contract != _EXPECTED_OUTPUT_CONTRACT:
            raise ValueError("browser lease output contract is not supported")
        if lease.source_key is None:
            raise ValueError("browser work requires source-capacity ownership")
        if lease.source_key != request.source_key:
            raise ValueError("browser lease source does not match request")
        if lease.source_policy_digest != request.source_policy_digest:
            raise ValueError("browser lease source policy does not match request")
        request.authorization.validate_for_execution(
            now_utc=now_utc,
            target_url=request.target_url,
            source_key=request.source_key,
            source_policy_digest=request.source_policy_digest,
        )
    ''',
)

write(
    "apps/browser_worker/src/browser_worker/__init__.py",
    r'''
    from browser_worker.gateway import SdkBrowserWorkerGateway
    from browser_worker.worker import BrowserWorker

    __all__ = ["BrowserWorker", "SdkBrowserWorkerGateway"]
    ''',
)

app_text = transformed(
    "apps/extraction_worker/src/extraction_worker/app.py",
    (
        ("extraction_core", "playwright_browser"),
        ("ExtractionEngine", "PlaywrightBrowserExecutor"),
        ("extraction_worker", "browser_worker"),
        ("SdkExtractionWorkerGateway", "SdkBrowserWorkerGateway"),
        ("ExtractionWorker", "BrowserWorker"),
        ("Extraction", "Browser"),
        ("EXTRACTION", "BROWSER"),
        ("extraction", "browser"),
    ),
)
app_text = re.sub(
    r"from playwright_browser import \([^)]*\)",
    "from playwright_browser import PlaywrightBrowserExecutor",
    app_text,
    flags=re.S,
)
app_text = re.sub(
    r"from playwright_browser import [^\n]+",
    "from playwright_browser import PlaywrightBrowserExecutor",
    app_text,
    count=1,
)
write("apps/browser_worker/src/browser_worker/app.py", app_text)

contracts_text = transformed(
    "apps/extraction_worker/src/extraction_worker/contracts.py",
    (
        ("extraction_worker", "browser_worker"),
        ("Extraction", "Browser"),
        ("EXTRACTION", "BROWSER"),
        ("extraction", "browser"),
    ),
)
write("apps/browser_worker/src/browser_worker/contracts.py", contracts_text)

main_source = ROOT / "apps/extraction_worker/src/extraction_worker/main.py"
if main_source.exists():
    main_text = transformed(
        "apps/extraction_worker/src/extraction_worker/main.py",
        (
            ("extraction_worker", "browser_worker"),
            ("Extraction", "Browser"),
            ("EXTRACTION", "BROWSER"),
            ("extraction", "browser"),
        ),
    )
    write("apps/browser_worker/src/browser_worker/main.py", main_text)
else:
    write(
        "apps/browser_worker/src/browser_worker/main.py",
        r'''
        from browser_worker.app import main

        if __name__ == "__main__":
            main()
        ''',
    )

write(
    "deploy/docker/browser-worker.Dockerfile",
    r'''
    FROM __PLAYWRIGHT_IMAGE__ AS build

    ENV UV_COMPILE_BYTECODE=1 \
        UV_LINK_MODE=copy \
        UV_PYTHON_INSTALL_DIR=/opt/uv-python

    WORKDIR /workspace

    RUN pip install --no-cache-dir uv==0.10.0

    COPY .python-version pyproject.toml uv.lock ./
    COPY apps/browser_worker ./apps/browser_worker
    COPY connectors/playwright_browser ./connectors/playwright_browser
    COPY packages/browser_contracts ./packages/browser_contracts
    COPY packages/browser_security ./packages/browser_security
    COPY packages/collection_contracts ./packages/collection_contracts
    COPY packages/source_connector_sdk ./packages/source_connector_sdk

    RUN uv python install 3.13.14 \
        && uv sync \
            --python 3.13.14 \
            --frozen \
            --no-dev \
            --no-editable \
            --package browser-worker

    FROM __PLAYWRIGHT_IMAGE__ AS runtime

    ENV PATH="/workspace/.venv/bin:${PATH}" \
        PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONUNBUFFERED=1 \
        HOME=/home/browser-worker \
        XDG_CACHE_HOME=/home/browser-worker/.cache \
        TMPDIR=/tmp/browser-worker

    RUN groupadd --gid 10001 browser-worker \
        && useradd --uid 10001 --gid browser-worker --create-home browser-worker \
        && mkdir -p /tmp/browser-worker /home/browser-worker/.cache \
        && chown -R 10001:10001 /tmp/browser-worker /home/browser-worker

    WORKDIR /workspace
    COPY --from=build /opt/uv-python /opt/uv-python
    COPY --from=build --chown=10001:10001 /workspace/.venv /workspace/.venv

    USER 10001:10001

    ENTRYPOINT ["browser-worker"]
    ''',
)
