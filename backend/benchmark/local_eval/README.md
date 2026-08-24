# 本地评测 harness（accuracy + latency）

一键复现视觉门（VLM-in-the-loop）在仓库 benchmark 上的准确率与耗时。

**双击 `Run-Eval.cmd`** 即可运行。（Windows 双击 `.ps1` 只会用编辑器打开，所以入口是
`.cmd`；它只为这一个进程设置 `-ExecutionPolicy Bypass`，不修改任何机器或用户级策略。）

也可以直接调用：

```powershell
.\Run-Eval.ps1                      # 全部 50 个 case，pass@1
.\Run-Eval.ps1 -N 3 -Concurrency 5  # 3 次重复，另给 pass@k / pass^k
.\Run-Eval.ps1 -Difficulty moderate # 只跑某个难度
.\Run-Eval.ps1 -NoVisual            # 消融：关掉视觉精修，量化其贡献
```

## 选择 provider

| 入口 | provider | 语言模型 | 视觉模型 |
|---|---|---|---|
| `Run-Eval.cmd` | 按 `backend/.env` | 由 `.env` 决定 | 由 `.env` 决定 |
| `run_eval_dashscope.cmd` | DashScope（Qwen） | `qwen3-coder-plus` | `qwen3-vl-plus` |
| `.\Run-Eval.ps1 -Provider openai` | OpenAI | `gpt-5.4` | `gpt-5.4` |

`-Provider` 只设置进程级环境变量（`LLM_PROVIDER` / `LLM_MODEL` / `VISION_MODEL` /
`LLM_BASE_URL` / `LLM_TIMEOUT_S`），它们优先级高于 `.env`。**脚本本身不读取任何密钥**：
`Settings` 自己加载 `.env`，脚本只检查对应的 key 行是否存在。

单独覆盖模型：

```powershell
.\run_eval_dashscope.cmd -Model qwen3-max -VisionModel qwen3-vl-flash
```

### 两个 provider 的实测结果（各 50 个 case，n=1）

| provider | 模型 | pass@1 | simple | moderate | complex | 单 case 墙钟中位数 |
|---|---|---|---|---|---|---|
| OpenAI | `gpt-5.4` | 43/50 (86.0%) | 85.7% | 83.3% | 91.7% | 74s |
| DashScope | `qwen3-coder-plus` + `qwen3-vl-plus` | 46/50 (92.0%) | 100% | 91.7% | 83.3% | 145s |

基线报告：`reports/baseline-2026-08-23-visual.json`、
`reports/baseline-2026-08-24-dashscope-visual.json`。用
`report_timing.py <a.json> <b.json>` 并排打印。

> **不要据此断言 DashScope 更准。** 两次都是 `n=1`。逐 case 对比显示 **7 个 case 翻转**
> （DashScope 修好 P07/P11/P15/P21/P27，弄坏 M05/P26），而净差只有 3 个——
> 抖动幅度大于差值，说明主要是采样噪声而非能力差异。要下结论需要 `-N 3` 以上并用
> [`compare.py`](../compare.py) 检验显著性。
>
> 能确定的是**耗时**：DashScope 慢 2.4 倍（均值 173s vs 71s，最慢 585s vs 167s）。

### DashScope 的模型是怎么选的

在 6 个 case 上做过实测对照（样本很小，只够用来排除明显更差的选项）：

| 模型 | pass@1 | 6 个 case 总耗时 |
|---|---|---|
| `qwen3-coder-plus` | 5/6 | 468s |
| `qwen3-max` | 5/6 | 660s |

准确率打平（连失败的 case 都相同），所以按速度选了 `qwen3-coder-plus`。
`qwen3.8-max` 被排除：它每次调用会输出约 4000 个 thinking token，单次约 92s，
单个 case 能跑到 294s，并且在 180s 的客户端超时下直接失败——所以 DashScope 的
`LLM_TIMEOUT_S` 默认调到 420s。

视觉侧 `qwen3-vl-plus`、`qwen3-vl-flash`、`qwen-vl-max` 实测都在 3–5s，
选了能力最强的 `qwen3-vl-plus`。注意视觉模型不只看图，还要**改代码**，
所以它的代码能力同样重要。

## 这不是产品链路

**`app/` 下没有任何代码 import 本目录，也不应该有。**

产品明确拒绝在宿主机上执行生成的 CAD（见 [CLAUDE.md](../../../CLAUDE.md)：业务层不得调用
Docker / Podman / 宿主 shell，缺少运行时必须返回 `blocked` 而不是隐式回退）。本 harness
只替换**一件事**——隔离边界：生成的代码在一个可被杀掉的本地子进程里执行，而不是沙箱容器里。
这样才能在没有容器运行时的工作站上测出真实准确率。

其余全部是真的：真实 planner、真实代码生成、真实 CadQuery 内核、真实几何校验、真实视觉门，
以及仓库自己的评分函数（`benchmark.metrics.extract_metrics` + `apply_artifact_gate`）和
产物校验（`benchmark.eval._verify_*`）。

**如果本机有容器运行时，请用真正的 harness**，它跑的是产品自己的 executor：

```bash
docker build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
cd backend && python -m benchmark.eval --n 3
```

## 评分口径

Case 里的 `expected_dims` / `expected_features` **只用于打分**，不会进入 pipeline——
模型只看到 `description`，和真实用户请求完全一致。判定口径直接复用
[`benchmark/metrics.py`](../metrics.py)，不是本目录自定义的：

- 3D：`executed` 且 `printable` 且 `dim_match is not False`，再加产物门（STEP/STL 可读、
  几何非空、渲染出 4 视图）
- 2D：`executed` 且产出 dxf/svg，再加 DXF 可读

## 两个耗时口径

报告同时给出两个数，单独引用任何一个都会误导：

| 指标 | 含义 |
|---|---|
| `wall_time_ms` | 整次生成的墙钟时间，**包含并发排队**。是负载下的吞吐，不是单例延迟。 |
| `exec_time_ms` | 仅 CAD 执行时间。 |

两者之差就是模型延迟：规划、代码生成，以及视觉环的 render / critique / patch 往返。
`-Concurrency` 调大会缩短总耗时，但会推高单 case 的 `wall_time_ms`。

## 前置条件

脚本会自检并逐项报告，缺什么给出精确的 pip 命令（可用 `-InstallDeps` 自动装）。
只安装缺失的包且不锁版本——整装 `requirements.txt` 会降级 numpy，可能弄坏可用环境。

凭据从 `backend/.env`（gitignored）或环境变量 `OPENAI_API_KEY` 读取。脚本自身**不接触
密钥内容**：`Settings` 直接加载 `.env`（`config.py: env_file=".env"`），脚本只检查该行
是否存在。

目标 Python 为 3.11+。3.10 会自动启用 `py311_shim/`（只补 `datetime.UTC` 和
`enum.StrEnum` 两个符号）。

## 输出

报告与日志写到 `reports/eval-<时间戳>-<visual|novisual>.json` / `.log`。事后重新打印：

```bash
cd backend && python benchmark/local_eval/report_timing.py benchmark/local_eval/reports/<报告>.json
```

`report_timing.py` 可以接多个报告，便于并排比较 visual 与 novisual 两次运行。

## 结果会波动

pipeline 是概率性的：同一个 case 两次运行可能一次过一次不过。`-N 1` 只是一个样本；
判断一次改动是变好还是变坏，用 `-N 3` 并关注 pass@1 的变化是否超过噪声，必要时用
[`benchmark/compare.py`](../compare.py) 做基线对比。
