---
title: Views receive state by injection, never by importing rotator
domain: codebase-llm-rotator
created: 2026-08-24
verified: 2026-08-24
confidence: tested
status: active
supersedes:
superseded_by:
tags: [architecture, flask, modules]
---

When the proxy runs as `python rotator.py`, the live application is the module `__main__`. A view or dashboard module that does `import rotator` re-executes rotator.py as a second copy whose globals (`NODE_POOL`, `health_ledger`, `telemetry`, …) are still `None` — producing 500s that only appear when running as a subprocess and never in-process (pytest imports rotator normally, so tests stay green). Cross-module state therefore flows through closures/providers built inside `create_app()` — e.g. `_admin_context()` passed to `register_admin_dashboard(application, context_provider=_admin_context)` — and new views must accept injected context the same way.

## Evidence

- rotator.py:602-606 — `create_app` docstring: "The only place anything is constructed; importing this module runs nothing."
- rotator.py:692,716 — `_admin_context` closure registered as `context_provider`; admin_dashboard.py:58-61 — "injected so this module never imports rotator".
- Mechanism reproduced 2026-08-24 with one file executed under two names: the `__main__` copy had state built, the by-name import saw a fresh unbuilt copy, namespaces distinct.
- `.venv/bin/python -c "import rotator"` (2026-08-24): import succeeds inertly — `NODE_POOL`/`settings` are `None` until `get_app()` builds them.

## Caveats

- Under gunicorn (`rotator:app`) the named module happens to be the live one, so a stray `import rotator` can appear to work there. The invariant stands so that `python rotator.py`, gunicorn, and tests behave identically.
