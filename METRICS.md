# Metrics Handoff

## Basic result metrics

`summary.json`, `generation_summary.json`, and `reference_summary.json` contain:

| Field | Meaning |
| --- | --- |
| `total` | Number of samples |
| `success` | Successful samples |
| `failed` | Failed samples |
| `success_rate` | `success / total` |
| `avg_execution_time_ms` | Average backend-reported or wall-clock time |
| `error_types` | Failure count grouped by error type |

## Per-sample fields

Each JSONL row may contain:

| Field | Meaning |
| --- | --- |
| `dataset` | CADPrompt or CAD-Coder |
| `sample_id` | Dataset sample ID |
| `mode` | `reference` or `generate` |
| `success` | Whether generation/execution succeeded |
| `request_id` | Backend request or workflow ID |
| `workflow_run_id` | Durable workflow ID |
| `task_status` | Durable workflow status |
| `project_id`, `branch_id` | CAD-Agent durable workspace identity |
| `expected_base_revision_id` | Branch head used as optimistic-concurrency base |
| `revision_id` | Candidate revision produced by the workflow |
| `change_set_id` | Change set ID, when available |
| `files` | Output URLs such as `stl` and `step` |
| `inspect_report` | Engineering inspection report from CAD-Agent |
| `repair_history` | Automatic repair attempt history |
| `validation` | Compatibility validation result |
| `error_type`, `error_message` | Failure details |

## Mesh quality metrics

`quality_eval.py` compares reference STL and generated STL with `trimesh`.

| Metric | Meaning |
| --- | --- |
| `bbox_relative_error` | Strict bounding-box size error |
| `volume_relative_error` | Strict volume error |
| `area_relative_error` | Surface-area error |
| `bbox_ratio_relative_error` | Normalized shape-ratio error |
| `fill_ratio_relative_error` | Volume fill-ratio error |
| `normalized_chamfer` | Centered/scaled Chamfer distance |
| `generated_watertight` | Whether generated STL is watertight |
| `dimension_score` | Size-oriented score |
| `shape_score` | Shape/topology-oriented score |
| `quality_score` | Combined quality score |

Current scoring formula:

```text
shape_score = 0.35 * validity_score + 0.30 * chamfer_score + 0.25 * bbox_ratio_score + 0.10 * fill_ratio_score
dimension_score = 0.55 * bbox_score + 0.35 * volume_score + 0.10 * area_score
quality_score = 0.55 * shape_score + 0.45 * dimension_score
```

## Interpretation guide

- High success rate but low quality score: files are produced, but geometry does
  not match the reference well.
- High bbox error: check units, numeric dimensions, and prompt measurement
  interpretation.
- High Chamfer distance: global shape differs significantly.
- `generated_watertight = false`: STL topology is likely invalid for downstream
  manufacturing use.
- Many provider/sandbox failures: check environment before judging algorithm
  quality.
