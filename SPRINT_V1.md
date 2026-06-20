# Sprint V1 — CAD Agent for 3D Printing Users

> Source design doc: `~/.gstack/projects/wentao/wentao-feature-modify-and-rollback-design-20260528-152126.md`
> Branch: `feature/modify-and-rollback`
> Owner: Owen
> Sprint window: 2026-05-29 → 2026-06-19 (3 weeks)
> Status: APPROVED via /plan-eng-review on 2026-05-29

## 1. Sprint Goal

把现有 4838 行后端 + 工业向前端,改造成「**为 3D 打印用户**」的中文 Web 单页应用,3 周内上线灰度,Day 8 起支持 founder dogfood。

**Done definition:**
1. 用户访问 `/` 输入 prompt → 30-90 秒生成 STL → 三维预览 → 一键下载
2. STL 下载页显示 OrcaSlicer 切片报告(打印时间、耗材克数、是否需支撑、推荐方向)
3. 顶部双 mode 切换(打印 / 工业),默认打印
4. Examples 库含 ≥ 50 个打印场景 example
5. 部署到腾讯云,域名指向(等 ICP 备案,可先用 IP)
6. 测试覆盖:print-check 单元 100% 分支、E2E 主流程通过

## 2. Architecture Decisions Locked (from /plan-eng-review)

| # | 决策 | 选择 | 备注 |
|---|---|---|---|
| 1 | REST + WebSocket 共存 | C 保留现状 | WebSocket 推送 step,REST 留给未来插件 |
| 2 | 装配体路径 | B Planner prompt 跳过 + runtime fallback | 删 prompts.py:L11 第 9 条 + 第 12 条;planner.py:74-77 在打印 mode 下短路 assembly |
| 3 | 打印预估 | C1 OrcaSlicer Docker + 5 机型 profile | Bambu A1 mini/A1/X1C + Prusa Mk4 + Creality K1 |
| 4 | Prompt 模式 | B3 双 system prompt + UI 顶部切换 | ⚠️ 偏 Premise 1,Day 7 dogfood 后回评 |
| 5 | Examples 库 | 全网调研 + 整理 50 个 + dogfood 期补充 | Top 100 Printables/MakerWorld 反推 |

## 3. v1 Scope (in / out)

### IN
- 后端
  - `/api/generate?mode=print|industry` (mode 参数透传到 codegen prompt 选择)
  - `/api/print-check` (新)
  - 双 system prompt: `CODEGEN_SYSTEM_PROMPT_PRINT` + `CODEGEN_SYSTEM_PROMPT_INDUSTRY`
  - Planner 在 print mode 时强制非装配
  - WebSocket step 简化:`planning|generating|executing|fixing|complete|failed` (砍 multi-step 细节)
  - examples 库扩到 ≥ 50 个打印件
- 前端
  - 重写 `App.tsx` 单页面布局
  - 顶部 mode toggle(打印 / 工业)
  - 新组件 `PrintReport.tsx`(显示 print-check 输出)
  - 复用 `Viewer3D.tsx`、`ChatPanel.tsx`、`useWebSocket.ts`
  - 删除 / 隐藏:`ParameterPanel`、`AssemblyTree`、`KnowledgeGraph`、`DFMRuleConfig`、`SettingsDrawer`、`MultiStepProgress`、`PipelineLog`、`HistorySidebar`、`PanelTabs`、`DesignAnalysis`
  - 删 i18n 多语言切换(zh.json 留,en/de 删)
- 部署
  - Docker Compose 单机版
  - GitHub Actions push → SSH pull restart
  - OrcaSlicer 打入主 Docker image(或单独 sidecar)

### NOT IN SCOPE (deferred to Month 2+)
- 微信登录 / 用户体系
- Remix 社区机制
- Container pool / prompt cache
- Bambu Studio 深度 deeplink(v1 用 STL 下载 + QR)
- 国际化、移动端 App
- ICP 备案(运营动作,工程外平行做)
- Insurance plugin(冷冻 `plugins/`,README 加 "v2")
- 历史记录 UI(SQLite schema 留,前端不暴露)

### EXPLICITLY KILLED (代码不动,运行时短路)
- assembly_planner — Planner prompt 不返回 assembly,runtime 收到 assembly 时 fallback custom
- multi_step UI 进度暴露 — 后端保留,前端不显示子步
- DFM 工业评分 / 知识图谱 — endpoint 注释 deprecate
- 2D / DXF / SVG — 仅 STL

## 4. Architecture Diagram

```
USER (中文 prompt)
   │
   ▼
┌─────────────────────────────────┐
│  Frontend (React)               │
│  ├ ModeToggle (print|industry)  │
│  ├ ChatPanel (input + steps)    │
│  ├ Viewer3D (STL preview)       │
│  └ PrintReport (slicer output)  │
└─────────────────────────────────┘
   │  WebSocket /ws/{sid}      │  REST /api/generate
   ▼                           ▼
┌─────────────────────────────────┐
│  FastAPI Backend                │
│                                 │
│  Orchestrator.generate(prompt, mode)
│    │                            │
│    ├─ Planner.plan_new          │
│    │    └─ if mode=print: skip assembly
│    │                            │
│    ├─ Retriever.find_similar    │
│    │    └─ filtered by mode tag │
│    │                            │
│    ├─ CodeGen.generate          │
│    │    └─ system prompt by mode│
│    │                            │
│    ├─ Sandbox.execute (Docker)  │
│    │    └─ retry up to 5x       │
│    │                            │
│    └─ GeometryValidator         │
│                                 │
│  PrintCheckService              │
│    ├─ trimesh: bbox + volume    │
│    ├─ orientation scoring (6方向)│
│    └─ OrcaSlicer subprocess CLI │
│        └─ G-code → time/grams   │
└─────────────────────────────────┘
   │  STL files
   ▼
storage/files/{request_id}/result.stl
```

## 5. Data Flow — print-check

```
POST /api/print-check
  body: {request_id, machine: "bambu_a1_mini" (default), material: "PLA"}
   │
   ▼
1. Locate STL: file_storage_dir/{request_id}/result.stl
2. trimesh.load → mesh
3. bbox = mesh.bounding_box.extents
   volume = mesh.volume          # mm³
   grams = volume * 1.24 / 1000  # PLA density
4. For each of 6 orientations (±X, ±Y, ±Z aligned to bbox axes):
     score = (bottom_area * 0.5) - (overhang_area * 0.3) - (height * 0.2)
   best_orientation = argmax(score)
   needs_support = (overhang_area / total_area) > 0.05
5. OrcaSlicer subprocess:
     orca-slicer --slice 0 --load profiles/{machine}_{material}_0.2mm.json
                 --orient {best_orientation} --output /tmp/{rid}.gcode {stl}
   parse G-code header for:
     - estimated_printing_time
     - filament_used (mm)
     - filament_used_g (g)
     - support_used (bool)
6. Return {
     bbox, volume_cm3, est_grams,
     needs_support, support_area_ratio,
     recommended_orientation,
     orientation_scores: [...],
     slicer_report: {time_min, filament_g, has_support}
   }
   │
   ▼ frontend renders PrintReport.tsx
```

**Failure modes (production scenarios):**

| 场景 | v1 处理 | 测试覆盖 |
|---|---|---|
| trimesh 加载失败 (STL 损坏) | 返回 500 + "STL 解析失败,请重新生成" | unit test: 喂坏 STL |
| OrcaSlicer subprocess timeout (>60s) | kill + 返回降级响应(只有 trimesh 几何指标) | unit test: mock subprocess timeout |
| OrcaSlicer 退出码非零 | 返回降级 + log warning | unit test: mock non-zero exit |
| profile 文件不存在 | 启动时 health check 失败,部署阻断 | startup test |
| STL 体积过大 (>50MB) | 拒绝 + 提示 | unit test: 大文件 |
| Volume 为 0 / 网格非 watertight | 警告但仍返回(用户能看见问题) | unit test: 非闭合网格 |

## 6. Test Coverage Diagram

```
CODE PATH COVERAGE
==========================================
[+] backend/app/api/print_check.py (NEW)
    │
    └── handle_print_check(request_id, machine, material)
        ├── [GAP] STL not found → 404
        ├── [GAP] STL too large (>50MB) → 413
        ├── [GAP] trimesh load error → 500 with friendly message
        ├── [GAP] mesh not watertight → warning + continue
        ├── [GAP] all 6 orientations scored correctly
        ├── [GAP] OrcaSlicer subprocess success path
        ├── [GAP] OrcaSlicer subprocess timeout → degraded response
        ├── [GAP] OrcaSlicer subprocess non-zero exit → degraded
        └── [GAP] profile missing → 500 + log

[+] backend/app/agent/printing_prompts.py (NEW — split system prompts)
    │
    ├── CODEGEN_SYSTEM_PROMPT_PRINT
    │   └── [GAP] generates code that respects print constraints
    │       (壁厚 ≥ 0.8mm, fillet ≤ 短边 40%, 默认 PLA 单壁)
    │       — eval suite, see §10
    └── CODEGEN_SYSTEM_PROMPT_INDUSTRY (existing renamed)
        └── [★★ TESTED] backend/tests/test_codegen.py — keep as-is

[*] backend/app/agent/orchestrator.py (MODIFIED)
    │
    ├── generate(prompt, mode="print"|"industry")
    │   ├── [GAP] mode="print" + 装配体描述 → fallback custom
    │   ├── [GAP] mode="print" → planner uses print system prompt
    │   ├── [GAP] mode="industry" → existing behavior preserved (REGRESSION)
    │   └── [★ TESTED] 现有 retry loop (test_executor.py) — preserved
    │
    └── _detect_intent (existing) → unchanged

[*] backend/app/agent/planner.py (MODIFIED)
    │
    └── plan_new(messages, mode="print")
        ├── [GAP] mode="print": part_type="assembly" 强制改 "custom"
        ├── [GAP] mode="industry": 行为不变 (REGRESSION)
        └── [GAP] mode="print": _ASSEMBLY_KEYWORDS 短路 (skip line 76-77)

[*] backend/app/api/websocket.py (MODIFIED)
    │
    └── handle user_message
        ├── [GAP] data.mode 透传 (新字段)
        ├── [GAP] step 推送只发 5 个状态(planning|generating|executing|fixing|complete)
        └── [GAP] 装配体步骤推送移除 — REGRESSION test (确认装配体工业 mode 仍 work)

[+] frontend/src/App.tsx (REWRITTEN)
    │
    └── 单页面:ModeToggle + ChatPanel + Viewer3D + PrintReport
        └── [→E2E] 完整 user flow,见 §6 user flow coverage

[+] frontend/src/components/ModeToggle.tsx (NEW)
    │
    └── [GAP] toggle 切换发起 mode 持久化 (localStorage)

[+] frontend/src/components/PrintReport.tsx (NEW)
    │
    ├── [GAP] 渲染 6 方向打分柱状图
    ├── [GAP] OrcaSlicer 降级时显示 "切片报告暂时不可用"
    └── [GAP] QR code 含 STL 下载链接

[*] frontend/src/components/ChatPanel.tsx (MODIFIED)
    │
    └── 简化 step 显示 — 只显 5 状态文案
        └── [REGRESSION] 已有 step rendering 不破坏

USER FLOW COVERAGE
==========================================
[+] 主流程 (打印 mode)
    │
    ├── [GAP] [→E2E] 进入首页 → 输入"50x30x20 手机支架" → 点生成
    ├── [GAP] [→E2E] 30-90s 内 step 实时刷新
    ├── [GAP] [→E2E] 三维预览渲染成功
    ├── [GAP] [→E2E] PrintReport 显示 6 方向 + slicer 报告
    ├── [GAP] [→E2E] 一键下 STL 成功
    └── [GAP] [→E2E] QR 扫描跳转 STL 下载链接

[+] Mode 切换流程
    │
    ├── [GAP] 切到工业 mode → planner system prompt 切换
    └── [GAP] 切到打印 mode → 默认状态恢复

[+] 错误状态
    │
    ├── [GAP] LLM 5 次重试失败 → 友好提示 + 重新生成按钮
    ├── [GAP] WebSocket 断线 → 自动重连
    ├── [GAP] STL 生成成功但 print-check 失败 → STL 仍可下,报告显示 "暂不可用"
    └── [GAP] 用户连续点击 "生成" → 防抖

[+] 边界
    │
    ├── [GAP] prompt 空 → 前端阻止
    ├── [GAP] prompt 超长 (>10000 chars) → 截断 + warning
    └── [GAP] 同时多个 panel(若保留多 panel)→ session 隔离

[+] LLM 输出质量 (eval)
    │
    └── [GAP] [→EVAL] print mode prompt 在 20 个典型打印需求上,
        生成的代码 (a) 通过 sandbox 执行 (b) 几何指标符合打印约束
        — see §10

─────────────────────────────────
COVERAGE PLAN: 32 paths identified
  Code paths: 22 GAPs (need unit tests)
  User flows: 9 GAPs (4 need E2E, 5 unit)
  Eval: 1 prompt eval suite needed
REGRESSIONS to guard: 4 (industry mode preserved, retry loop, multi-step BE, ChatPanel step rendering)
─────────────────────────────────
```

## 7. Sprint Schedule (3 weeks, 15 working days)

### Week 1 — Backend foundation + frontend skeleton (Day 1-5)

| Day | Task | Files | Acceptance |
|---|---|---|---|
| 1 | 拆 system prompt;改 planner mode 参数 | `prompts.py`,`planner.py`,`orchestrator.py` | unit test mode 切换通过 |
| 1 | examples 库调研启动(并行) | `backend/examples/print_*.json` | 当天产出 20 条 candidate description |
| 2 | OrcaSlicer Docker 镜像 build + profile 准备 | `sandbox/Dockerfile`,`profiles/*.json` | docker run 调用 orca CLI 成功 |
| 3 | `/api/print-check` 实现 + trimesh 几何 + orientation scoring | `api/print_check.py`,`app/printing/orientation.py` | unit test 100% 分支 |
| 4 | `/api/print-check` 接 OrcaSlicer subprocess | `app/printing/slicer.py` | E2E 喂入真 STL 输出 G-code 解析 |
| 5 | examples 库整理完毕(50 个) + 入 ChromaDB | `backend/examples/`,retriever rebuild | retrieval 测试:输 "手机支架" 命中 ≥ 3 个打印件 |

### Week 2 — Frontend rewrite + integration (Day 6-10)

| Day | Task | Files | Acceptance |
|---|---|---|---|
| 6 | App.tsx 重写 + ModeToggle + 删除组件 | `App.tsx`,`ModeToggle.tsx`,删 9 个 | tsc + vite build 通过 |
| 7 | PrintReport.tsx 实现(柱状图 + slicer 报告 + QR) | `PrintReport.tsx` | mock 数据渲染正确 |
| 8 | ChatPanel 简化 step 推送 | `ChatPanel.tsx`,`useWebSocket.ts`,`websocket.py` | 5 状态文案 |
| 9 | E2E 主流程联调 + 错误状态处理 | 全栈 | playwright 主流程通过 |
| 10 | **Day 8: Owen 开始 dogfood**(平行,Bambu A1 mini 已到位) | `DOGFOOD_LOG.md` | 第 1 条日记 |

### Week 3 — 测试覆盖 + 部署 + dogfood 收尾 (Day 11-15)

| Day | Task | Files | Acceptance |
|---|---|---|---|
| 11 | 补全 unit test (print-check 全分支) | `tests/test_print_check.py` | pytest 通过,覆盖率 100% (print-check 模块) |
| 12 | E2E playwright 套件 | `frontend/e2e/` | CI 跑过 |
| 12 | LLM eval 套件(20 个打印需求 → 几何指标 + sandbox 执行) | `backend/benchmark/print_eval.py` | 通过率 ≥ 80% |
| 13 | 部署:腾讯云 + GitHub Actions + Docker Compose | `.github/workflows/deploy.yml`,`docker-compose.yml` | push main → 自动上线 |
| 14 | 灰度内测(5 个朋友 + Owen) | 内测反馈表 | ≥ 3 人完成主流程 |
| 15 | 修 bug + dogfood 总结 + Premise 1 回评 | `DOGFOOD_LOG.md`,Premise 1 评估 | sprint 结束 |

## 8. Acceptance Criteria — v1 Ship Gate

**Functional:**
- [ ] 主流程 E2E:输 "50x30x20 手机支架" → 90s 内出 STL → 下载成功
- [ ] PrintReport 显示 6 方向打分 + slicer 报告(时间 / 克数 / 是否需支撑)
- [ ] Mode 切换有效;打印 mode 拒绝装配体描述,工业 mode 保留行为
- [ ] WebSocket step 5 状态推送
- [ ] examples 库命中:Top 30 打印需求测一遍,RAG 命中率 ≥ 70%

**Quality:**
- [ ] print-check unit test:100% 分支覆盖
- [ ] 4 个 REGRESSION 测试通过(工业 mode 不破坏)
- [ ] E2E 主流程 + Mode 切换 + 错误状态共 9 条 playwright case 通过
- [ ] LLM eval 通过率 ≥ 80%
- [ ] tsc + vite build + pytest 全绿
- [ ] OrcaSlicer 60s timeout + degraded 路径已测

**Operational:**
- [ ] 腾讯云部署完成,可外网访问(IP 临时,等 ICP)
- [ ] GitHub Actions push → SSH deploy 跑通一次
- [ ] Sentry / 简单 access log 接好
- [ ] Owen 已 dogfood 3+ 次,DOGFOOD_LOG.md ≥ 5 条目

## 9. Worktree Parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| A. Prompt 拆分 + planner mode | `agent/prompts.py`,`agent/planner.py`,`agent/orchestrator.py` | — |
| B. print-check 服务 | `api/print_check.py`,`app/printing/`,`tests/test_print_check.py` | — |
| C. examples 库整理 | `backend/examples/`,retriever rebuild | — |
| D. OrcaSlicer Docker | `sandbox/Dockerfile`,`profiles/` | — |
| E. 前端重写 | `frontend/src/` 全部 | — (mock backend) |
| F. WebSocket step 简化 | `api/websocket.py`,`ChatPanel.tsx` | A |
| G. E2E + 部署 | CI/CD,`docker-compose.yml`,`.github/workflows/` | A,B,C,D,E,F |

**Lanes:**
- **Lane 1 (后端):** A → F (sequential, 共享 orchestrator/websocket)
- **Lane 2 (基建):** D (independent, Dockerfile)
- **Lane 3 (后端独立):** B (independent, 新模块)
- **Lane 4 (数据):** C (independent, JSON 文件)
- **Lane 5 (前端):** E (independent, mock backend)
- **Lane 6 (集成):** G (waits for all)

**执行顺序:** Day 1 同时启动 1+2+3+4+5,Day 6 后开始 6,Day 13 进入 G。

**Conflict 警告:** Lane 1 改 orchestrator 时若 Lane 3 也改 orchestrator(为接 print-check 后处理)→ Lane 3 应只 import,不改 orchestrator 主体。

## 10. LLM Eval Plan (新)

`backend/benchmark/print_eval.py` — 20 条打印典型需求,每条评估:
1. 代码通过 sandbox 执行
2. STL 几何符合打印约束:
   - 最薄壁厚 ≥ 0.8mm (trimesh 厚度估算)
   - 包围盒能放进 256×256×256 (Bambu A1 mini 体积)
   - watertight = True
   - 体积 > 0
3. 命中预期 part_type(若描述明确)

**测试用例样本:**
```
"放在桌面的 iPhone 14 Pro Max 横放支架"
"宽 60mm 深 40mm 高 30mm 的小型收纳盒,内部分两格"
"挂耳机的 J 形挂钩,孔径 8mm"
"键盘升降脚撑,5 度倾角"
"iPad mini 笔筒,直径 30mm 高 80mm,内置磁吸笔位"
... 共 20 条
```

**Baseline 与 gating:**
- 当前 industry mode 在工业 benchmark 上通过率(假定)~75%
- v1 上线 gate:print eval 通过率 ≥ 80%
- 持续运行:CI 在 prompt 改动时跑该 eval

## 11. NOT in scope (deferred)

| 项 | 原因 | 时间窗 |
|---|---|---|
| 微信登录 + 用户体系 | v1 验证产品而非账号 | Month 2 |
| Remix 社区机制 | 先验证基础生成,再做社区 | Month 2-3 |
| Bambu Studio deeplink | API 不公开,QR + 手动拖即可 | 等 Bambu 出 API |
| Container pool 性能优化 | Sandbox 2-3s cold start 暂可忍 | 等 PMF 信号 |
| Prompt cache | 通义千问 / Anthropic SDK cache 可后加 | Month 2 |
| 国际化 | 中文 only 是 v1 红利 | 出海再做 |
| 移动端 App | PWA 优先,native app 不做 | 永不做(?) |
| 工业插件维护 | 冷冻 `plugins/` | v2 |
| ICP 备案 | 运营动作,工程外平行 | Day 1 启动,Week 6 完成 |

## 12. What already exists (复用)

| Sub-problem | 已有 | 复用方式 |
|---|---|---|
| NL → CadQuery → STL | `Orchestrator.generate` | 直接复用 |
| LLM retry on error | `code_gen.fix_error` + 5 次 retry | 复用 |
| 静态代码分析 | `code_analyzer.analyze_code` | 复用 |
| Docker sandbox | `CadQueryExecutor` | 复用 + 新增 OrcaSlicer image |
| Three.js STL 预览 | `Viewer3D.tsx` | 复用 |
| WebSocket step 推送 | `useWebSocket.ts` + `websocket.py` | 简化 (5 状态) 后复用 |
| 历史 SQLite | `storage/history.py` | 冷冻(schema 留,UI 不暴露) |
| File serving | `/api/files/{id}/{name}` | 复用 |
| Examples retrieval | `VectorExampleRetriever` | 引擎复用,内容换 |
| 几何校验 | `GeometryValidator` (trimesh) | 复用,新增 print-check 几何指标 |

## 13. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **UI 双 mode 违 Premise 1** | High | High | Day 15 dogfood 后回评:若打印用户占比 < 70%,触发产品决策(删工业 / 接受双产品) |
| OrcaSlicer Docker build 失败或不稳定 | Medium | High | Day 2 spike,失败 fallback 到 trimesh-only(Q3 → 退到 B) |
| LLM eval < 80% 通过率 | Medium | Medium | Prompt 迭代 + examples 补充 + 关键 cases 加到 retriever |
| Owen 没买到 / 没用打印机 | Medium | Critical (阻 dogfood) | Day 1 必须下单 Bambu A1 mini,Day 5 到货 |
| ICP 备案卡到 sprint 后 | High | Medium | v1 用 IP / 海外节点先内测,正式上线等备案 |
| 50 examples 来源版权 | Low | Medium | 只反推 prompt,不复制 STL/code,生成自己的实现 |
| 工业 mode regression(改了 planner / orchestrator) | Medium | Medium | 4 个 REGRESSION 测试是 ship gate 硬条件 |

## 14. Pattern Watch (Founder 自我提醒)

来自 office hours 的 4 条 premise + 这次 plan-eng-review 的 5 个决策中,**3 次偏向"工程更全"**(Q3 C1、Q4 B3、Q5 不用 LLM 生成):
- 这是工程师反射,不是产品经理反射
- Premise 4 在响:**"产品经理大脑必须战胜工程师大脑"**
- Mitigation:Day 15 dogfood 总结时,自查 "我这周做的功能,真的 dogfood 用上了吗?"
- 若 Day 15 时打印模式用户占比 < 70% AND 工业 mode 调用次数 ≥ 30%,这是错位信号,触发产品决策

## 15. Definition of Done — 怎么算这个 sprint 成功

**工程 (硬条件):**
- ✅ §8 所有 acceptance criteria 通过
- ✅ Owen dogfood 5 条以上 DOGFOOD_LOG.md 条目
- ✅ 内测 5 人中 3 人完成主流程

**产品 (软条件,Day 15 评估):**
- 至少 1 条 dogfood 痛点是"完全没预料到"(否则说明没真 dogfood)
- 至少 1 个内测朋友愿意继续用(口头承诺,不是出于人情)
- Premise 1 回评通过(打印用户占比 ≥ 70%)

**判定:**
- 工程 + 产品都通过 → 进入 Month 2 增长(小红书 / B 站内容投入)
- 工程通过 + 产品没过 → 暂停 feature,纯 dogfood + 用户访谈 1 周
- 工程没过 → sprint 延长 1 周,绝不带 bug 上线
