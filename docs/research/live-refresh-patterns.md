# Live-refresh patterns for a zero-build ops UI

**Ticket:** [ZeroClue/llm-rotator#42](https://github.com/ZeroClue/llm-rotator/issues/42) · part of Dashboard v1 (#39)
**Date:** 2026-08-23 · **Status:** research complete — recommendation below
**Scope:** Flask + Jinja fragments, no node_modules/bundler; gunicorn `gthread`, `GUNICORN_WORKERS=1`, `GUNICORN_THREADS=8`; loopback-bound single-operator console.

## TL;DR

**Recommendation for dashboard v1: htmx periodic polling (`hx-trigger="every 2s"`–`every 5s`) swapping small Jinja-rendered fragments, with htmx vendored as a single static file. Do not use SSE for dashboard refresh.** Under the pinned gunicorn 26.1.0 gthread worker, every open SSE response pins one of the 8 pool threads *for the lifetime of the tab*, silently starving LLM proxy traffic; periodic polling holds a thread only for the milliseconds a fragment takes to render, because gthread parks idle keepalive connections on its selector instead of on threads. Keep `<meta http-equiv="refresh">` only as a `<noscript>` fallback; a hand-rolled `setTimeout`+fetch loop is the viable zero-dependency alternative if even a vendored JS file is unwanted.

---

## Ground truth: the gthread thread budget (pinned source)

Facts verified against `gunicorn==26.1.0` (pinned in `requirements.txt`), `gunicorn/workers/gthread.py`, read locally from the PyPI wheel:

| Fact | Evidence |
|---|---|
| Request concurrency cap = `ThreadPoolExecutor(max_workers=cfg.threads)` → **8** | gthread.py:247; `gunicorn.conf.py:9` |
| Every in-flight request occupies one pool thread for the entire response-body iteration | `handle()` runs in a worker thread (:446-447); `handle_request` writes `respiter` item-by-item synchronously (:683-684) |
| **Idle keepalive connections do NOT hold a thread** — they are returned to the main selector loop and closed after `keepalive` timeout | design note :5-10; `finish_request` keepalive branch re-registers conn on the poller (:432-438); `murder_keepalived()` (:315-331) |
| New connections that send no bytes within 5 s are deferred back to the poller without consuming a thread | `DEFAULT_WORKER_DATA_TIMEOUT = 5.0` (:35-38); `_DEFER` path :454-458, :423-431 |
| Idle-keepalive slots bounded at `worker_connections − threads` (992 with defaults); overflow forces connection close | :220, :232-237, :675-676 |
| SIGTERM → stop accepting → drain existing connections up to `graceful_timeout` (30 s) → `tpool.shutdown(wait=False)`; **nothing force-cuts an in-flight response body** | `handle_exit` :249-254; `run()` :397-408, :410-416 |
| The main loop heartbeats (`notify()`) regardless of how deep the executor backlog is → **thread starvation is invisible to the arbiter**; queued requests simply hang | `run()` :378-388 |

Repo-local corollaries:

- `ShutdownState` / `guarded_stream` implement graceful stream draining, but `guarded_stream` has **exactly one production call site** — streamed `/v1/chat/completions` upstream bodies (`rotator.py:1317`; definition `rotator.py:1145`). Any *new* long-lived endpoint would not emit a terminal SSE event on shutdown today.
- Shutdown ordering invariant: `STREAM_DRAIN_WINDOW` < `GUNICORN_GRACEFUL_TIMEOUT` < container `stop_grace_period` (AGENTS.md gotchas; ADR 0001).

## Pattern comparison

### 1. htmx periodic polling — `hx-trigger="every Ns"`

**Mechanism.** `<div hx-get="/fragments/nodes" hx-trigger="every 5s">` makes htmx issue a normal `GET` every 5 s and swap the response into the element's innerHTML ([htmx `hx-trigger` docs, "Polling"](https://htmx.org/attributes/hx-trigger/)). Each poll is an ordinary short-lived HTTP request/response cycle — there is no persistent connection. Conditional forms (`every 5s [cond]`) exist but require `eval`.

**Flask/Jinja complexity.** Minimal. One fragment route rendering a partial template (`render_template("_nodes.html", ...)`), one full-page template including the same partial, one `<script src="/static/js/htmx.min.js">`. No new Python dependencies.

**Thread/connection cost (quantified).** A thread is held only while a poll is being served. Expected concurrent in-flight polls ≈ `tabs × (render_time / interval)` — at ~2 ms render and 5 s interval that is 0.0004/tab; even 50 tabs expect < 0.05 simultaneous requests. Worst case (all tabs fire simultaneously) momentarily needs `min(N, 8)` threads for a few ms each. Between polls the browser's keepalive connections sit parked on gthread's selector (zero threads, ≤ 992 idle fds). **N tabs ≈ 0 steady-state threads; proxy capacity untouched at any realistic N.**

**Graceful shutdown.** Nothing is held open. Fragment renders take milliseconds; a poll caught mid-drain either completes or dies with the process — no terminal-event machinery needed. No interaction with `ShutdownState`/drain windows.

**Proxy-restart failure modes.** A failed poll leaves the last-good fragment in the DOM (htmx does not swap non-2xx responses); the next successful tick self-heals. Optionally surface staleness via `hx-on::response-error`. Recovery is automatic with zero client code.

**Dependency weigh-up.** Vendored `htmx.min.js`: **51,238 B raw / 16,576 B gzipped** (htmx.org 2.0.10 dist, sizes measured locally). Served once from loopback, cached thereafter; no build step; no supply-chain surface beyond the one checked-in file.

### 2. SSE-driven fragment swaps

**Mechanism.** Browser opens one `EventSource`; server pushes `text/event-stream` chunks forever; a tiny JS listener writes received fragments into the DOM. `EventSource` holds "a persistent connection … [that] remains open until closed" ([MDN EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)).

**Flask/Jinja complexity.** Moderate. Generator response with `Content-Type: text/event-stream`, `stream_with_context()` so the request context survives past view return, headers fixed before first yield ([Flask streaming docs](https://flask.palletsprojects.com/en/stable/patterns/streaming/)); plus periodic heartbeat comments (SSE comment lines exist precisely "to prevent connections from timing out" — [MDN Using SSE](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)), plus client JS for applying payloads. More moving parts than polling, and the app already speaks upstream SSE only as a *client* — serving SSE adds a new server-side lifecycle.

**Thread/connection cost (quantified).** The generator is iterated synchronously inside `handle_request` (gthread.py:683-684), so **each connected tab pins exactly 1 of the 8 pool threads for the lifetime of the tab**: 3 open tabs leave 5 threads for LLM streams; 8 tabs = zero proxy capacity, and because the worker keeps heartbeating its main loop (gthread.py:379-380) the arbiter never notices — queued `/v1/chat/completions` requests hang silently. Dead tabs are only reaped when a write fails, i.e. at most one heartbeat interval later, so abandoned generators linger holding threads. On top of that, browsers cap SSE at **6 connections per origin across all tabs** on HTTP/1.1 (explicitly painful multi-tab; Won't-fix in Chrome/Firefox — [MDN](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)); our bind is plain HTTP/1.1 loopback.

**Graceful shutdown.** Open dashboard streams would hold their threads through the entire `graceful_timeout` drain window, competing with real LLM streams during drain. They are outside the current guard scope (`guarded_stream` wraps only chat-completion upstream bodies, `rotator.py:1317`), so they would be severed abruptly at process exit with no terminal event unless the drain machinery were extended first — new code plus renewed care for the `STREAM_DRAIN_WINDOW` < `GUNICORN_GRACEFUL_TIMEOUT` ordering.

**Proxy-restart failure modes.** The one clear win: `EventSource` auto-reconnects by default when the connection drops ([MDN Using SSE, "Closing event streams"](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)), tunable via the `retry:` field. But during longer outages every open tab retries in a loop, and the benefit is moot given polling also self-heals trivially.

**Verdict.** Push semantics buy sub-second freshness the dashboard does not need (node table, counters at 2–5 s granularity) and pay for it in permanently pinned threads, a drain-guard gap, and the browser 6-connection cap. Rejected for v1.

### 3a. Plain meta-refresh

**Mechanism.** `<meta http-equiv="refresh" content="10">` reloads the whole document every 10 s ([MDN `<meta>`, `http-equiv`](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta)). Zero JS, zero deps.

**Complexity / cost.** Trivial to build; same momentary thread profile as polling. But each tick re-renders and re-downloads the *whole* page, visibly flashes, and destroys scroll position, focus, and any half-typed form state. During a proxy restart the browser replaces the dashboard with its own error page — the operator loses the stale view entirely until recovery (self-healing afterwards).

**Verdict.** Fine as a `<noscript>` fallback (meta with `http-equiv` is permitted inside `<noscript>` in `<head>` — [MDN `<meta>` technical summary](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta)); poor primary UX for a console someone watches continuously.

### 3b. Plain JS `setInterval` + fetch

Same shape as htmx polling — short-lived GETs swapping fragments — with ~15–25 lines of inline JS instead of a library. Same near-zero thread cost and self-healing. Two hand-roll traps: use a `setTimeout` **chain**, not bare `setInterval` (prevents overlapping in-flight fetches piling up when renders slow down or the server stalls), and write your own stale-on-error handling. Viable if the vendored-JS weight is unacceptable; htmx earns its 17 KB gzipped the moment any interaction beyond refresh appears (pause toggle, per-node drill-down, `hx-boosted` links), and it standardizes the swap/error semantics.

## Summary matrix

| Criterion (N open tabs) | htmx polling | SSE swaps | meta-refresh | setInterval+fetch |
|---|---|---|---|---|
| Steady-state threads held | ~0 (ms bursts) | **N** (continuous) | ~0 (page-sized bursts) | ~0 (ms bursts) |
| Proxy capacity left (of 8) | 8 | **8 − N** | 8 | 8 |
| New Python deps / JS payload | 0 / 16.6 KB gz vendored | 0 / ~20 lines + endpoint work | 0 / 0 | 0 / ~20 lines inline |
| Graceful-shutdown interaction | none | holds threads through drain window; outside guard scope | negligible | none |
| Restart failure mode | stale fragment, self-heals | auto-reconnect (retry:) but reconnect churn | error page replaces UI | stale fragment, self-heals |
| UX during refresh | seamless fragment swap | live push (< 1 s latency) | full-page flash, state loss | seamless fragment swap |

## Recommendation for dashboard v1

1. Vendor `htmx.min.js` (2.x) into `static/` with an integrity-friendly versioned filename; serve the dashboard shell + `include`s of the same Jinja partials used by the fragment routes.
2. Refresh targets: node table and counters via `hx-get` + `hx-trigger="every 5s"` (counters may go 2 s if desired — still free); default `hx-swap="innerHTML"`.
3. Add `<noscript><meta http-equiv="refresh" content="15"></noscript>` in the head as the no-JS fallback.
4. Skip SSE for v1. Revisit only with a capacity plan (dedicated worker/thread budget, e.g. raised `GUNICORN_THREADS` or a separate lightweight worker class) *and* extended drain-guard coverage for the new endpoint.

## Pitfalls checklist

1. **Silent starvation:** under gthread, an exhausted thread pool produces hanging requests while the worker keeps heartbeating — monitoring must watch request latency, not worker liveness (gthread.py:378-388).
2. **Drain-guard scope:** `guarded_stream` covers only `/v1/chat/completions` upstream bodies (`rotator.py:1317`). Any future long-lived endpoint (SSE or WebSocket-ish) needs its own terminal-event story before it ships.
3. **Ordering invariant:** anything that holds threads during shutdown must respect `STREAM_DRAIN_WINDOW` < `GUNICORN_GRACEFUL_TIMEOUT` (30 s) < `stop_grace_period`.
4. **Browser 6-connection cap** (HTTP/1.1, per origin, shared across tabs): favors short-lived polls over persistent streams; relevant again if the dashboard ever proxies API calls from the same origin.
5. **Stale-on-error is the default:** htmx won't swap non-2xx poll responses; wire `hx-on::response-error` (or equivalent) if staleness must be visible to the operator.
6. **WSGI disconnect detection is lazy:** a generator notices client disconnects only on its next write — another reason per-tab SSE heartbeats and reaping get fiddly while polling never faces it.
7. **Hand-rolled JS:** prefer a `setTimeout` chain over `setInterval` to prevent overlapping refreshes under load.
8. **meta-refresh side effects:** full reload loses scroll/focus/form input and flashes; also an accessibility smell for short intervals — restrict it to the `<noscript>` fallback.
9. **Keepalive fd budget** is `worker_connections − threads` (992 by default) — a non-issue here, but it is the resource polling actually consumes; don't shrink `worker_connections` blindly.
10. **Interval sizing:** keep poll interval ≥ 10× worst-case fragment render time; add small per-panel jitter if many panels ever share one interval (cosmetic at this scale).
11. **htmx conditional triggers need `eval`** (`every 5s [cond]`); avoid filters unless enabling them deliberately.

## Sources

- gunicorn 26.1.0 `workers/gthread.py` (read from the pinned PyPI wheel): thread pool :247, keepalive-parking design :5-10 & :432-444, `_DEFER` :35-38/:423-431/:454-458, `max_keepalived` :220, SIGTERM drain :397-416, heartbeat :378-388
- Repo: `gunicorn.conf.py:8-14` (workers/threads/graceful timeout); `rotator.py:1139-1171` (terminal SSE event, `guarded_stream`), `rotator.py:1199-1228` (`install_shutdown_handlers`), `rotator.py:1317` (sole guard call site); AGENTS.md gotchas; `docs/adr/0001-graceful-stream-draining.md`
- htmx — [`hx-trigger` attribute (Polling)](https://htmx.org/attributes/hx-trigger/) — accessed 2026-08-23
- htmx dist sizes: `dist/htmx.min.js` @ 2.0.10 via unpkg, raw/gzip measured locally (51,238 B / 16,576 B)
- MDN — [Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events) (auto-reconnect, `retry:`, keepalive comments, 6-connection HTTP/1.1 warning) — accessed 2026-08-23
- MDN — [EventSource](https://developer.mozilla.org/en-US/docs/Web/API/EventSource) (persistent connection, ready states, error event) — accessed 2026-08-23
- MDN — [`<meta>` element](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/meta) (`http-equiv="refresh"`, permitted inside `<noscript>`) — accessed 2026-08-23
- Flask — [Streaming Contents](https://flask.palletsprojects.com/en/stable/patterns/streaming/) (generator responses, `stream_with_context`, headers-before-body) — accessed 2026-08-23
