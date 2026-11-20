# Rebase Workload Review: compare_YWT

- Reviewer: Codex
- Review ID: `rebase-compare-ywt-workload`
- Date: 2026-11-16
- Scope: read-only comparison of `cms_dev` and `compare_YWT`
- Common ancestor: `e84d8717c2226ca5ae275f80cc7795c326e4c417`
- Working tree: two untracked personal design documents; no staged or modified tracked files
- Business code modified during review: none

## Conclusion

This is a high-risk, medium-to-large rebase. It is not a documentation-only
sync and should not be handled as a single blind rebase followed by one broad
test run.

The recommended direction is to stay on `cms_dev` and run `git rebase
compare_YWT`. In that direction, the 22 commits already present on
`compare_YWT` become the new base and the 5 commits unique to `cms_dev` are
replayed. Replaying `compare_YWT` onto `cms_dev` would instead replay 22
commits and has a materially larger conflict surface.

## Evidence

| Measure | Result |
| --- | --- |
| `cms_dev`-only commits | 5 |
| `compare_YWT`-only commits | 22 |
| `compare_YWT` changed paths from common ancestor | 145 |
| `compare_YWT` line changes | about 11,151 additions and 733 deletions |
| Paths changed on both branches | 14 |
| Feature map validation | PASS: 24 domains, 424 referenced paths |
| `git diff --check` for both ranges | PASS |
| Real rebase | PASS: `cms_dev` rebased onto `compare_YWT`; 5 commits replayed |

## Main Conflict Surface

The 14 paths changed on both branches are:

- `backend/app/agent/durable_planner.py`
- `backend/app/agent/planner.py`
- `backend/app/freecad/operation_generator.py`
- `backend/app/llm.py`
- `backend/app/services/durable_submission.py`
- `backend/app/validation/requirement_binding.py`
- `backend/app/workflows/agent_v2.py`
- `backend/app/workflows/handlers/native_generation.py`
- `backend/app/workflows/handlers/planning.py`
- `backend/tests/test_acceptance_validation_repairs.py`
- `backend/tests/test_design_source_context.py`
- `backend/tests/test_durable_agent_planner.py`
- `backend/tests/test_engineering_acceptance_contract.py`
- `backend/tests/test_freecad_operation_generator.py`

The highest-risk files are `durable_planner.py`, `planner.py`, and
`requirement_binding.py`. The current branch adds verified CAD planning,
acceptance contracts, and live evaluation hooks. `compare_YWT` adds stricter
source semantics, numeric meaning, primitive-shape binding, engineering
qualifiers, and model-owned interpretation. Their behavior must be composed;
choosing one side wholesale would discard a feature contract.

The workflow files are also contract boundaries. They connect planning,
candidate execution, native validation, durable submission, and report
observation. A conflict resolution that only makes imports pass can still
break task admission, worker activity inputs, terminal states, or live report
events.

## Scope That Should Usually Apply Cleanly

Most of the current live-evaluation implementation is in paths only added or
changed by `cms_dev`, including `backend/app/evaluation/` and
`backend/benchmark/cad_tests/`. Most of the native-reference and intake
semantics implementation is in paths only added or changed by `compare_YWT`.
These areas still require regression testing, but they are less likely to
require manual line-by-line conflict resolution than the 14 shared paths.

`compare_YWT` also changes the FreeCAD sandbox, native oracle, deployment
image inputs, CI contracts, and a large native/e2e test set. Those changes are
operationally significant even when Git can apply them automatically.

## Required Execution Order

1. Preserve the two untracked personal documents and confirm the working tree
   is unchanged before starting.
2. Rebase `cms_dev` onto `compare_YWT`, resolving the 14 shared paths by
   contract rather than accepting either side wholesale.
3. After planner and requirement-binding conflicts, run focused parser,
   acceptance, and planner tests before resolving the next workflow group.
4. After workflow conflicts, run worker wiring, durable submission, task
   admission, and terminal-state tests.
5. Validate the FreeCAD/Podman native runtime and native oracle with the local
   environment before claiming the rebase is usable.
6. Run the live evaluation tests and the normal generation, modification, and
   historical-restore regression flow.
7. Run frontend type checking, unit tests, build, `git diff --check`, feature
   map validation, encoding scan, and fake-implementation review.

## Post-Rebase Verification

The rebase completed with five rewritten commits:

- `44e23b9` `fix: preserve verified CAD planning and acceptance contracts`
- `f825a29` `fix: unblock historical restore confirmation`
- `442f4f7` `feat: add live CAD evaluation and report comparison`
- `6ee19c4` `feat: add live evaluation complexity metrics`
- `63fcbe3` `feat: summarize live report comparisons`

The following checks passed after the rebase:

- Live evaluation and report tests: `107 passed`
- Requirement semantics, planner, restore, and FreeCAD unit tests: `311 passed`
- Worker wiring, durable API, and visual validation tests: `57 passed`
- Frontend TypeScript build and Vite production build
- Frontend ESLint
- `git diff --check`
- Feature map validation: `24` domains, `433` referenced paths

One shared legacy suite remains red and requires a separate compatibility fix:
`tests/test_design_source_context.py` reports `60 passed, 6 failed`. Four
failures retain the old expectation that cube dimensions are three
`overall_dimension` checks, while the rebased requirement contract uses
`cube_shape` plus `cube_edge`. Two failures expect the old source-context
error path instead of the newer pre-dispatch `engineering_verification_unavailable`
diagnostic. No source workaround was added during the rebase to hide these
failures.

The local branch now has `compare_YWT` as its base and is not pushed. It is
`27` commits ahead and `5` commits behind `origin/cms_dev`, which is expected
after rewriting the local branch history. The local safety branch
`rebase-backup-cms-dev` still points to the pre-rebase `cms_dev`.

## Review Limitations

`git merge-tree --write-tree` could not create temporary Git objects before
the rebase because the local `.git/objects` directory denied write access. The
actual rebase was then completed after Git ref write permission was granted.
No remote push was performed. Native PostgreSQL, Temporal, Podman, and real
FreeCAD acceptance were not run in this verification pass.

The risk scanner reported a lead at
`backend/sandbox/feature_verification.py:796`, where an exception is cached
and re-raised. This is not by itself evidence of a fake implementation, but it
should be reviewed when the native verification path is tested.

## Recommended Next Action

Keep the local rebase result for manual review. Before pushing, run the real
Podman/Temporal/PostgreSQL workflow and the normal generate, modify, restore,
and modify-again acceptance flow. Push only after those environment-dependent
checks pass and after confirming the remote branch rewrite is intentional.
