# Implementation workflow

How changes land in this repo: branch → commit → PR → review → verify → merge
→ close. Each step ends in a checkable **gate**; a failed gate stops the
workflow until it passes.

Applies to every change that lands on `main`, including docs-only changes.
Where a generic skill default says "commit your work to the current branch",
this workflow overrides it.

1. **Branch** — One ticket per branch, never work directly on `main`:
   `git checkout -b implement-<slug>`. If working-tree changes span two
   tickets, split them onto separate branches before the first commit, not
   after review.
2. **Commit and push each commit** — Clear conventional messages (`feat:`,
   `fix:`, `docs:`). Push immediately after every commit. A local-only commit
   is silently dropped by squash merges.
3. **PR** — Open with `gh pr create`. Every `Closes #N` must name the issue
   whose acceptance criteria this diff actually implements (read the issue
   body; roadmap numbering and issue numbers diverge). Apply `ready-for-agent`
   if the PR is agent-grabbable.
4. **Code review** — Run `/code-review` against `main` (the merge-base). Fix
   findings on the same branch; commit AND push the fixes before merging.
   Review without landed fixes is a no-op.
5. **Verify remote matches the intended diff** —
   `gh api repos/{owner}/{repo}/pulls/<N>/files` must list every file in the
   full change set (check after every push). Merging an under-pushed PR lands
   only what GitHub has; the rest is lost until someone notices.
6. **Live verification** — For user-facing behavior, deploy locally
   (`docker build` + `docker-compose up`) and exercise the feature
   (playwright-cli for UI, curl for APIs) before merging. Unit tests green is
   necessary, not sufficient. Internal refactors with no behavior change may
   skip this gate; say why in the PR body.
7. **Merge** — Once gates 4–6 pass:
   `gh pr merge --squash --delete-branch`.
8. **Close tickets** — Issues linked with `Closes #N` auto-close on merge;
   close stragglers with
   `gh issue close <N> --comment "Done in #<PR>"`. An issue counts as done
   only when its acceptance criteria are verified in code on `main`
   (read/grep/build/test), never because a PR or commit message mentioned it.
   Partial work: keep the issue open, note progress in a comment, split
   remaining criteria into focused issues.
9. **Update CONTEXT.md** — If domain terms or decisions changed during
   implementation, update `CONTEXT.md` and add an ADR if the decision is hard
   to reverse.
