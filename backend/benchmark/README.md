# CAD Agent 评测基线（eval harness）

回答一个问题：**这次更新（prompt / 模型 / retriever / retry）让结果变好了、没效果、还是变坏了？**

> 这是**贵、慢、概率性**的 LLM 评测，需要真实 LLM key + sandbox 镜像。**不进 PR CI**——只在动了 prompt/模型/retriever 时手动或 nightly 跑。逻辑回归由 `tests/`（hermetic，无 LLM/Docker）在 CI 把关。

## 组成

| 文件 | 作用 |
|---|---|
| `eval_cases.py` | 50 个评测 case，聚焦 3D 打印件，覆盖各建模路径，**与 RAG examples 不重合** |
| `metrics.py` | 纯函数：从 `GenerateResponse` 抽取分层指标 + 聚合（pass@1 / pass@k / pass^k）|
| `eval.py` | 主评测脚本：跑真实流水线 N 次 + 渲染图 + 版本化输出 |
| `compare.py` | 基线 vs 候选 的回归对比，逐 case 列「修好/弄坏」|
| `check_overlap.py` | 维护工具：检测 case 是否和 RAG examples 撞车（开卷）|
| `runner.py` / `cases.py` | **legacy**（旧的 20-case success-rate benchmark，保留不动）|

## 用法

```bash
cd backend

# 1) 维护期：确认 case 没和 RAG examples 撞车
python -m benchmark.check_overlap

# 2) 小样冒烟（1 个 case 1 次，看渲染图确认形状）
python -m benchmark.eval --cases P01,X01 --n 1

# 3) 建立基线（全量 50 case x 3 次）
python -m benchmark.eval --n 3
#   产出 reports/eval_<UTC>_<gitsha>.json + reports/<run_id>/renders/

# 4) 改了 prompt 后，跑候选并直接对比基线
python -m benchmark.eval --n 3 --baseline reports/eval_<基线>.json

# 5) 单独对比两份报告
python -m benchmark.compare reports/eval_A.json reports/eval_B.json

# zero-shot（关 RAG，测裸能力）
python -m benchmark.eval --n 3 --no-rag
```

## 怎么判定「合格」（核心）

主指标**不是** `success`（流水线最后一次即使几何没过也返回 success=True，会高估质量）。每个 case 的 `passed` 只由**真实几何事实**决定：

- **3D 件**：执行成功 **且** `printable`（来自 inspect_report/validation）**且** 尺寸匹配（bbox 排序 ±tol）
- **2D 件**：执行成功 **且** 产出了 dxf/svg
- **装配体**：`should_be_printable=False`，只要求产出可测实体（有 inspect verdict）

`part_type` / 孔数（`feature_match`）来自 planner 的自然语言理解，不是几何，**只作诊断，不参与合否判定**——否则几何正确的模型会因分类抖动误判 fail。

人工复核：每次运行保存 4 视角渲染图于 `reports/<run_id>/renders/<case>_run<n>/`，自动指标抓不到的「尺寸对但形状错」靠看图兜底。

> 已知口径差异：复杂件走 multi-step 路径时只填 `validation` 不填 `inspect_report`（见 `multi_step.py`），所以这些 case 的 `verdict_pass` 可能不可用，但 `printable`/`passed` 仍从 `validation` 正确取到。读 `verdict_pass_rate` 时按 `by_path` 分组看；`unavailable` 表示没有权威字段，不等于失败。

## 怎么读 compare 结果

- **pass@1**：单次成功率（日常主看）。delta 超过基线 std 才标 `*significant*`，否则是 LLM 噪声。
- **pass@k**：k 次里≥1 次过（能力上限）。**pass^k**：k 次全过（稳定性）。
- `overall.pass_metric_coverage` reports scored cases/runs against all cases/runs. Pass rates remain calculated only from scored samples; check coverage before interpreting a rate as representative of the full batch.
- **逐 case 清单**：`FIXED` / `BROKE` / `hard regressions`（曾 100% 过现在不过）。这是最有价值的——抓 prompt 改动的副作用。
- 退出码：净回归（broke > fixed）或任何 hard regression → 非零，可作 gate。

## 基线纪律

- 报告 `meta` 带 `git_sha / model / temperature / rag_enabled / case_set_hash`。
- **只在 `case_set_hash` 一致时**比较聚合指标才有效（compare 会警告）。
- 改了 case 集 = 旧基线作废，重新建基线。
- `reports/` 已 gitignore；canonical 基线请手动另存/显式提交。

## Developer-only executable CADTests

The repository also contains a small, offline evaluator for developer and CI use. It reads an existing benchmark JSON report, runs versioned CADTests against the evidence already recorded in that report, and writes a separate UTF-8 JSON report.

```powershell
python -m benchmark.cad_eval --mode observe `
  --input reports/eval_<id>.json `
  --output reports/cad_eval_<id>.json
```

The command is developer-only and does not instantiate the production orchestrator, call a model provider, start a sandbox, modify a document, or change a task. `--mode disabled` is the default and exits without reading or writing a report. Missing or non-authoritative evidence is reported as `unknown` or `blocked`; it is never converted into a passing engineering result.

## Normal developer observe mode

Use observe mode when developing through the normal frontend workflow. No
document ID, project ID, branch ID, task ID, or benchmark case is required.

```powershell
python -m app.workers.run --evaluation-mode observe
python -m app.run --evaluation-mode observe --port 8000
```

Then use the frontend normally for create, part edit, review, commit, and
history restore operations. The real workflow automatically writes one live
report per workflow under:

```text
backend/benchmark/reports/live/live_<workflow-id>.json
backend/benchmark/reports/live/live_<workflow-id>.md
```

The JSON file is the machine-readable evidence record. The Markdown file is a
deterministic developer summary of the observed stages, statuses, failures,
and terminal outcome. It does not invent a stage or replace the business
workflow result. Normal users do not see this developer-only output.

## Compare live reports

Compare two live reports without rerunning a task:

```powershell
python -m benchmark.live_compare `
  reports/live/live_<base-workflow-id>.json `
  reports/live/live_<candidate-workflow-id>.json
```

Use `--json` for machine-readable output. Use `--fail-on-drift` in a
developer check when a normalized call sequence change should fail the
command. The comparison ignores runtime IDs, timestamps, and duration values;
it reports added, removed, reordered, retried, status-changed, and
input/output-hash-changed calls separately. The output also includes an
evidence-based aggregate conclusion:

- `consistent`: the observed tool path and available outcome evidence match;
- `different`: the tool path, geometry, quality, or lifecycle evidence differs;
- `unknown`: some evidence exists, but a required comparison dimension is missing;
- `not_comparable`: neither report contains enough authoritative evidence.

The aggregate conclusion does not use raw input/output hashes as semantic
identity. Different user-task IDs do not by themselves make the tool-path
comparison fail. Resource metrics remain diagnostic and are marked
`not_comparable` when the two reports do not share a benchmark source identity
and base revision.

## Optional live benchmark execution

Use `--live-observe` when the benchmark must exercise the same durable API,
Temporal workflow, Worker, sandbox, validation, candidate, and report path as a
real task. This is an optional developer tool for injecting a fixed benchmark
case into a dedicated existing workspace; it is not the normal acceptance
path. It never
creates a project or revision on behalf of the benchmark.

```powershell
python -m benchmark.eval --live-observe --cases P01 --n 1 --concurrency 1 `
  --document-id <benchmark-document-uuid>
```

The API and Worker must already be running with `--evaluation-mode observe`.
The document must already exist and be a dedicated benchmark document. The
command reads its current project, active branch, and head revision from the
real document snapshot before submitting any case. It never creates a project,
branch, revision, or placeholder UUID.

The benchmark uses `source.kind=benchmark` and `source.id=<case-id>` when it
enters durable admission, then links each row to the corresponding
`benchmark/reports/live/live_<workflow-id>.json` report. A missing or unfinished
live report is recorded as a failed benchmark row and cannot become a pass.

The built-in cases in `benchmark.eval --live-observe` report business delivery
only (`evaluation_scope=delivery_only`); they are not CADTest cases. The CLI
reads live reports from the same `CAD_EVALUATION_REPORT_ROOT` as the API and
Worker. Use the CADTest batch command below for independent scoring.

## CADTest batch evaluation

The batch entry point is part of CAD-Agent and does not call the external
project's `run.py`. The dataset is read-only input data; all scoring code,
native delivery checks, batch aggregation, and live-report integration are in
this repository.

Set `CAD_TEST_REQUIRED=true`, `TEMPORAL_WORKER_BUILD_ID`, and
`CAD_TEST_DATASET_ROOT` in both the CLI and evaluator Worker environments.
The build ID must be the same immutable deployment revision in both processes;
Temporal Workers use build-ID versioning and scoring rejects missing or
mismatched Worker identity before invoking the scorer. In Docker Compose, set
`CAD_TEST_DATASET_HOST_ROOT` in the repository-root `.env` to the host dataset
directory; the Worker mounts it read-only at `/app/cad-test-dataset`. A required
evaluator Worker fails startup if the dataset, scorer image, FreeCAD runtime, or
build ID is unavailable. Ordinary Workers keep `CAD_TEST_REQUIRED=false` and do
not require CADTest assets. If using a custom report directory, set
`CAD_EVALUATION_REPORT_ROOT` for the CLI, Worker, and API to the same shared
directory. `CAD_TEST_OUTPUT_ROOT` only changes the scorer's working directory,
not the live report store. `live` path flags assert the configured paths; they
cannot configure a separately running Worker. A missing shared report or a
Worker dataset whose hash differs from the admitted dataset cannot pass.

The sealed candidate becomes available before independent CADTest scoring
finishes. Strict batch runs wait separately for the scoring result; if the
scoring wait expires, the row retains the business report but does not pass.

```powershell
$env:CAD_TEST_DATASET_ROOT = 'D:\project\projectcad\CAD_test'
$env:CAD_TEST_REQUIRED = 'true'
$env:TEMPORAL_WORKER_BUILD_ID = '20ea31b5d0d5faf766be2b178c33c374e7559154'
# For Docker Compose, set the same host path in the repository-root .env:
# CAD_TEST_DATASET_HOST_ROOT=D:\project\projectcad\CAD_test

python -m benchmark.cad_test_batch dataset-check

python -m benchmark.cad_test_batch live `
  --cases ctb-00000007,ctb-00001411 `
  --repeat 1 `
  --evaluation-timeout-seconds 420 `
  --document-id <benchmark-document-uuid>
```

The live batch preflights the dataset, scorer image, report-root write access,
and FreeCAD native-delivery runtime before submitting any model. Its generation
timeout and independent CADTest evaluation timeout are separate; unavailable
or pending scores are excluded from model-verdict pass-rate denominators.

The live batch report is written under
`backend/benchmark/reports/cad_test/<run-id>/report.json`. Each case includes
the CADTest checks, native delivery facts, the live workflow report path, and
the process evaluation block containing activity count, CAD operation count,
repair count, retries, failures, geometry/DFM/visual statuses, lifecycle
status, and measured duration. The batch summary aggregates those process
metrics in addition to pass rates. The same directory also contains
`report.html`, a developer-only static readable view of the JSON report. It
shows the final CADTest gate separately from Live process metrics and expands
each case to show stage durations, tool calls, timeline events, and failure
evidence. It does not run another scorer or infer missing values.

For an already downloaded STEP artifact, score it without submitting a new
workflow:

```powershell
python -m benchmark.cad_test_batch score `
  --dataset-root 'D:\project\projectcad\CAD_test' `
  --case ctb-00000007 `
  --step .\result.step `
  --out .\reports\cad_test\manual-ctb-00000007
```

`CAD_TEST_ROOT` remains accepted as a compatibility alias for the dataset
root. It no longer selects a scorer script or imports code from that path.

For a case with `path: "2d"` in the validated manifest, its `file_hashes` must
include `data/cases/<id>/reference.dxf` and `data/cases/<id>/checks.json`.
The reference drawing is independent of the submission. `checks.json` is
`{"schema_version":"cad-test-dxf-checks.v1","tolerance_mm":0.01}` (choose the
case's own positive tolerance). Both drawings must use millimeters. The scorer
compares planar lines, circles, arcs and lightweight polylines by type, count,
size, position and polyline connectivity/closure. Unsupported representations
or missing truth cannot pass. The existing 3D dataset needs no conversion.

For compatibility with existing scripts, the old explicit workspace form is
still accepted:

```powershell
python -m benchmark.eval --live-observe --cases P01 --n 1 --concurrency 1 `
  --project-id <project-uuid> `
  --branch-id <branch-uuid> `
  --base-revision-id <head-revision-uuid>
```

Do not combine `--document-id` with the three legacy options. Normal frontend
usage does not require any of these options; they are only for developer live
evaluation.

Live cases are serialized because one benchmark workspace has one expected
branch head. The existing direct `benchmark.eval` path remains the default, and
`benchmark.cad_eval` remains available for compatibility fixtures and existing
reports.
