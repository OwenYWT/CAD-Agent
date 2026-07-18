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
