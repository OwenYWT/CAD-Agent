# CAD-Agent Review 使用说明

`cad-agent-review` 是仓库共享的代码审阅与回归检查 Skill。它检查当前修改是否引入乱码、假实现、功能回归、接口或事件契约漂移、相关功能影响以及测试闭环，并给出基于证据的结论。

它只负责审阅，不会修改业务代码。发现问题后由开发者修复，再重新调用审阅。

## 统一规范源

所有 Agent 使用同一份规则和知识库：

```text
.agents/skills/cad-agent-review/
```

Codex、Claude 或其他 Agent 的入口只能导航到该目录，不能各自复制维护功能映射、测试映射、不变量或历史记录。

## 推荐调用

### Codex

在仓库根目录打开 Codex，明确调用 Skill：

```text
$cad-agent-review 审阅当前工作区修改。
请检查乱码、假实现、功能回归、接口和事件契约、相关功能影响以及测试闭环。
执行可用的针对性回归测试，并给出最终结论。
```

如果客户端不支持 `$skill-name` 形式，直接写“使用 cad-agent-review”即可。

### Claude Code

仓库提供 `.claude/skills/cad-agent-review/SKILL.md` 薄入口：

```text
/cad-agent-review 审阅当前工作区修改。
请检查乱码、假实现、功能回归、接口和事件契约、相关功能影响以及测试闭环。
执行可用的针对性回归测试，并给出最终结论。
```

### 其他 Agent

支持 `AGENTS.md` 的 Agent 会从仓库根入口获知规范位置。若平台不支持 Skill 或仓库指令文件，使用：

```text
先读取并严格遵循 .agents/skills/cad-agent-review/SKILL.md，
然后审阅当前工作区修改。只检查、运行可用回归并给出结论，
不要修改业务代码或应用测试。
```

## 指定审阅范围

默认审阅当前工作区的已暂存、未暂存和未跟踪文件。为了让多人结果可复现，推荐明确范围：

```text
使用 cad-agent-review 审阅 origin/main...HEAD 以及当前工作区修改。
```

也可以指定提交、功能或路径：

```text
使用 cad-agent-review 审阅 abc1234..def5678，重点检查 WebSocket 事件兼容性。
```

## 输出结论

- `PASS`：未发现实质问题，所需且可用的测试层均通过。
- `PASS_WITH_LIMITATIONS`：未发现实质回归，但真实运行时、外部服务或产品验收仍有明确限制。
- `FAIL`：发现回归、乱码、假实现、契约漂移或必要测试失败。
- `BLOCKED`：缺少取得必要证据的环境，不能声明正确。

测试证据还会区分 `PASS`、`FAIL`、`BLOCKED`、`SKIPPED`、`CONTRACT_ONLY` 和 `REAL_ACCEPTANCE`，避免把 Mock、单测或历史报告写成真实验收。

## 输出文件

审阅报告写入：

```text
docs/qa/code-review/YYYY-MM-DD-<head>-<review-id>.md
```

可复用的已验证知识写入：

```text
.agents/skills/cad-agent-review/references/review-memory/YYYY-MM-DD-<head>-<review-id>.md
```

`review-id` 使用小写短横线形式并包含 Agent 或任务标识，例如 `codex-sidebar`、`claude-auth`。同名文件已存在时必须换一个 ID，不能覆盖他人的结果。

## 团队协作流程

```text
开发者完成修改
  -> 调用 cad-agent-review
  -> Agent 只输出发现、证据和结论
  -> 开发者修复问题
  -> 再次调用 cad-agent-review
  -> 人工确认后合并
```

Skill 本身通过 Git 共同维护。维护规则见 `.agents/skills/cad-agent-review/references/multi-agent-maintenance.md`：稳定知识只在当前代码和具体证据支持时升级；Agent 专属目录不保存副本；报告和 review memory 采用追加文件，避免多人并发覆盖。
