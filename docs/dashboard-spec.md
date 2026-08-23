# Operator Dashboard — v1 Spec

Hand-off document for the build sessions. Everything here is decided:
page inventory (#40), refresh pattern (#42), layout verdict (#43), backend
contract (#41). Open questions remaining: none — testing approach is
§8, ratified with this spec.

Audience: the maintainer alone, loopback-bound, served by the proxy process
itself. Vocabulary per root `CONTEXT.md` (node, egress, proxy, health
ledger, request ring, reason buckets).

## 1. Stack (locked)

- Zero-build server-rendered Flask/Jinja; **no node_modules, no bundler**.
- Vendored htmx (already at `static/vendor/htmx.min.js`, ~48 KB raw /
  ~17 KB gzipped) — periodic polling only; SSE rejected (pins a gthread
  thread per tab); `<noscript>` meta-refresh fallback.
- One page, anchored sections: `/admin#nodes`, `/admin#requests`,
  `/admin#config` — each an independently-polling htmx fragment.

## 2. Layout (variant A verdict, #43)

Status-board structure: gauge strip over a full-width node table; config in
a collapsed footer `<details>`. Amendments from the verdict:

- Cooldown renders **countdown-first** (`42s`) with a thin progress bar.
- The node table reserves a **last-error column** (reason + relative time).
- A **recent-activity slot** sits below the node table (the requests
  section) — planned for from day one.
- Per-node sparklines are deferred (expandable-row/hover detail after ring
  buffers exist in a later iteration; not v1).

## 3. Sections

### 3.1 Nodes (`#nodes`, poll ~3s)

- Summary strip: nodes-available gauge · in-flight streams gauge ·
  draining badge.
- Node table columns: `#` (+ ▶ cursor marker on the rotation-cursor row) ·
  persona UA (post-#50 addition — deliberate, not in the original #40
  inventory) · egress host · status badge (`usable` / `cooling 42s`) ·
  cooldown countdown+bar · consecutive failures · last error (reason +
  ago) · quota % (ledger headroom, `budget_remaining_pct` from #48) ·
  ok/fail counts with derived success % · per-minute outcome sparkline.
- Sparkline: 60-minute outcome history from reason buckets, success vs
  failure shading.
- Per-node latency rollups are **out of v1**: latency is per-request in
  §3.2; a per-node aggregate needs ring scanning and lands later if
  wanted.

### 3.2 Recent requests (`#requests`, poll ~5s)

- Ring-backed table (200 entries, newest first): ts (relative, absolute on
  hover) · method/path · short request_id (click-to-copy full) · final node
  · outcome badge + duration · token totals when present.
- Row expands (htmx) to attempt-by-attempt detail: node, status, reason,
  duration per attempt.
- Explicit empty state on fresh boot ("no requests proxied yet").

### 3.3 Config (`#config`, static — never polls)

- Collapsible groups of effective `Settings` + `OptimizationConfig`, every
  value labeled with its env var; keys always masked.
- Banner: "frozen at boot — restart to change".

### 3.4 Chrome

- Global pause toggle (stops all polling).
- Per-fragment staleness self-report: "updated Ns ago", stale styling past
  2× poll interval — a dead API never looks healthy.
- Aggregate line (the single home for fleet rates): tokens served
  (last hour / lifetime) + request rate — both from reason buckets.
- Footer: uptime + started-at (no version string in v1).

## 4. Backend contract (`telemetry.py`, new module)

Owns the request ring **and** the per-node reason buckets under its own
RLock. `HealthLedger` untouched. Fed by the view layer; the transport never
imports it.

### 4.1 Reason taxonomy (per attempt)

`ok` · `rate_limited` (429) · `server_error` (5xx) · `timeout` ·
`connection` · `error` (other RequestException). Non-retryable completed
statuses (401 etc.) are transport-level `ok` — the status code rides along
for display.

### 4.2 Attempt detail seam

`SendResult` / `AllNodesFailed` carry `attempts: [{node_id, status, reason,
duration_ms}]` (`duration_ms` = `perf_counter` around the upstream call).
The view writes ring entries after `send()` returns.

### 4.3 Request ring

`deque(maxlen=200)` of: `ts, request_id, method, path, node_id, outcome
(ok | failed | single_shot_abort), status_code, ttfb_ms, total_ms, tokens
{prompt, completion, total} | None, attempts[]`. `single_shot_abort` = a
POST under `RETRY_POSTS=false` whose single attempt failed. No bodies, no
keys, no persistence. Latency rule for both modes: **TTFB = first byte
available to forward; total = last byte forwarded.** Streamed entries are
written on send-return and updated in place at stream end.

### 4.4 Reason buckets

Per node: per-minute counters over the taxonomy **plus a token-sum per
minute**; 60-minute retention, pruned on write. Powers sparklines, "last
failure Nm ago", last-hour aggregates. Lifetime token total: a counter fed
in the view at the existing usage-parse site (buffered 200 responses).

### 4.5 Gauges & identity

In-flight gauge and draining state from `ShutdownState` (exists). Uptime +
started-at from a timestamp captured in `create_app()`.

## 5. Routes & auth

| Route | Returns | Poll |
|---|---|---|
| `GET /admin` | Full page | — |
| `GET /admin/fragments/nodes` | `#nodes` partial | 3s |
| `GET /admin/fragments/requests` | `#requests` partial | 5s |
| `GET /admin/fragments/config` | `#config` partial | never |

- Auth: **loopback-trust** — same posture as `/health`. No cookie/bearer
  flow in v1; the named escalation path if exposure ever widens is a
  `PROXY_AUTH_TOKEN` login→cookie. CSRF moot (read-only, same-origin htmx).
- No JSON API in v1 (YAGNI until a second consumer exists).

## 6. `/metrics` parity (additive)

New series alongside the existing ones: per-reason attempt counters
(`llm_rotator_node_attempts_total{node_id,reason}`), in-flight gauge
(`llm_rotator_streams_inflight`), uptime seconds
(`llm_rotator_uptime_seconds`). Hand-rolled as today.

## 7. Non-goals (v1)

Action buttons (cooldown reset, drain-now) · editing config · JSON API ·
multi-user auth · multi-tab sync · persistence beyond process-local
buffers · mobile polish · Grafana link-outs.

## 8. Testing approach

- `telemetry.py`: unit tests on the deterministic-clock pattern (buckets
  prune/roll, ring eviction, in-place streamed update) — no HTTP.
- Fragment routes: Flask test client asserts rendered shapes (cursor row
  present, cooldown countdown, empty-state) against seeded telemetry.
- Transport attempts list: extends the existing scripted-session tests.
- Browser-level: one **local-only** playwright smoke (`/admin` renders, all
  three fragments load, pause toggle stops polling) — run manually via the
  playwright-cli skill, not gated in CI (browser deps not worth the runner
  cost for one page).

## 9. Build decomposition (proposed tickets)

1. **telemetry.py + attempts seam + /metrics parity** — module, transport
   `.attempts`, view wiring, new metric series, and the CONTEXT.md
   glossary entries for *request ring* and *reason buckets*. (Unit-tested.)
2. **`/admin` page + nodes & config fragments** — layout per §2–3,
   prototype folded in as the starting point, plus the bearer-gate
   exemption wiring for `/admin*` (currently hardcoded to
   `/health`, `/ready`, `/metrics`).
3. **Requests section** — ring table, expandable attempt rows, empty state.
4. **Chrome** — pause toggle, staleness self-report, aggregate line, footer.
