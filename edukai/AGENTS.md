# EDUKAI — Agent Operating Manual

This folder is your long-term understanding. Read it before you act; write to it when you learn. It is plain text so any agent can pick up where the last one left off.

## Read protocol

1. **Session start**: read `SYLLABUS.md` — every domain and its state. Then check for maintenance debt (`git status`: dirty or ahead of origin; stale overview freshness lines) and, if any, run the heartbeat checklist as a *recovery pass* before starting user work. Sessions die abruptly; recovery at start is what makes upkeep inevitable.
2. **Before working on a topic**: read that domain's `curriculum/<domain>/overview.md`. Drill into `lessons/*.md` for evidence and caveats.
3. **Before relying on a lesson**: check its `verified:` date. If it is older than ~30 days and stakes are high (you are about to act on it), re-verify against reality first.
4. Overviews are summaries; lessons are ground truth. When they disagree, trust the lesson and fix the overview.

## Write protocol

Capture raw while working; distill before leaving.

| Situation | Action |
|---|---|
| You discover a durable fact | New lesson: `curriculum/<domain>/lessons/YYYY-MM-DD-slug.md` from `templates/lesson.md` |
| User corrects you or says "remember this" | Write the lesson immediately, mid-session |
| Context worth keeping but not yet understood | Append to `notebook/YYYY-MM-DD.md` |
| A fact changed | New lesson with `supersedes:` set; mark the old one `status: superseded`, `superseded_by:` pointing forward |
| Session end / heartbeat — trigger when the notebook passes ~20 lines, a topic switches, or the user wraps up | Distill notebook entries into lessons; regenerate touched `overview.md`; run the verification sweep and Audit checks; update `SYLLABUS.md`; commit if dirty — staging only paths inside this store; push only if this store is a standalone repo (rejected push: pull --rebase, resolve, retry once) |

### Rules

- **One lesson = one claim.** If the title needs "and", split it.
- **Never delete.** Supersede instead. History is knowledge.
- **Set `confidence` honestly**: `tested` (you reproduced it), `observed` (you saw/read it), `inferred` (you deduced it — flag for verification).
- **Cite evidence.** Every lesson names where the knowledge came from.
- **Use `templates/`.** Don't invent formats.
- **Surface `inferred` lessons.** They belong in their domain overview's Open Questions until verification upgrades them.

## Load discipline

Every edit pays three rents: tokens (bytes get read), cache (one changed byte in an always-loaded file invalidates cached prompts), and attention (bloat dilutes every rule). Check all three before writing.

### Token aware

- The line test: would removing it cause an agent to make a mistake it cannot recover from by reading the code? If no, cut it.
- Budgets: SYLLABUS ≈ one row per domain; overview ≤ 60 lines; lesson ≤ 40 lines including frontmatter. Over budget means split or distill, not squeeze.
- Never duplicate across layers. Overviews point to lessons; they don't restate them.

### Cache aware

- `AGENTS.md`, `README.md`, and `SYLLABUS.md` are the hot path — loaded into prompts every session, cache-invalidated on any byte change. Change them rarely and deliberately; bundle edits.
- Volatile data lives on the cold path (domain files), never the hot path.
- Appends go at tails. Never reorder rows, sections, or fields. Supersession touches exactly one lesson file.

### Context aware

- Loading is layered and each layer stands alone: SYLLABUS → overview → lessons. Every layer must answer "open the next layer?" without being opened further.
- Frontmatter is the routing layer: title, confidence, and tags must suffice for triage.

## Domains

A domain is an area of study: a codebase, a person, a toolchain, a cluster of user preferences. Create one when a topic accumulates 3+ lessons' worth of knowledge or the user treats it as a standing concern. Follow `templates/domain.md`.

When a domain stops mattering, move its folder to `archives/` and mark it `graduated` in `SYLLABUS.md`. An active domain untouched for ~90 days becomes a graduation *candidate* — propose it at heartbeat; move it only after a review pass confirms.

## Review

Re-verification is part of learning. When you re-check a lesson — because it was stale, contradicted, or load-bearing — append a dated line to that domain's `review.md` and update the lesson's `verified:` date.

### Audit

Heartbeat drift checks — read-only grep/line counts; fix what fails:

- Every `supersedes:` / `superseded_by:` pointer resolves to an existing lesson file.
- SYLLABUS rows and `curriculum/*/` folders match exactly — no orphans in either direction.
- Budgets hold: lesson ≤ ~40 lines, overview ≤ ~60 lines.
- Lesson frontmatter carries title/domain/created/verified/confidence/status.
- `skeleton/` matches the root spec files (`diff -r` catches copy-drift).
- `ADOPTED.md` entries point at real project stores — spot-check occasionally.

### Verification sweep

At heartbeats and recovery passes, read each overview's freshness line. An active domain whose oldest lesson is older than ~30 days owes re-verification: re-check its load-bearing lessons against reality, note debts in that overview's Open Questions, and log checks in `review.md`.

## Recall at scale

Grep is fine while edukai is small. Once it grows and `qmd` is available, prefer:

```bash
qmd index .
qmd query "<question>"
```

## The one rule

If future-you should know it, write it down now. Unwritten knowledge does not survive the session.
