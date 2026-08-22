# Terminal-event stream draining on shutdown

On SIGTERM, in-flight SSE completions used to be cut hard: at gunicorn's
`graceful_timeout` (truncated body, no marker), or instantly under the bare
werkzeug dev server. We decided on **finish-first draining**: streams keep
pumping after the shutdown signal and end naturally if they finish within a
drain window (`STREAM_DRAIN_WINDOW`, default 20s, kept below
`GUNICORN_GRACEFUL_TIMEOUT`); past the window each stream is ended by the
proxy with a well-formed terminal SSE event (OpenAI-style error object +
`data: [DONE]`) so clients see an explicit failure, never a silent truncation.

## Considered options

- **Fail-fast** (cut every in-flight completion immediately on signal):
  rejected — discards completions that were milliseconds from done and bills
  for tokens the client never receives.
- **Config/docs only** (raise `GUNICORN_GRACEFUL_TIMEOUT`): rejected — the
  cut is still hard when it happens, and the dev-server path has no window
  at all.
- **Bare `[DONE]` without an error object**: rejected — clients would parse a
  truncated completion as success.

## Consequences

- The drain check runs between chunks: an upstream stalled mid-read still
  hits gunicorn's hard kill; the window accelerates clean termination but
  cannot preempt a blocked socket read.
- The guard lives in the view layer (rotator.py), not failover.py: the
  transport knows nothing about SSE framing.
- One global deadline is fixed at arm time; streams starting after arming get
  the terminal event immediately.

Design record: issue #30 (grill session 2026-08-22).
