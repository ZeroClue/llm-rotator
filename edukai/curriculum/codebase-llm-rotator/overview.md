# Codebase: llm-rotator

How this repository is wired at runtime: which pieces are constructed where, which seams exist for extension and testing, and which non-obvious structural traps await anyone adding code. The host repo's AGENTS.md carries operational gotchas in compressed form; this domain records verified structural knowledge with cited evidence at lesson depth.

The app is built lazily and exactly once from the environment: importing `rotator` runs nothing (`NODE_POOL`/`settings` stay `None`), and `get_app()` — also reachable as module attribute `.app` via module-level `__getattr__` — performs the single `create_app()` construction. Everything downstream receives services through that one function or through providers it closes over, never through re-imports of the module.

## Current understanding

- Module/app lifecycle is lazy and single-build: bare import is side-effect-free; global state exists only after `get_app()`/`create_app()` runs — see `lessons/2026-08-24-views-get-state-by-injection-not-import.md`
- Views and dashboard modules take injected context providers instead of importing rotator, for a verified runtime reason — same lesson as above.

## Open questions

- How `FailoverTransport`'s constructor seams (session/sleeper/rng injection) should map onto transports beyond requests/curl_cffi — candidate for a second lesson once next touched.

Lessons: 1 · oldest 2026-08-24 · newest 2026-08-24 — refresh on every regeneration; old dates mean re-verify before trusting.
