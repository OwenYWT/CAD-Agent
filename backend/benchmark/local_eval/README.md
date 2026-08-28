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

| 入口 | `LLM_PROVIDER` | 语言模型（规划+生码） | 视觉模型（评审+改码） |
|---|---|---|---|
| `Run-Eval.cmd` | 按 `backend/.env` | 由 `.env` 决定 | 由 `.env` 决定 |
| `.\Run-Eval.ps1 -Provider openai` | `openai` | `gpt-5.4` | `gpt-5.4` |
| `run_eval_dashscope.cmd` | `dashscope` | `qwen3-coder-plus` | `qwen3-vl-plus` |
| `run_eval_dashscope_kimi.cmd` | `dashscope` | `kimi-k3` | `qwen3-vl-plus` |

`-Provider` 的取值是**预设名**而不是 provider 名：`dashscope-kimi` 会把
`LLM_PROVIDER` 设成 `dashscope`，只把语言模型换成 Kimi。DashScope 上没有 Kimi 的
视觉版本，所以视觉门仍然走 `qwen3-vl-plus`。

`-Provider` 只设置进程级环境变量（`LLM_PROVIDER` / `LLM_MODEL` / `VISION_MODEL` /
`LLM_BASE_URL` / `LLM_TIMEOUT_S`），它们优先级高于 `.env`。**脚本本身不读取任何密钥**：
`Settings` 自己加载 `.env`，脚本只检查对应的 key 行是否存在。

单独覆盖模型：

```powershell
.\run_eval_dashscope.cmd -Model qwen3-max -VisionModel qwen3-vl-flash
```

### 实测结果（全部 50 个 case，均为 n=1）

| 语言模型 | pass@1 | simple | moderate | complex | 墙钟中位数 | 并发 |
|---|---|---|---|---|---|---|
| `gpt-5.4` (OpenAI) | 43/50 (86.0%) | 85.7% | 83.3% | 91.7% | 74s | 4 |
| `qwen3-coder-plus` | 46/50 (92.0%) | 100% | 91.7% | 83.3% | 145s | 4 |
| `kimi-k3` 第 1 次 | 40/50 (80.0%) | 78.6% | 79.2% | 83.3% | 286s | 4 |
| `kimi-k3` 第 2 次 | 42/50 (84.0%) | 71.4% | 91.7% | 83.3% | 374s | 8 |

视觉模型：OpenAI 用 `gpt-5.4`，三个 DashScope 跑法都用 `qwen3-vl-plus`。
基线报告在 `reports/baseline-*.json`，用 `report_timing.py <a.json> <b.json>` 并排打印。

> ### 这张表**不能**用来排名
>
> 最有信息量的不是 provider 之间的差，而是**同一个模型跑两次的差**：
> `kimi-k3` 两次分别是 **80.0% 和 84.0%**，配置完全相同，只有并发不同
> （并发本身已验证不是原因，见下）。失败构成也整体换了一批——
> 第 1 次是 7 个 planner / 1 个尺寸，第 2 次是 3 个 planner / 3 个尺寸，
> 两次都失败的只有 P03 和 X02。
>
> 也就是说，**单次 50-case 运行的抖动约 ±4 个百分点**。表里 `gpt-5.4` 86% 与
> `qwen3-coder-plus` 92% 的差距落在这个抖动范围内，**不构成能力差异的证据**。
> 要真的比较，必须 `-N 3` 以上并用 [`compare.py`](../compare.py) 检验显著性。
>
> 能确定的只有**耗时**：差距大到不可能是噪声。GPT 中位数 74s，Qwen 145s，
> Kimi 374s，且只有 Kimi 撞到过 deadline（X06 在 1171s 超时）。

#### 并发不影响准确率

第 1 次 Kimi 运行有 7 个 case 挂在 planner JSON 上，一度以为是并发争抢导致的。
实测否定了这个猜测：把 7 个 planner 调用同时打出去（叠加在另一个正在跑的评测上），
6/7 通过；失败率在并发 2、4、8 下都是 ~14%，与并发无关。
把并发从 2 提到 8 之后总耗时从约 2 小时降到约 45 分钟，pass@1 反而更高。
**这里可以放心加并发。**

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

### DashScope 上的 Kimi

DashScope 也转售 Moonshot 的 Kimi。实测单次生码调用：

| 模型 | 耗时 | 说明 |
|---|---|---|
| `kimi-k3` | 21.1s | 最新，作为 `dashscope-kimi` 预设的默认语言模型 |
| `kimi-k2.7-code` | 17.6s | 代码专用，也是仓库 `settings.llm_model` 的默认值 |
| `kimi-k2-thinking` | 37.5s | 推理型，明显更慢 |
| `kimi/kimi-k3` 等带 `kimi/` 前缀的 | — | 本账号未开通，返回 “product is not activated” |

> **Kimi 不接受 `temperature`**（返回 400，直接失败而不是降级）。原先
> `app/llm.py` 是按 **provider** 判断要不要去掉采样参数的（`provider == "moonshot"`），
> 所以经 DashScope 转售的 Kimi 全部漏判、每个请求都 400。已改成按**模型名**判断
> （`_is_kimi_model`），任何 provider 下的 Kimi 都能正确处理。

#### 未修复：kimi-k3 的 planner JSON 退化

约 **14%** 的 planner 调用会返回**语法非法的 JSON**，而且不是被截断——
`finish_reason` 是正常的 `stop`，长度也没到上限，但内容退化成重复垃圾：

```
: "    :","    ,"%22    ,        : "    :","    ,"%22    ]    }":"    "},"}
```

（空 key、混入 URL 编码的 `%22`、大量填充空格。）即使已经传了
`response_format={"type": "json_object"}` 也会发生。

planner 本身已经重试 2 次。如果失败是独立的，14% 的单次失败率重试两次后应该只剩约 3%，
但实测**整案失败率就是 ~14%**，说明失败与 prompt 相关而非随机——
P03 在三次运行里全部失败可以佐证。

影响不止评测：`kimi-k2.7-code` 就是仓库 `settings.llm_model` 的默认值，
生产链路同样会踩到。**尚未修复**，可能的方向是提高 planner 重试次数，
或把「`stop` 但 JSON 非法」单独识别为可重试错误。

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
