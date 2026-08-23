---
title: edukai/-only commits take the fast lane; everything else stays gated
domain: codebase-llm-rotator
created: 2026-08-24
verified: 2026-08-24
confidence: tested
status: active
supersedes: lessons/2026-08-24-main-is-gated-even-for-store-commits.md
superseded_by:
tags: [process, workflow, git]
---

As of 2026-08-24, commits whose changed paths are exclusively under `edukai/` land directly on `main` — no ticket, branch, PR, or pre-merge review; CI still runs post-hoc. Any commit mixing paths outside `edukai/` voids the exemption and takes the full gated workflow. Rationale: the gate protects runtime behavior, which the store cannot affect (excluded from the Docker build context, never COPYed, imported by nothing, read by no test), while a per-heartbeat issue+PR+CI+review cycle was the likelier failure mode — store upkeep quietly stopping. Store hygiene is enforced by the heartbeat Audit checklist instead of code review.

## Evidence

- docs/agents/implementation-workflow.md preamble (as amended) — the carve-out paragraph; gate 1 names the fast lane as sole exception.
- Decision conversation with Armin, 2026-08-24 ("the edukai commits should follow a fast lane").
- Cost data: adoption PRs #72/#74 each spent an issue + PR + CI + two review sub-agents on docs-only diffs that CI validated trivially.

## Caveats

- The lane is path-scoped, not intent-scoped: `.gitignore`/`.dockerignore` edits for the store's benefit still ride the full lane.
- README/`docs/`/ADR edits are project-facing claims that can rot and mislead agents (see the repo's README-divergence gotcha) and never qualify.
