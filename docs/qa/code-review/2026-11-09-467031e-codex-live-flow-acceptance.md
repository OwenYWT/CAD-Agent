# Live CAD Flow Acceptance Review

- Review ID: `codex-live-flow-acceptance`
- Review date: 2026-11-09
- Reviewed head: `467031e`
- Branch: `cms_dev`
- Working tree: clean tracked tree; two pre-existing untracked design documents
- Scope: live developer evaluation reports for create, part modification, history restore, and post-restore part modification
- Business code modified by this review: none

## Conclusion

`PASS`

The completed create -> part modification -> history restore flow produced coherent live
reports from the real execution path. The reports contain authoritative task, workflow,
revision, candidate, Change Set, validation, tool-call, and terminal-state evidence.

The post-restore modification and submission also completed successfully. This closes the
Task 6 acceptance sequence without changing ordinary business-state authority.

## Evidence

| Flow | Report | Result |
|---|---|---|
| Create | `live_live-e324e5f1-965b-4d39-b0f4-fb43e3ac286e.json` | finished; geometry and DFM passed; revision committed |
| Part modification | `live_live-eeee6adf-cebb-45a9-b6d9-e4dfeed2b97e.json` | finished; one real geometry failure, one repair, final geometry and DFM passed |
| History restore | `live_live-fb4cb047-15c3-4e10-8e0a-2e0700afac72.json` | finished; restore source, base, candidate, Change Set, and committed revision linked |
| Post-restore modification | `live_live-d7d0fb1e-ceed-4143-ab25-543d0ab1f1b5.json` | finished; restored revision used as base, one real repair, final geometry and DFM passed, revision committed |
| Latest complete flow | `live_live-2ba2042e-c7d5-4d7b-9d31-404379153902.json` | finished; create -> modify -> restore -> modify chain verified, final modification committed |

The revision chain is coherent:

```text
create:     base 5fd812db -> candidate 19a74c87
modify:     base 19a74c87 -> candidate 42e5209b
restore:    base 42e5209b, source 19a74c87 -> candidate d31e4a91
post-restore modify:
            base d31e4a91 -> candidate f68e4230

Latest complete flow:
            create candidate f6ba0f4c
            modify candidate 05eb33c2
            restore source f6ba0f4c -> candidate 2427e8a8
            modify base 2427e8a8 -> candidate 82359ca3

This latest chain is the current baseline seed. The local live-report directory currently
contains these four reports as the only retained complete chain, so a second independent
same-class sample is still required before calculating a regression delta. No comparison
result is inferred from absent or deleted reports.

That second sample is now available:

```text
restore report:    live_live-f89d1b9d-9566-4302-9aaa-45fba4cd9e8d.json
post-restore edit: live_live-529dc9c7-360e-4fbd-9494-c0b436d15488.json
```
```

The latest restore report has no recorded evaluator errors, 9 workflow activity calls,
13 timeline events, geometry passed, DFM passed, Change Set accepted, revision committed,
and terminal status `finished`.

The post-restore modification report has no recorded evaluator errors, 17 tool calls,
one failed first geometry validation, one real `agent_v2.repair_operations` call, a passing
second geometry validation, passing DFM validation, accepted Change Set, committed revision,
and terminal status `finished`. Its `base_revision_id` is the restored candidate revision
`d31e4a91-41aa-456f-99ce-ba0c51041120`.

The latest complete-flow report also has no recorded evaluator errors and ends in
`business_status=committed`. Its final modification uses restored candidate
`2427e8a8-d7b5-4196-bcb4-d4c0ac5f863f` as `base_revision_id`, creates candidate
`82359ca3-4582-49be-97e5-970ee5dc08dd`, records Change Set
`272503b6-ce3a-425f-b6f7-f059799cde81`, performs one real repair after the first geometry
failure, and passes the final geometry and DFM validations.

## Checks

| Check | Result |
|---|---|
| Live and CAD evaluation tests | `139 passed` |
| Feature map validation | `PASS` |
| `git diff --check` | `PASS` |
| UTF-8 and JSON validation for 23 live reports | `PASS` |
| Live report secret/encoding marker scan | `PASS` |
| Same-class live comparison commands | `PASS`; differences reported as data, not command errors |
| Existing Pydantic warning | Known warning for `model_kind`; no new failure |

## Baseline limitation

The current create, modification, and restore comparison pairs have no added or removed
calls in the create and restore cases, but input/output hashes differ across executions.
The modification pair also differs in repair-path structure. These are observations, not
regressions: the compared executions do not share an identical request and the repair path
is data-dependent. A stable regression baseline requires repeated same-class runs with a
fixed prompt, starting revision, model configuration, and case identity.

## Next Phase

The lifecycle acceptance is complete. The next phase is to establish a stable same-class
baseline using repeated fixed cases before adding complexity stratification, repair-gain,
or resource-cost metrics. The current live reports remain ignored by Git and must not be
added to the commit.

## Preliminary Baseline

The current real reports provide usable baseline candidates, but they are not yet a strict
regression gate because the executions do not all share one fixed case identity and one
starting revision.

| Flow class | Candidate reports | Current evidence | Baseline state |
|---|---|---|---|
| Create | `e324e5f1`, `bccd0a33` | 11 calls each; geometry and DFM passed | usable provisional pair |
| Part modification | `eeee6adf`, `d7d0fb1e` | 17 calls each; both contain one real repair and final validation passed | usable diagnostic pair; prompts differ |
| History restore | `fb4cb047`, `837fb41e` | 9 calls each; structure and order match; geometry and DFM passed | strongest provisional pair |
| Post-restore modification | `d7d0fb1e` | restored candidate used as base; final commit passed | single sample; needs one repeat |

The same-class comparison results are diagnostic only:

- Create and restore have no added or removed calls and no reorder in the selected pairs.
- Input/output hashes differ because runtime IDs, starting revisions, and execution payloads
  differ; these differences are recorded separately and are not treated as a regression.
- Modification sequences are data-dependent because repair can introduce additional CAD
  tool calls and a second validation pass. A structural mismatch alone is not a failure.

Before enabling a hard baseline gate, collect at least two repeated runs per class using
the same prompt, model configuration, starting revision shape, and declared case identity.
For the post-restore class, collect one additional successful restore-then-modify-then-commit
run first. Compare call structure, repair count, validation outcome, and duration range;
do not gate on raw runtime duration from a single run.

### Current Seed

The retained complete chain is:

```text
create:             03dbe267 -> f6ba0f4c
modify:             f6ba0f4c -> 05eb33c2
restore:            source f6ba0f4c, base 05eb33c2 -> 2427e8a8
post-restore edit:  2427e8a8 -> 82359ca3
```

All four reports finished with `business_status=committed`. The two modification reports
each recorded a first geometry failure followed by one real repair and a passing final
geometry/DFM result. This is a valid seed, but not a statistical baseline by itself.

### First Same-Class Comparison

The restore comparison used `live_live-231184bf-2585-46ea-bc35-658a8b19e349.json` and
`live_live-f89d1b9d-9566-4302-9aaa-45fba4cd9e8d.json`:

- 9 calls in both runs;
- no added or removed calls;
- no reorder;
- identical structural signature;
- no status changes or retries;
- geometry and DFM passed in both reports.

The post-restore modification comparison used
`live_live-2ba2042e-c7d5-4d7b-9d31-404379153902.json` and
`live_live-529dc9c7-360e-4fbd-9494-c0b436d15488.json`:

- 15 calls in both runs;
- identical structural signature and no reorder;
- one real repair call in each run;
- one initial geometry failure followed by a passing geometry validation in each run;
- DFM passed, Change Set was accepted, and the revision was committed in both runs.

Input/output hash changes are expected because the runs used different revision, artifact,
task, and candidate identities. They are retained as diagnostic data and are not treated
as a structural regression.

## Phase 5 Repair Gain Increment

The first Phase 5 increment is implemented in `backend/app/evaluation/repair_gain.py` and
is attached by `FileEvaluationStore.finish` in `backend/app/evaluation/store.py`.

The new field is derived only from authoritative geometry evidence already present in the
live report. It records repair count, measured repair duration, before/after evidence,
changed checks, regressions, and remaining failures. A repair without comparable geometry
evidence is recorded as `unknown`; a report without a repair is `not_applicable`.

Validation evidence:

- Store tests: `6 passed`.
- Live/CAD evaluation regression suite: `141 passed, 1 warning`.
- Python `compileall`: passed.
- Real report derivation for both 60 cm modification runs: `status=measured`, one repair,
  width error reduced from approximately 500 mm to 0 mm, no remaining failures.

Existing generated reports were not rewritten. The `repair_gain` field will appear in the
next report produced by a real observe-mode workflow; historical reports remain immutable
evidence of the execution that produced them.
