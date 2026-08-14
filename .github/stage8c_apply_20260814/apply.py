from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def main() -> int:
    if not (ROOT / "docs/proofs/stage8b-control-api-ci.md").exists():
        raise RuntimeError("Stage 8B exact-head proof is required before Stage 8C")

    write(
        "apps/review_web/package.json",
        '''{
  "name": "collection-review-web",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "vite build",
    "check:architecture": "node tools/check-architecture.mjs",
    "generate:api": "openapi-typescript ../../contracts/control_api/openapi.json -o src/shared/api/generated/schema.ts",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {},
  "devDependencies": {}
}
''',
    )
    write(
        "apps/review_web/tsconfig.json",
        '''{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src", "vite.config.ts"]
}
''',
    )
    write(
        "apps/review_web/vite.config.ts",
        '''import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
''',
    )
    write(
        "apps/review_web/index.html",
        '''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="color-scheme" content="light dark" />
    <title>Collection Review Console</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
''',
    )
    write(
        "apps/review_web/src/shared/api/controlApi.ts",
        '''import type { components } from "./generated/schema";

export type ReviewQueueResponse = components["schemas"]["ReviewQueueResponse"];
export type ReviewCaseDetail = components["schemas"]["ReviewCaseDetailResponse"];
export type DecisionResponse = components["schemas"]["DecisionResponse"];
export type ManualObservation = components["schemas"]["ManualObservation"];
export type SuppressionRevision = components["schemas"]["SuppressionRevision"];
export type SubmitDecisionRequest = components["schemas"]["SubmitDecisionRequest"];
export type ManualObservationRequest = components["schemas"]["ManualObservationRequest"];
export type ActivateSuppressionRequest = components["schemas"]["ActivateSuppressionRequest"];
export type ResolveSuppressionRequest = components["schemas"]["ResolveSuppressionRequest"];

type ReviewState = "open" | "decided";

export class ControlApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;

  constructor(status: number, code: string, message: string, correlationId: string | null) {
    super(message);
    this.name = "ControlApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
  }
}

export async function listReviewCases(
  token: string,
  state: ReviewState,
  cursor?: string,
): Promise<ReviewQueueResponse> {
  const query = new URLSearchParams({ state, limit: "50" });
  if (cursor) query.set("cursor", cursor);
  return request<ReviewQueueResponse>(token, `/review/cases?${query.toString()}`);
}

export function getReviewCase(token: string, caseId: string): Promise<ReviewCaseDetail> {
  return request<ReviewCaseDetail>(token, `/review/cases/${encodeURIComponent(caseId)}`);
}

export function submitReviewDecision(
  token: string,
  caseId: string,
  body: SubmitDecisionRequest,
): Promise<DecisionResponse> {
  return request<DecisionResponse>(
    token,
    `/review/cases/${encodeURIComponent(caseId)}/decisions`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function addManualObservation(
  token: string,
  candidateId: string,
  body: ManualObservationRequest,
): Promise<ManualObservation> {
  return request<ManualObservation>(
    token,
    `/review/candidates/${encodeURIComponent(candidateId)}/manual-observations`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function activateSuppression(
  token: string,
  body: ActivateSuppressionRequest,
): Promise<SuppressionRevision> {
  return request<SuppressionRevision>(token, "/review/suppressions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function resolveSuppression(
  token: string,
  suppressionId: string,
  body: ResolveSuppressionRequest,
): Promise<SuppressionRevision> {
  return request<SuppressionRevision>(
    token,
    `/review/suppressions/${encodeURIComponent(suppressionId)}/resolve`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

async function request<T>(
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const correlationId = crypto.randomUUID();
  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Correlation-ID": correlationId,
      ...init.headers,
    },
  });
  if (!response.ok) {
    const payload = await safeJson(response);
    const message = stringValue(payload, "message") ?? `Control API request failed (${response.status}).`;
    const code = stringValue(payload, "code") ?? "CONTROL_API_REQUEST_FAILED";
    throw new ControlApiError(
      response.status,
      code,
      message,
      response.headers.get("X-Correlation-ID"),
    );
  }
  return (await response.json()) as T;
}

async function safeJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function stringValue(value: unknown, key: string): string | null {
  if (!value || typeof value !== "object") return null;
  const candidate = (value as Record<string, unknown>)[key];
  return typeof candidate === "string" ? candidate : null;
}
''',
    )
    write(
        "apps/review_web/src/features/auth/AuthGate.tsx",
        '''import { type FormEvent, type ReactNode, useState } from "react";

const STORAGE_KEY = "collection-review-token";

export function AuthGate({ children }: { children: (token: string, signOut: () => void) => ReactNode }) {
  const [token, setToken] = useState(() => sessionStorage.getItem(STORAGE_KEY) ?? "");
  const [draft, setDraft] = useState("");

  if (!token) {
    const submit = (event: FormEvent) => {
      event.preventDefault();
      const canonical = draft.trim();
      if (!canonical) return;
      sessionStorage.setItem(STORAGE_KEY, canonical);
      setDraft("");
      setToken(canonical);
    };
    return (
      <main className="auth-shell">
        <form className="auth-card" onSubmit={submit}>
          <p className="eyebrow">Collection operations</p>
          <h1>Review console</h1>
          <p>Enter a reviewer credential. It is retained only for this browser session.</p>
          <label htmlFor="review-token">Reviewer bearer token</label>
          <input
            id="review-token"
            name="review-token"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            required
          />
          <button type="submit">Open review queue</button>
        </form>
      </main>
    );
  }

  const signOut = () => {
    sessionStorage.removeItem(STORAGE_KEY);
    setToken("");
  };
  return <>{children(token, signOut)}</>;
}
''',
    )
    write(
        "apps/review_web/src/features/review/ReviewWorkspace.tsx",
        '''import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ControlApiError,
  activateSuppression,
  addManualObservation,
  getReviewCase,
  listReviewCases,
  resolveSuppression,
  submitReviewDecision,
  type ReviewCaseDetail,
  type ReviewQueueResponse,
} from "../../shared/api/controlApi";

type ReviewState = "open" | "decided";

export function ReviewWorkspace({ token, signOut }: { token: string; signOut: () => void }) {
  const [state, setState] = useState<ReviewState>("open");
  const [queue, setQueue] = useState<ReviewQueueResponse>({ items: [], nextCursor: null });
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadQueue = useCallback(async (append = false, cursor?: string) => {
    setBusy(true);
    setError(null);
    try {
      const page = await listReviewCases(token, state, cursor);
      setQueue((current) => ({
        items: append ? [...current.items, ...page.items] : page.items,
        nextCursor: page.nextCursor,
      }));
      if (!append && page.items.length && !selectedCaseId) {
        setSelectedCaseId(page.items[0].caseId);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }, [selectedCaseId, state, token]);

  const loadDetail = useCallback(async (caseId: string) => {
    setBusy(true);
    setError(null);
    try {
      setDetail(await getReviewCase(token, caseId));
    } catch (caught) {
      setError(errorMessage(caught));
      setDetail(null);
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => {
    setSelectedCaseId(null);
    setDetail(null);
    void loadQueue(false);
  }, [state]);

  useEffect(() => {
    if (selectedCaseId) void loadDetail(selectedCaseId);
  }, [loadDetail, selectedCaseId]);

  const refresh = async () => {
    await loadQueue(false);
    if (selectedCaseId) await loadDetail(selectedCaseId);
  };

  return (
    <main className="workspace">
      <header className="topbar">
        <div>
          <p className="eyebrow">Collection operations</p>
          <h1>Review console</h1>
        </div>
        <div className="topbar-actions">
          <button type="button" className="secondary" onClick={() => void refresh()} disabled={busy}>Refresh</button>
          <button type="button" className="secondary" onClick={signOut}>Sign out</button>
        </div>
      </header>

      {error ? <div role="alert" className="error-banner">{error}</div> : null}
      <div className="workspace-grid">
        <aside className="queue-panel" aria-label="Review queue">
          <div className="segmented" aria-label="Review state">
            {(["open", "decided"] as const).map((value) => (
              <button
                type="button"
                key={value}
                aria-pressed={state === value}
                onClick={() => setState(value)}
              >
                {value === "open" ? "Open" : "Decided"}
              </button>
            ))}
          </div>
          <ol className="queue-list">
            {queue.items.map((item) => (
              <li key={item.caseId}>
                <button
                  type="button"
                  className={selectedCaseId === item.caseId ? "queue-item selected" : "queue-item"}
                  onClick={() => setSelectedCaseId(item.caseId)}
                >
                  <strong>{item.reasonCodes.join(", ")}</strong>
                  <span>Candidate {item.candidateId}</span>
                  <small>revision {item.revision}</small>
                </button>
              </li>
            ))}
          </ol>
          {!queue.items.length && !busy ? <p className="empty">No cases in this queue.</p> : null}
          {queue.nextCursor ? (
            <button
              type="button"
              className="secondary full-width"
              disabled={busy}
              onClick={() => void loadQueue(true, queue.nextCursor ?? undefined)}
            >
              Load more
            </button>
          ) : null}
        </aside>
        <section className="detail-panel" aria-live="polite">
          {busy && !detail ? <p>Loading review evidence…</p> : null}
          {detail ? (
            <CaseDetail token={token} detail={detail} onChanged={refresh} setError={setError} />
          ) : !busy ? (
            <p className="empty">Select a review case.</p>
          ) : null}
        </section>
      </div>
    </main>
  );
}

function CaseDetail({
  token,
  detail,
  onChanged,
  setError,
}: {
  token: string;
  detail: ReviewCaseDetail;
  onChanged: () => Promise<void>;
  setError: (message: string | null) => void;
}) {
  const candidatePayload = useMemo(
    () => JSON.stringify(detail.candidate.normalized_payload, null, 2),
    [detail.candidate.normalized_payload],
  );
  return (
    <div className="case-stack">
      <section className="case-header">
        <p className="eyebrow">Case {detail.case.case_id}</p>
        <h2>{detail.case.reason_codes.join(", ")}</h2>
        <dl className="facts">
          <div><dt>Candidate</dt><dd>{detail.candidate.candidate_id}</dd></div>
          <div><dt>Entity kind</dt><dd>{detail.candidate.entity_kind}</dd></div>
          <div><dt>Resolution</dt><dd>{detail.candidate.resolution_state}</dd></div>
          <div><dt>Export eligible</dt><dd>{detail.quality?.export_eligible ? "Yes" : "No"}</dd></div>
        </dl>
      </section>
      <section>
        <h3>Candidate payload</h3>
        <pre>{candidatePayload}</pre>
      </section>
      <section>
        <h3>Evidence</h3>
        <ul>{detail.candidate.evidence.map((item) => <li key={item.evidence_digest}>{item.evidence_kind}: {item.evidence_digest}</li>)}</ul>
      </section>
      <section>
        <h3>Quality blockers</h3>
        <ul>{detail.quality?.blockers.length ? detail.quality.blockers.map((item) => <li key={item}>{item}</li>) : <li>None reported.</li>}</ul>
      </section>
      <section>
        <h3>Decision history</h3>
        <ol>{detail.decisions.map((decision) => <li key={decision.decision_id}><strong>{decision.outcome}</strong> by {decision.actor_id}: {decision.rationale}</li>)}</ol>
      </section>
      <DecisionForm token={token} detail={detail} onChanged={onChanged} setError={setError} />
      <ManualObservationForm token={token} detail={detail} onChanged={onChanged} setError={setError} />
      <SuppressionPanel token={token} detail={detail} onChanged={onChanged} setError={setError} />
    </div>
  );
}

function DecisionForm({ token, detail, onChanged, setError }: FormProps) {
  const [outcome, setOutcome] = useState("accept_candidate");
  const [rationale, setRationale] = useState("");
  const [evidence, setEvidence] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await submitReviewDecision(token, detail.case.case_id, {
        expectedRevision: detail.case.revision,
        outcome: outcome as "accept_candidate",
        rationale,
        evidenceReferences: canonicalValues(evidence),
        supersedesDecisionId: detail.case.current_decision_id,
      });
      setRationale("");
      setEvidence("");
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };
  return (
    <form className="action-card" onSubmit={(event) => void submit(event)}>
      <h3>Record decision</h3>
      <label>Outcome<select value={outcome} onChange={(event) => setOutcome(event.target.value)}>{["accept_candidate", "reject_candidate", "approve_merge", "reject_merge", "request_recollection", "block_export"].map((value) => <option key={value}>{value}</option>)}</select></label>
      <label>Rationale<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} required /></label>
      <label>Evidence digests<textarea value={evidence} onChange={(event) => setEvidence(event.target.value)} placeholder="One sha256 digest per line" required /></label>
      <button type="submit">Save immutable decision</button>
    </form>
  );
}

function ManualObservationForm({ token, detail, onChanged, setError }: FormProps) {
  const [fieldKey, setFieldKey] = useState("");
  const [valueText, setValueText] = useState("");
  const [reasonCode, setReasonCode] = useState("MANUAL_VERIFICATION");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await addManualObservation(token, detail.candidate.candidate_id, {
        candidateRevision: detail.candidate.revision,
        fieldKey,
        valueText,
        reasonCode,
        supersedesObservationId: null,
      });
      setFieldKey("");
      setValueText("");
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };
  return (
    <form className="action-card" onSubmit={(event) => void submit(event)}>
      <h3>Add manual observation</h3>
      <label>Field key<input value={fieldKey} onChange={(event) => setFieldKey(event.target.value)} pattern="[a-z][a-z0-9_]*" required /></label>
      <label>Value<textarea value={valueText} onChange={(event) => setValueText(event.target.value)} required /></label>
      <label>Reason code<input value={reasonCode} onChange={(event) => setReasonCode(event.target.value)} pattern="[A-Z][A-Z0-9_]*" required /></label>
      <button type="submit">Append evidence</button>
    </form>
  );
}

function SuppressionPanel({ token, detail, onChanged, setError }: FormProps) {
  const [reasonCode, setReasonCode] = useState("LEGAL_REVIEW");
  const [evidence, setEvidence] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await activateSuppression(token, {
        targetKind: "candidate",
        targetId: detail.candidate.candidate_id,
        scopes: ["discovery", "export"],
        reasonCode,
        evidenceReference: evidence.trim(),
        expiresAtUtc: null,
      });
      setEvidence("");
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };
  const resolve = async (suppressionId: string, revision: number) => {
    setError(null);
    try {
      await resolveSuppression(token, suppressionId, {
        expectedRevision: revision,
        evidenceReference: evidence.trim(),
      });
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };
  return (
    <section className="action-card">
      <h3>Suppress collection use</h3>
      <form onSubmit={(event) => void submit(event)}>
        <label>Reason code<input value={reasonCode} onChange={(event) => setReasonCode(event.target.value)} pattern="[A-Z][A-Z0-9_]*" required /></label>
        <label>Evidence digest<input value={evidence} onChange={(event) => setEvidence(event.target.value)} pattern="sha256:[0-9a-f]{64}" required /></label>
        <button type="submit">Activate discovery/export suppression</button>
      </form>
      <ul>{detail.activeSuppressions.map((item) => <li key={item.suppression_id}>{item.reason_code} ({item.scopes.join(", ")}) <button type="button" className="secondary" disabled={!evidence.trim()} onClick={() => void resolve(item.suppression_id, item.revision)}>Resolve with current evidence</button></li>)}</ul>
    </section>
  );
}

type FormProps = {
  token: string;
  detail: ReviewCaseDetail;
  onChanged: () => Promise<void>;
  setError: (message: string | null) => void;
};

function canonicalValues(value: string): string[] {
  return [...new Set(value.split(/\s+/).map((item) => item.trim()).filter(Boolean))].sort();
}

function errorMessage(error: unknown): string {
  if (error instanceof ControlApiError) {
    return `${error.code}: ${error.message}${error.correlationId ? ` (${error.correlationId})` : ""}`;
  }
  return error instanceof Error ? error.message : "Unexpected review console failure.";
}
''',
    )
    write(
        "apps/review_web/src/app/App.tsx",
        '''import { AuthGate } from "../features/auth/AuthGate";
import { ReviewWorkspace } from "../features/review/ReviewWorkspace";

export function App() {
  return (
    <AuthGate>
      {(token, signOut) => <ReviewWorkspace token={token} signOut={signOut} />}
    </AuthGate>
  );
}
''',
    )
    write(
        "apps/review_web/src/main.tsx",
        '''import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("review console root is missing");
createRoot(root).render(<StrictMode><App /></StrictMode>);
''',
    )
    write(
        "apps/review_web/src/styles.css",
        ''':root { font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #17202a; background: #f3f5f7; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; min-height: 100vh; }
button, input, textarea, select { font: inherit; }
button { cursor: pointer; border: 0; border-radius: .45rem; padding: .65rem .9rem; background: #173f5f; color: white; }
button:disabled { cursor: not-allowed; opacity: .55; }
button.secondary { color: #173f5f; background: #e7edf2; }
label { display: grid; gap: .35rem; font-weight: 600; }
input, textarea, select { width: 100%; border: 1px solid #b7c1ca; border-radius: .4rem; padding: .65rem; background: white; color: #17202a; }
textarea { min-height: 6rem; resize: vertical; }
.auth-shell { display: grid; min-height: 100vh; place-items: center; padding: 1rem; }
.auth-card { display: grid; gap: 1rem; width: min(32rem, 100%); padding: 2rem; background: white; border-radius: .8rem; box-shadow: 0 12px 35px rgb(25 46 66 / 12%); }
.eyebrow { margin: 0; color: #557086; font-size: .75rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.workspace { min-height: 100vh; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem 1.5rem; background: white; border-bottom: 1px solid #d9e0e6; }
.topbar h1 { margin: .15rem 0 0; }
.topbar-actions { display: flex; gap: .5rem; }
.error-banner { margin: 1rem; padding: .8rem 1rem; border: 1px solid #bd3f3f; border-radius: .5rem; background: #fff0f0; color: #7d1d1d; }
.workspace-grid { display: grid; grid-template-columns: minmax(18rem, 25rem) minmax(0, 1fr); min-height: calc(100vh - 5rem); }
.queue-panel { padding: 1rem; border-right: 1px solid #d9e0e6; background: #eef2f5; }
.segmented { display: grid; grid-template-columns: 1fr 1fr; gap: .35rem; margin-bottom: 1rem; }
.segmented button[aria-pressed="false"] { color: #173f5f; background: white; }
.queue-list { display: grid; gap: .5rem; padding: 0; list-style: none; }
.queue-item { display: grid; gap: .25rem; width: 100%; text-align: left; color: #243746; background: white; border: 1px solid transparent; }
.queue-item.selected { border-color: #173f5f; box-shadow: 0 0 0 2px rgb(23 63 95 / 12%); }
.queue-item span, .queue-item small { overflow: hidden; text-overflow: ellipsis; }
.full-width { width: 100%; }
.detail-panel { min-width: 0; padding: 1.5rem; }
.case-stack { display: grid; gap: 1rem; max-width: 74rem; }
.case-stack > section, .action-card { padding: 1rem; background: white; border: 1px solid #d9e0e6; border-radius: .6rem; }
.case-header h2 { margin-top: .25rem; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .75rem; }
.facts div { min-width: 0; }
.facts dt { color: #607788; font-size: .8rem; }
.facts dd { margin: .2rem 0 0; overflow-wrap: anywhere; }
pre { max-height: 24rem; overflow: auto; padding: 1rem; border-radius: .4rem; background: #111b24; color: #e6edf3; }
.action-card { display: grid; gap: .8rem; }
.action-card form { display: grid; gap: .8rem; }
.empty { color: #607788; }
@media (max-width: 800px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .workspace-grid { grid-template-columns: 1fr; }
  .queue-panel { border-right: 0; border-bottom: 1px solid #d9e0e6; }
}
@media (prefers-color-scheme: dark) {
  :root { color: #e5edf3; background: #111820; }
  body, .workspace { background: #111820; }
  .topbar, .auth-card, .case-stack > section, .action-card, .queue-item { background: #1b2731; border-color: #344653; }
  .queue-panel { background: #15202a; border-color: #344653; }
  input, textarea, select { background: #101820; color: #e5edf3; border-color: #506473; }
  button.secondary, .segmented button[aria-pressed="false"] { color: #dce8f1; background: #2b3d4b; }
}
''',
    )
    write(
        "apps/review_web/src/test/setup.ts",
        '''import "@testing-library/jest-dom/vitest";
''',
    )
    write(
        "apps/review_web/src/shared/api/controlApi.test.ts",
        '''import { afterEach, describe, expect, it, vi } from "vitest";
import { listReviewCases, submitReviewDecision } from "./controlApi";

const TOKEN = "secret-review-token";
const DIGEST = "sha256:" + "a".repeat(64);

afterEach(() => vi.unstubAllGlobals());

describe("Control API client", () => {
  it("sends the bearer credential only as an Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], nextCursor: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await listReviewCases(TOKEN, "open");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain(TOKEN);
    expect(init.headers).toMatchObject({ Authorization: `Bearer ${TOKEN}` });
  });

  it("never puts actor identity in a decision body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ case: {}, decision: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitReviewDecision(TOKEN, "case-1", {
      expectedRevision: 0,
      outcome: "accept_candidate",
      rationale: "Verified.",
      evidenceReferences: [DIGEST],
      supersedesDecisionId: null,
    });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body).not.toHaveProperty("actorId");
    expect(body).not.toHaveProperty("actor_id");
  });
});
''',
    )
    write(
        "apps/review_web/src/features/auth/AuthGate.test.tsx",
        '''import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it } from "vitest";
import { AuthGate } from "./AuthGate";

beforeEach(() => sessionStorage.clear());

it("keeps the reviewer credential only in session storage", async () => {
  const user = userEvent.setup();
  render(<AuthGate>{(token) => <p>Authenticated as {token.length}</p>}</AuthGate>);
  await user.type(screen.getByLabelText("Reviewer bearer token"), "a".repeat(40));
  await user.click(screen.getByRole("button", { name: "Open review queue" }));
  expect(screen.getByText("Authenticated as 40")).toBeInTheDocument();
  expect(localStorage.getItem("collection-review-token")).toBeNull();
  expect(sessionStorage.getItem("collection-review-token")).toBe("a".repeat(40));
});
''',
    )
    write(
        "apps/review_web/tools/check-architecture.mjs",
        '''import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = new URL("../src/", import.meta.url).pathname;
const files = walk(root).filter((path) => /\.(ts|tsx)$/.test(path));
const violations = [];
for (const file of files) {
  const text = readFileSync(file, "utf8");
  const name = relative(root, file);
  for (const forbidden of [
    "dangerouslySetInnerHTML",
    "actorId:",
    "actor_id:",
    "collection_infrastructure",
    "psycopg",
    "sqlalchemy",
  ]) {
    if (text.includes(forbidden) && !name.endsWith(".test.ts") && !name.endsWith(".test.tsx")) {
      violations.push(`${name}: forbidden review-web token ${forbidden}`);
    }
  }
}
const generated = join(root, "shared/api/generated/schema.ts");
if (!statSync(generated).isFile()) violations.push("generated OpenAPI schema is missing");
if (violations.length) {
  console.error(violations.join("\n"));
  process.exit(1);
}

function walk(path) {
  return readdirSync(path).flatMap((name) => {
    const target = join(path, name);
    return statSync(target).isDirectory() ? walk(target) : [target];
  });
}
''',
    )
    write(
        "deploy/docker/review-web.Dockerfile",
        '''FROM node:24-alpine AS build
WORKDIR /workspace/apps/review_web
COPY apps/review_web/package.json apps/review_web/package-lock.json ./
RUN npm ci
COPY contracts/control_api /workspace/contracts/control_api
COPY apps/review_web ./
RUN npm run generate:api && npm run typecheck && npm run test && npm run check:architecture && npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
COPY deploy/nginx/review-web.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/apps/review_web/dist /usr/share/nginx/html
EXPOSE 8080
''',
    )
    write(
        "deploy/nginx/review-web.conf",
        '''server {
  listen 8080;
  server_name _;
  root /usr/share/nginx/html;
  index index.html;

  add_header X-Content-Type-Options nosniff always;
  add_header Referrer-Policy no-referrer always;
  add_header Content-Security-Policy "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" always;

  location / {
    try_files $uri $uri/ /index.html;
  }
}
''',
    )
    write(
        "docs/specifications/stage8c-review-console.md",
        '''# Stage 8C — review console

## Boundary

`apps/review_web` is a browser-only React/TypeScript client generated from the checked-in Control API OpenAPI contract. It does not import Python packages, calculate match scores, decide export eligibility, or persist review state locally.

## Security and correctness

- Reviewer bearer credentials live only in `sessionStorage` and are sent only in the Authorization header.
- Request bodies contain no actor identity; the Control API derives the actor from the principal.
- React text rendering is used exclusively; `dangerouslySetInnerHTML` is forbidden by an architecture check.
- Decisions use the exact case revision returned by the API and expose immutable supersession when a current decision exists.
- Manual corrections append observations instead of rewriting source evidence.
- Suppression actions use explicit discovery/export scopes and exact evidence digests.
- Queue pagination uses the opaque cursor returned by the backend.
- The UI displays backend-owned quality and resolution state without recomputing them.

## Deployment

The build is served by an unprivileged nginx image on port 8080. `/api` remains a deployment-owned same-origin route to the Control API; the static image contains no backend credential.
''',
    )
    write(
        ".codex/modules/review-web.md",
        '''# Review web module

- Source: `apps/review_web`.
- Contract source: `contracts/control_api/openapi.json`.
- Generated API types: `src/shared/api/generated/schema.ts`.
- Reviewer token: session storage only.
- No local matching, quality, export, or actor ownership.
- No raw HTML rendering.
''',
    )
    status = ROOT / "docs/implementation-status.md"
    text = status.read_text(encoding="utf-8")
    marker = "## Stage 8C — review console"
    if marker not in text:
        text = text.rstrip() + f'''\n\n{marker}\n\nStatus: **React review console implemented against generated Control API types**.\n\n- Open/decided queues, case evidence, quality blockers, decisions, manual observations, and suppression actions are represented.\n- Actor identity is absent from request bodies and remains backend-owned.\n- Bearer credentials use session storage only.\n- Browser code cannot import collector database or infrastructure owners.\n- Full Compose integration and browser E2E remain later operational work.\n'''
        status.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
