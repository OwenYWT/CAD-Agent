# 视觉精修（VLM-in-the-loop）

本文说明 `backend/app/visual_refine` 的职责、边界和配置。实现事实以代码为准；本地启动见 [`development.md`](development.md)，生产配置见 [`../DEPLOY.md`](../DEPLOY.md)，评测方式见 [`../backend/benchmark/README.md`](../backend/benchmark/README.md)。

状态：已实现并接入 Durable 与进程内两条链路。核对日期 2026-08-23。

## 解决的问题

旧视觉门只做一件事：把四张 matplotlib 渲染图和一句需求交给视觉模型，问「像不像」。它有两个结构性缺陷：

1. **渲染不可读。** `Poly3DCollection` 没有深度缓冲，实体渲染成半透明：背面透出、通孔看起来像实心柱、凹槽看起来像悬空框。画面也没有任何比例信息，`100x60x40` 的零件只占画布一小块，「这个是不是 35mm」无法回答。
2. **修复看不见结果。** `fix_visual_issues` 只收到文字问题描述，没有渲染图。模型在没见过自己代码产出的情况下改代码。

结果是尺寸错误只能靠运气发现，而修复经常删掉特征来「消除」问题。

## 现在的做法

### 1. 可测量的渲染

`mesh_views.py` 用 numpy 软件光栅器重写，逐像素 z-buffer，无外部渲染依赖：

- 实体不透明，隐藏面正确剔除；边线由深度/法线图像空间检测得到，因此不会把三角化对角线画成假倒角。
- 每个视图按自身投影范围取景；三个正交视图**共用一个 mm/px 比例**，比例错误因此可见。
- 图上烧录：视图名、屏幕坐标轴对应的模型轴、两个方向的真实 mm 长度、毫米网格、比例尺、整体包围盒。
- 背面用暖色渲染。在剖视图里这是被切开的内壁（正常）；在非剖视图里出现暖色即表示法线翻转或开壳（缺陷）。
- 额外提供 `section` 剖视图，露出型腔、凹槽和盲孔——这些从外部视图完全看不见，也正是旧视觉门最常误判通过的地方。

### 2. 能测的就测，不问模型

`facts.py` 直接从网格测量，`spec.py` 据此判定：

| 判定来源 | 内容 |
| --- | --- |
| `measured`（网格测量，权威） | 整体尺寸、孔数、孔径、通孔/盲孔、独立实体数、水密性、体积 |
| `observed`（视觉模型） | 形状是否是所要求的物体、特征是否存在且位置合理、是否退化/自交/多余凸起 |

测量结果作为「已确定事实」写进给模型的提示，模型不得据图推翻。视觉模型只回答真正需要「看」的问题。

孔径与孔数用最小二乘拟合圆从网格恢复，因此「四个 Ø6.00 通孔，轴向 Z，中心 (±40, ±22)」是测出来的，不是猜的。

自然语言里的孔数解析采取**歧义即弃权**策略：`四个角各一个直径 6mm 的通孔` 同时支持 4 和 1，因此不产生确定性判定，交给视觉模型结合测量事实判断。误判一个正确零件的代价远高于少一条确定性检查。

### 3. 总判定是算出来的，不是模型说的

`contracts.py` 中 `VisualCritique.is_match` 由逐条判定推导：存在 blocking/major 违反即失败；置信度低于 0.7 不算通过；没有任何检查项即 indeterminate。模型列出致命缺陷却写 `"is_match": true` 这种常见失败模式在结构上不可能发生。

### 4. 修复时能看见自己的产出

`patcher.py` 把失败构建的渲染图连同源码、测量事实和缺陷清单一起发给模型——正是「用截图改界面」的同一套做法。

### 5. 单调收敛

`loop.py` 对每个候选评分，只返回**能证明更好**的那个：

- 通过 → 直接返回，不浪费修复预算。
- 修复后构建失败 → 丢弃，保留原件。
- 修复后评分更低 → 丢弃，保留原件。
- 视觉服务不可用 → 返回 indeterminate 并保留原件，绝不静默通过。

因此提高迭代预算只会更慢，不会更差。

## 分层与解耦

由内向外，每层只依赖内层：

| 模块 | 依赖 | 职责 |
| --- | --- | --- |
| `mesh_views.py` / `facts.py` | numpy、trimesh、Pillow | 纯几何与像素。**无 `app.*` 导入** |
| `contracts.py` / `spec.py` | pydantic | 需求、判定、评分，纯数据 |
| `prompts.py` | 无 | 提示词，单独版本化 |
| `ports.py` | 上述 | 四个 Protocol：构建、渲染、评审、修补 |
| `critic.py` / `patcher.py` | 注入的 client | 模型供应商边界 |
| `renderers.py` | 渲染核心 | 默认 `ViewRenderer` |
| `loop.py` | 仅 ports | 编排，与供应商和 CAD 无关 |
| `factory.py` | `app.config` | **唯一**读取应用配置的模块 |

`loop.py` 不认识 Docker、Temporal、OpenAI 或 CadQuery，因此 Durable 链路、进程内链路和无外部依赖的单测使用同一份编排代码。

`mesh_views.py` 与 `facts.py` 由 `backend/sandbox/Dockerfile` **原样复制**进隔离 Worker（两者只依赖已固定的 numpy/trimesh/Pillow）。控制平面与 Worker 运行同一份文件，渲染哈希与测量结果因此可比；维护两份相似实现会让已存证据静默失效。

## 接入点

| 链路 | 接入位置 |
| --- | --- |
| Durable | `agent_v2.py::_visual_gate` → `activities.py` 的 `render_visual` / `judge_visual` / `repair_visual` → `validation/durable_visual.py` |
| 进程内 | `agent/state_machine.py`、`agent/multi_step.py` → `agent/visual_gate.py` |
| 单次判定（降级） | `validation/vision_validator.py` |

Worker 内测量几何并随渲染证据一并返回，控制平面不需要自己打开模型文件。剖视图作为**补充证据**单独传递，四张渲染图的 Durable 合同不变，已存证据仍然有效。

未配置视觉凭据时，进程内链路降级为单次判定；两者都不可用时如实记为 indeterminate，不伪装通过。

## 配置

见 [`../backend/.env.example`](../backend/.env.example)：

| 变量 | 说明 |
| --- | --- |
| `VISION_MODEL` | 评审与修补都用它。必须支持图像输入 |
| `VISUAL_REFINEMENT_ENABLED` | 关闭后退回单次视觉门 |
| `VISUAL_REFINEMENT_MAX_ITERATIONS` | 每次生成的迭代预算，每轮 = 一次沙箱执行 + 一次视觉调用 |
| `VISION_RENDER_PX` | 低于约 640px 时烧录的尺寸文字对模型不可读，测量优势失效 |
| `VISION_RENDER_SUPERSAMPLE` | 抗锯齿倍率 |
| `VISION_CRITIC_MAX_TOKENS` / `VISION_PATCHER_MAX_TOKENS` | 两个边界各自的输出预算 |

### Provider

`moonshot`、`openai`、`dashscope` 各自带默认端点，切换 provider 不需要同时改
`LLM_BASE_URL`；若该值仍指向另一个已知 provider 的域名，会被判定为陈旧值并替换成正确端点，
自定义网关/代理地址则始终保留。

| `LLM_PROVIDER` | 密钥变量 | 建议的 `VISION_MODEL` |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `gpt-5.4` |
| `dashscope` | `DASHSCOPE_API_KEY` | `qwen3-vl-plus` |
| `openai_compatible` | `DASHSCOPE_API_KEY` | 由部署方指定，必须自行设置 `LLM_BASE_URL` |

`VISION_MODEL` 不只用来看图，**它同时负责改写代码**，所以代码能力和视觉能力一样重要。
Qwen 的 `qwen3.x-max` 系列每次调用会产生数千 thinking token（实测单次约 92s），
用它之前必须把 `LLM_TIMEOUT_S` 调高，否则视觉门会直接超时失败。

不同 provider 的实测对比见
[`../backend/benchmark/local_eval/README.md`](../backend/benchmark/local_eval/README.md)。

## 升级注意

渲染器与测量代码进入了沙箱镜像，**必须重建镜像**，否则 Worker 仍在跑旧渲染：

```bash
docker build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
```

生产环境按 [`../DEPLOY.md`](../DEPLOY.md) 推送到 Registry 并更新 `SANDBOX_IMAGE` 的 `@sha256:` digest。

渲染实现变化会改变渲染字节，因此新旧证据的 sha256 不同。这不影响已存证据的有效性（按 `staging_manifest_id` 回放），但两次运行的渲染哈希不再可直接比对。

## 验证

```bash
cd backend
python -m pytest tests/test_visual_refine_spec.py tests/test_visual_refine_loop.py \
                 tests/test_visual_refine_critique.py tests/test_durable_visual_validation.py
```

以上为无外部依赖的 hermetic 测试。真实模型与沙箱的准确率评测：

```bash
python -m benchmark.eval --difficulty medium --n 3
```
