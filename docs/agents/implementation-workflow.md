# Implementation workflow

How changes land in this repo: branch → commit → PR → review → verify → merge
→ close. Each step ends in a checkable **gate**; a failed gate stops the
workflow until it passes.

Applies to every change that lands on `main`, including docs-only changes — with one
**fast lane**: commits whose changed paths are exclusively under `edukai/` (the education
store) may land directly on `main` with no ticket, branch, PR, or pre-merge review; CI
still runs post-hoc, and store hygiene is enforced by the heartbeat Audit checklist in
`edukai/AGENTS.md`. Mixing any path outside `edukai/` into a commit voids the exemption
(`.gitignore`/`.dockerignore` edits made for the store's benefit included). README,
`docs/`, and ADRs are project-facing claims and never qualify.
Where a generic skill default says "commit your work to the current branch",
this workflow overrides it.

1. **Branch** — One ticket per branch, never work directly on `main` (sole exception:
   the `edukai/` fast lane above):
   `git checkout -b implement-<slug>`. If working-tree changes span two
   tickets, split them onto separate branches before the first commit, not
   after review.
2. **Commit and push each commit** — Clear conventional messages (`feat:`,
   `fix:`, `docs:`). Push immediately after every commit. A local-only commit
   is silently dropped by squash merges. **Adding new files?** Audit the three
   silent manifests in the same commit — `.gitignore` (skipped by
   `git add -A`), `.dockerignore` (excluded from the build context), and the
   Dockerfile COPY whitelist (absent from the image). All three fail without
   a warning; see the AGENTS.md gotcha.
3. **PR** — Open with `gh pr create`. Every `Closes #N` must name the issue
   whose acceptance criteria this diff actually implements (read the issue
   body; roadmap numbering and issue numbers diverge). Apply `ready-for-agent`
   if the PR is agent-grabbable (vocabulary: `docs/agents/triage-labels.md`).
4. **Code review** — Run `/code-review` against `main` (the merge-base). Fix
   findings on the same branch; commit AND push the fixes before merging.
   Review without landed fixes is a no-op.
5. **Verify remote matches the intended diff** —
   `gh api repos/$(gh repo view --json nameWithOwner -q .nameWithOwner)/pulls/<N>/files`
   must list every file in the full change set (check after every push).
   Merging an under-pushed PR lands only what GitHub has; the rest is lost
   until someone notices.
6. **Live verification** — For user-facing behavior, deploy locally
   (`docker build` + `docker-compose up`) and exercise the feature
   (playwright-cli for UI, curl for APIs) before merging. Unit tests green is
   necessary, not sufficient. Whether a change is user-facing follows from the
   issue's acceptance criteria, not the diff's size.
7. **CI green before merge** — `gh run list --branch <branch>` (or the PR
   checks) must show the latest run passing. Merging red hides the failure on
   main and pins every later bisect (PRs #65-#68 merged red; the container
   smoke test had been failing for days before anyone looked). If CI is red,
   the PR is not done.
8. **Merge** — Once gates 4–7 pass:
   `gh pr merge --squash --delete-branch`.
9. **Close tickets** — Issues linked with `Closes #N` auto-close on merge;
   close stragglers per `docs/agents/issue-tracker.md`. An issue counts as
   done only when its acceptance criteria are verified in code on `main`
   (read/grep/build/test). Partial work: keep the issue open, note progress in
   a comment, split remaining criteria into focused issues.
10. **Update CONTEXT.md** — If domain terms or decisions changed during
    implementation, update `CONTEXT.md` and add an ADR if the decision is hard
    to reverse. Gate: every domain term in the merged diff matches the
    `CONTEXT.md` glossary; every hard-to-reverse decision has an ADR.
