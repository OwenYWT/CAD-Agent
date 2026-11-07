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

> 已知口径差异：复杂件走 multi-step 路径时只填 `validation` 不填 `inspect_report`（见 `multi_step.py`），所以这些 case 的 `verdict_pass` 恒为 False，但 `printable`/`passed` 仍从 `validation` 正确取到。读 `verdict_pass_rate` 时按 `by_path` 分组看，别把 multi-step 的低 verdict 误读为质量差。

## 怎么读 compare 结果

- **pass@1**：单次成功率（日常主看）。delta 超过基线 std 才标 `*significant*`，否则是 LLM 噪声。
- **pass@k**：k 次里≥1 次过（能力上限）。**pass^k**：k 次全过（稳定性）。
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
input/output-hash-changed calls separately.

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
