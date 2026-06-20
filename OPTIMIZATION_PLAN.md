# datachat_new 优化开发文档：借鉴 text-to-cad / blender-mcp / forgecad

> 状态：**第一批 + 第二批已全部实现并验证（2026-06-09）。** 累计 464 后端测试绿（38 新增）+ 前端 tsc/build 绿。剩 Phase 4 可选基建、Phase 5 分发 wedge（需另行决策）。
> **优先级（2026-06-09 Owen 定）：优先学习 forgecad。** 实施从 forgecad 衍生的 C1→C2→C3 起步（见下方「实施顺序」），text-to-cad 的 A1/A3 taxonomy 顺延。理由：forgecad 与本项目同为**平台型**产品（自有 web workbench + runtime），其功能设计最对口；且 C1/C2/C3 互不依赖 A1，可独立先做。
>
> ### ✅ 已完成（2026-06-09）
> - **C1 Evidence inspect 报告** — `validation/inspect.py`（新）+ `InspectReport`/`InspectCheck` schema + orchestrator 构建 + 前端 `InspectChecks` 组件。9 测试。
> - **C2 视觉诚实** — `VisionValidationResult.is_match: bool|None`；无图/解析失败→indeterminate（不再 fail-open）；orchestrator 门 `is False` + 报告加 vision warn check；multi_step 同步。6 测试。
> - **C3 制造契约 + 数值统一** — prompt 加「默认产出=可制造原型」契约；按「设计目标(1.2mm/250mm) vs 硬下限(0.8mm/256mm)」口径澄清，零 config/rule 数值改动。
> - **A2 CADPlan brief 回传** — `plan` 字段挂双契约 + 四出口接上 + 前端 `RequirementBrief` 组件。（C3+A2 共 6 测试）
> - **A1 失败分类重构** — `agent/failure_taxonomy.py`（新，19 FailureClass + classify + render_prompt_table）；删 `_ERROR_HINTS`；`ERROR_FIX_PROMPT` 表→`{error_table}` 占位符（单一真相源）；接入 retry loop：震荡 key 归一化（修真实 bug）、infra HARD_STOP（不白烧 LLM）、gate 标注。
> - **A3 最小责任修复** — 每类 `fix_scope` 指令注入 fix_error + prompt 加「最小必要修改」原则。（A1/A3 共 28 测试）
> - 务实偏离：未加「每类 retry 预算 dict」——唯一有预算的 vision_mismatch=2 已被现有 vision_retry_count 计数器强制；taxonomy 仅记录该值。
> 三个被调研项目：
> - text-to-cad — https://github.com/earthtojake/text-to-cad（agent skills + build123d/OCCT，STEP-first，命名失败类的 repair-loop）
> - blender-mcp — https://github.com/ahujasid/blender-mcp（MCP server 驱动 Blender bpy，mesh 艺术建模）
> - forgecad-public-kit — https://github.com/KoStard/forgecad-public-kit（商业闭源 .forge.js CAD-as-code，evidence inspect，多后端）

## Context（为什么做这件事）

调研三个开源 NL→CAD/3D agent 后，目标是把它们的优势折回到本项目（NL→CadQuery→Docker→STL，FastAPI+React，Qwen）。

核心判断：本项目的 MOAT 是**自有的「生成 + retry + 沙箱 + DFM」全链路**；三个外部项目大多是「skill 寄生在别人 agent 里」「mesh 艺术建模」或「闭源引擎」。借鉴原则 = **只折回能强化这条护城河、且不动摇自有 runtime 的东西**，其余作为 wedge / 基建 / 反面教材登记清楚。

> 勘探结论（已 file:line 验证）：error "type" 散在 4 层无中央分类器；`_ERROR_HINTS`(code_gen.py:148) 与 `ERROR_FIX_PROMPT` 表(prompts.py:293) 重复维护；几何事实已算出但散落、`dfm_analysis` 是 free-form dict 且**前端忽略**；vision **fail-open**（无图/解析失败都返回 is_match=True）；prompt/config/rule 三处打印数值**不一致**(1.2mm vs 0.8mm；250mm vs 256mm)；**完全无 MCP**。

---

## 全部借鉴点清单（决策表）

| 编号 | 来源 | 借鉴点 | 现有代码锚点 | 落地 | MOAT契合 | 决策 |
|---|---|---|---|---|---|---|
| **A1** | text-to-cad | 失败分类重构（中央 FailureClass + 每类修复策略） | orchestrator.py:688-930；code_gen.py:148；prompts.py:293 | 中 | **最高** | ✅ Phase 1 |
| **A3** | text-to-cad | 最小责任修复纪律 | code_gen.py:159-190 | 低 | 高 | ✅ Phase 1 |
| **A2** | text-to-cad | prose 中间表示（CADPlan brief 回传 + 用作验证对照） | schemas.py:59；orchestrator.py:98 | 中 | 中 | ✅ Phase 3 |
| **A4** | text-to-cad | 确定性 inspect 独立于渲染 | 见 C1（重叠） | — | — | 并入 C1 |
| **A5** | text-to-cad | skill/plugin 分发 | 见 B1 | 中 | 低（让渡runtime） | ⚠️ Phase 5 wedge |
| **B1** | blender-mcp | MCP server 接口 | 无（grep 零命中） | 中(net-new) | 低-中 | ⚠️ Phase 5 wedge |
| **B2** | blender-mcp | 截图在环视觉自检 | vision_validator.py + renderer.py | — | — | ✅ **已领先，不借** |
| **B3** | blender-mcp | exec() 裸跑单工具 | — | — | — | ❌ **反面教材** |
| **B4** | blender-mcp | 素材库编排 | — | — | — | ❌ 路线不同 |
| **B5** | blender-mcp | 按需注入 prompt 片段 | prompts.py 单块静态 | 低 | 中 | ◻️ Phase 4 可选 |
| **C1** | forgecad | evidence-driven inspect 报告 | geometry_validator.py:67-96；design_analyzer.py | 中 | 高 | ✅ Phase 2 |
| **C2** | forgecad | inspect 防作弊 / 堵 fail-open | vision_validator.py:100,155；orchestrator.py:842 | 低 | 高 | ✅ Phase 2 |
| **C3** | forgecad | manufacture-realistic 默认契约 + 数值统一 | prompts.py:257；config.py:25 | 低 | 中-高 | ✅ Phase 3 |
| **C4** | forgecad | 多后端几何（Manifold/OCCT/Truck） | CadQuery 单后端 | 高 | 低 | ❌ 大改不建议 |
| **C5** | forgecad | SDF/隐式几何 | — | — | — | ❌ 路线冲突 |
| **C6** | forgecad | 多模型 CAD eval 套件 | examples/*.json(30)；e2e harness | 中 | 中 | ◻️ Phase 4 可选 |

**实施分期**：Phase 1（核心 retry 重构）→ Phase 2（inspect + 诚实）→ Phase 3（prompt/契约/brief）→ Phase 4（基建可选）→ Phase 5（分发 wedge，需另行决策）。

---

## Phase 1 — 失败分类重构 + 最小修复（A1 + A3）

**新文件 `backend/app/agent/failure_taxonomy.py`**（stdlib only，匹配现有 dataclass 风格）：

```python
class FixPath(Enum): CODE / VISUAL / HARD_STOP

@dataclass(frozen=True)
class FailureClass:
    key: str            # 稳定 id，如 "fillet_too_large"；用作震荡 guard key
    label/cause/fix_hint/fix_scope: str   # 后三者生成 prompt 表 + 运行时 hint
    fix_path: FixPath = CODE
    retry_budget: int | None = None       # None=用全局 MAX_RETRIES；vision=2
    substrings: tuple[str,...] = ()        # 对 message+traceback 大小写不敏感匹配
    gates: tuple[str,...] = ()             # 对 orchestrator gate 字面量匹配

def classify(error_type, message, traceback=None, gate=None) -> FailureClass  # 纯函数，gate 优先 → substring 顺序扫描 → unknown 兜底
def fix_hint_for(fc) -> str
def render_prompt_table() -> str          # 生成 prompts 里的 markdown 表
```

**类表 = 8 个 _ERROR_HINTS ∪ 13 行 prompt 表 ∪ gate 字面量 ∪ infra 串 ∪ geometry rule 名**（19 类）：`fillet_too_large`/`empty_stack_boolean`/`shell_failed`/`wire_not_closed`/`revolve_crosses_axis`/`loft_insufficient_wires`/`null_selector`/`disallowed_import`/`syntax_error`/`infinite_recursion`/`exec_timeout`/`static_analysis`/`geometry_invalid`/`vision_mismatch`/`vision_indeterminate`(新)/`docker_unavailable`/`docker_error`/`sandbox_no_output`/`unknown`。

`fix_scope` 示例（最小改动指令）：
- `fillet_too_large` → 「只改 .fillet()/.chamfer() 这一行：删除或把半径降到最短边的25%。不要重写其它部分。」
- `wire_not_closed` → 「只在 .extrude()/.revolve() 前插入 .close()。其它行不动。」
- `empty_stack_boolean` → 「只改创建首个实体的那几行：先 extrude/revolve 出实体再 union/cut。」
- `null_selector` → 「只改受影响的面/边选择器（如 >Z↔<Z）。其它行保持不变。」

**消除重复（单一真相源）**：
- 删 `code_gen.py:148-157` 的 `_ERROR_HINTS`；169-175 改为 `fc = classify(...); hint = fix_hint_for(fc)`。
- `prompts.py:293-306` 表改成 `{error_table}` 占位符，由 `render_prompt_table()` 在 fix_error 的 `.format()` 处填入。散文「修复原则」(308-313) 保持手写。
- 注意：prompt 表 13 行是 8 个 hints 的超集（多 ModuleNotFoundError / SyntaxError / recursion / Timeout / StaticAnalysis）；taxonomy 须取并集，生成表才无损。

**classify 接入 `_execute_with_retry`**（结构不变，每个 gate 的 fix dict 加 `gate` 键再分类）：
- **(a) 震荡 guard key 改为 `fc.key`**（替换 orchestrator.py:885 的 `error_type:message[:80]`）——修复「同一 OCCT 错误因 message 尾部坐标不同被当成不同签名」的 bug；并把 guard **从只跑 exec 路径扩展到 validation/geometry 路径**。保留 identical-code guard(915-917)。
- **(b) 每类 retry 预算**：`vision_retry_count<2`(line 833) 泛化成 `dict[str,int]` 按 `fc.key` 计数；默认 None=全局，仅 vision_mismatch=2（行为零变化）。
- **(c) fix_path 分支**：CODE→fix_error；VISUAL→fix_visual_issues；**HARD_STOP→不调 LLM 直接 break**（修复 DockerUnavailable/timeout 今天会对死沙箱白烧 5 次 LLM 的浪费，executor.py:95/133/152）。

**A3 最小责任修复（推荐 directive-only，保留全码进出）**：理由——`fix_error`+`_extract_code` 全是整文件往返，diff/patch 协议 blast radius 太大（违反 CLAUDE.md §2）。最小可行杠杆是把 `fc.fix_scope` 指令追加到现有 hint(code_gen.py:172-177)，并在 prompt 原则加一句「最小必要修改：只改与错误直接相关的行，其余逐字不变」。`fix_error(code,error,plan)` 签名不变。残留风险（仍可能改无关行）可接受、可逆；若日后实测漂移再加 old/new diff 验证器（本次范围外）。

**测试**：新 `tests/test_failure_taxonomy.py`（每个 legacy 子串/关键词/gate 字面量分类正确；render_prompt_table 含每行；classify 必返回）；扩 `tests/test_retry_intent_ratelimit.py`（震荡 key 归一化后两个不同尾部的 Standard_NullObject 现在能触发；DockerUnavailable→1 次 executor、0 次 fix_calls 证明 HARD_STOP）。

---

## Phase 2 — Evidence inspect 报告 + 视觉诚实（C1 + C2）

**C1 新文件 `backend/app/validation/inspect.py`**：
```python
def build_inspect_report(geo: GeometryValidation, dfm: DesignAnalysis | None) -> InspectReport
```
**纯聚合，只读已算值、不重算几何**。在 `_execute_with_retry` 成功路径、`geo_validation` 算完后调用（orchestrator.py:805-813），**无条件对每个成功 3D 生成构建**（不走 `_should_auto_dfm` 关键词闸门，dfm 仅作可选 enrichment）。

**新 Pydantic 模型（schemas.py，挨着 ValidationResult）**：
```python
class InspectCheck(BaseModel): name; status("pass"|"warn"|"fail"); message; source("geometry"|"dfm"|"step"|"vision")
class InspectReport(BaseModel):
    verdict("pass"|"warn"|"fail")  # = checks 里最差
    printable: bool|None; is_watertight; bounding_box; volume; min_wall_thickness
    checks: list[InspectCheck]      # forgecad 式 manifest
    print_warnings: list[str]
    design_score: int|None; dfm_violations: list[RuleViolationModel]   # 仅 DFM 跑了才填
```
**字段全部复用**：checks ← `GeometryValidation.rules` 1:1（ValidationRule(name,passed,message,severity) 映射 InspectCheck，error+!passed→fail / warning+!passed→warn / else pass）；printable/watertight/volume/bbox/wall ← GeometryValidation 字段；connectivity/floating ← executor_entry.py:204-210 已有的 disconnected-solid 计数；design_score/violations ← DesignAnalysis（DFM 跑了时）。forgecad manifest 映射：bbox=build_volume+dimension_range，wall=min_wall，connectivity/floating=watertight+断开实体数，collisions 对单实体 N/A 则**省略不造假**。

**回传**：`GenerateResponse.dfm_analysis`(schemas.py:132，前端忽略) 换成 `inspect_report: InspectReport|None`；保留 dfm_analysis 作 deprecated alias 一个版本（不破 analyze 端点契约 / test_deploy_*）；并镜像到 WS `GenerationResult`(46-56，今天连 dfm_analysis 都没有，补上闭合 REST/WS 缺口)。

**C2 视觉诚实（堵 fail-open，唯一碰边界契约的改动）**：`VisionValidationResult.is_match: bool` → `bool|None`（None=indeterminate）。
- vision_validator.py:100-104（无图）→ `is_match=None, confidence=0.0`（原 True/0.5）
- vision_validator.py:155-160（解析失败）→ `is_match=None, confidence=0.0`
- orchestrator.py:842-845（渲染为空）→ 不再静默跳过，构造 indeterminate 并分类 `vision_indeterminate`：不算通过、不触发 fix、不翻 success，往 InspectReport 加 `InspectCheck(name="vision",status="warn",...)`。
- **关键**：orchestrator.py:849 的 `if not vision_result.is_match` 改为 `if vision_result.is_match is False`（否则 `not None` 为真会误进 retry）。诚实契约：indeterminate 永不读成 is_match==True，永不抬高 success/printable。

**测试**：新 `test_inspect_report.py`（non_watertight→verdict=="fail" 且 watertight check fail；oversized→build_volume fail；printable→verdict pass/warn 且 printable True，复用 test_printability 的真 GeometryValidator）；新 `test_vision_honesty.py`（validate("x",[],code)→is_match is None；renderer 空→无 fix_visual_issues、success True、报告含 indeterminate check）。

---

## Phase 3 — 制造契约 + 数值统一 + brief 回传（C3 + A2）

**C3 数值统一（修三处打架）**：以 config 为单一真相源。当前 prompt(prompts.py:260-261) 说壁厚≥1.2mm/尺寸≤250mm，但 config(config.py:25-26) 是 0.8mm/256mm，FDM rule(default_rules.json) 是 0.8。
- **建议**：prompt 用「设计目标」(保守 1.2mm)、gate 用「硬下限」(0.8mm)，二者本就不同语义，只需在 prompt 注明「这是设计目标，硬性下限见校验」消除「看似矛盾」。尺寸统一到 256（或都写 250）。**最终取值待 Owen 拍板。**

**C3 manufacture-realistic 契约**：在 CODEGEN_SYSTEM_PROMPT 的 3D 打印块(prompts.py:257-265)前或核心规则区加「默认产出 = 可制造原型」契约：真实壁厚、贴板平面、避免悬空薄片、（装配件）紧固件/bosses/ribs 合理、期望水密单实体。纯 prompt 改动。

**A2 CADPlan brief 回传**：CADPlan(schemas.py:59) 今天只 log(orchestrator.py:98) 从不回传。把它序列化进 `GenerateResponse` / WS `GenerationResult` 一个新字段（如 `plan: CADPlan|None`），前端可展示「理解到的需求/尺寸/特征」。进阶：把 plan 的 dimensions 作为 geometry validator 的 `expected_dimensions` 对照（geometry_validator.py:156 已有该 rule，目前少有人喂值）。

**测试**：扩 prompt 相关 test（断言契约文本存在、数值一致）；schema test（plan 字段序列化往返）。

---

## Phase 4 — 基建（可选，C6 + B5）

- **C6 CAD eval 套件**：复用 `backend/examples/*.json`(30 案例) + `tests/e2e_harness.py`，建一组「prompt→期望几何特征」的 eval，跑多模型对比（已有 make_llm_client 可换 model）。产出一张类似 forgecad 的归档表。用于回归「改 prompt/retry 后一次成型率是否提升」。
- **B5 按需 prompt 注入**：codegen prompt 现在单块静态(prompts.py:25-271)。可按 part_type（2D/assembly/simple/有打印意图）注入不同片段，减少无关约束噪声。低风险增量。

---

## Phase 5 — 分发 wedge（需另行战略决策，⚠️ 与自有 runtime MOAT 相反）

- **B1 MCP server**：把 Orchestrator(generate/modify/execute_code, orchestrator.py:64/350/386) 包成 MCP server，让 Claude/Cursor 直接调做获客。net-new，seam 干净。
- **A5 skill 分发**：发一个能装进 Claude Code 的 CadQuery skill。
- ⚠️ 两者都把 agent runtime 让渡给 host——只建议作获客试验，不作产品主线。**本阶段不在本次开发范围，仅登记。**

---

## 不做 / 已领先（明确登记，避免日后误抄）

- ❌ **B3 exec() 无沙箱**：反面教材，本项目 Docker 沙箱是更成熟姿态，绝不退化。
- ❌ **B4 素材库** / **C5 SDF** / **C4 多后端**：路线冲突或大改不划算；fillet/chamfer 不可靠是 OCCT 系共病，换后端不解决（text-to-cad 与 forgecad 的 skill 都警告这点）。
- ✅ **B2 截图在环**：本项目 vision_validator + 4 角度 render 已更完整，无需借。

---

## 实施顺序与风险（**已按「优先学习 forgecad」重排**）

**第一批 — forgecad 衍生（优先）：**
1. **C1 InspectReport + inspect.py + 接 GenerateResponse/GenerationResult**（低-中风险，dfm_analysis 留 alias 保契约）。纯聚合已算值，自含，不依赖 taxonomy。前端可立刻展示结构化 inspect 报告。
2. **C2 视觉诚实**（**中风险：碰边界契约 + `is_match is False` 语义**；FakeVision 加 None 用例）。与 C1 衔接：indeterminate 作为一条 InspectCheck 进报告。
3. **C3 制造契约 + 数值统一 + A2 brief 回传**（低风险，纯 prompt/config/schema）。

**第二批 — text-to-cad 衍生（顺延）：**
4. `failure_taxonomy.py` + classify（纯模块，零风险，解锁后续）
5. 重接 fix_error + ERROR_FIX_PROMPT 到 taxonomy（低风险，签名不变）
6. classify 接入 _execute_with_retry（**中风险：动 live retry loop**；靠现有两个震荡测试兜底）。注：第 6 步会把 C2 的 `vision_indeterminate` 正式纳入 taxonomy 的 fix_path，届时回填。

**保 415 测试绿**：第 2、6 步是可能回归的，均靠扩展现有 hermetic 测试文件覆盖；全程不需 Docker/key。
> 注：原文档其余章节（Phase 1/2/3 设计细节）内容不变，仅本「实施顺序」按 forgecad 优先重排。C1/C2 原列于 Phase 2，现提为第一批。

## 验证（端到端）

- 后端：`cd backend && python -m pytest -m "not docker and not llm"`（目标维持 415 passed / 0 failed）
- 新测试：test_failure_taxonomy / test_inspect_report / test_vision_honesty + 扩 test_retry_intent_ratelimit
- 前端：`cd frontend && npx tsc --noEmit -p tsconfig.app.json && npx vite build`（InspectReport 新字段渲染）
- 需 Docker host 时（非本次）：build sandbox image，跑一次真 generate 确认 read_only 不破 CadQuery。

## 关键文件

- NEW `backend/app/agent/failure_taxonomy.py`（taxonomy / classify / render_prompt_table）
- NEW `backend/app/validation/inspect.py`（build_inspect_report）
- `backend/app/agent/orchestrator.py`（_execute_with_retry 688-930：classify 接入、震荡 key、HARD_STOP、inspect 构建点）
- `backend/app/agent/code_gen.py`（fix_error 159-190：删 _ERROR_HINTS、注入 fix_scope）
- `backend/app/agent/prompts.py`（ERROR_FIX_PROMPT 表→占位符；257-265 制造契约+数值统一）
- `backend/app/validation/vision_validator.py`（64-69/100-104/155-160 fail-open→indeterminate）
- `backend/app/models/schemas.py`（InspectReport/InspectCheck；GenerateResponse:132 + GenerationResult:46-56 + plan 字段）
- `backend/app/config.py`（25-26 数值统一）
