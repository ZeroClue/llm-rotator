---
title: Every change to main rides the gated workflow — store commits included
domain: codebase-llm-rotator
created: 2026-08-24
verified: 2026-08-24
confidence: tested
status: superseded
supersedes:
superseded_by: lessons/2026-08-24-edukai-commits-take-the-fast-lane.md
tags: [process, workflow, git]
---

`docs/agents/implementation-workflow.md` gates ALL landings on `main` — ticket → branch → push-each-commit → PR (`Closes #N`) → review → CI green → squash merge — explicitly "including docs-only changes". An edukai/ heartbeat committed straight onto local `main` is therefore stranded off-workflow: squash merges only take pushed branches, so the work must be surgically re-parented (`git checkout -b implement-<slug>` carrying HEAD, then `git branch -f main <origin-sha>`) before it can travel. Doing it right from the start costs one issue + PR per heartbeat.

## Evidence

- docs/agents/implementation-workflow.md:7 — "Applies to every change that lands on `main`, including docs-only changes"; :11 — "One ticket per branch, never work directly on `main`".
- Lived 2026-08-24: adoption commit dbe7500 landed directly on local main; remediated mid-flight by re-parenting onto `implement-adopt-edukai-store` and resetting main to origin before PR #72 could open.

## Caveats

- One-issue-plus-PR per heartbeat is real overhead for store upkeep; whether heartbeats earn a fast lane is an open policy question for Armin. Until decided, treat the ceremony as part of the heartbeat.
