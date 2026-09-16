# CAD-Agent Code Review Records

本目录保存仓库共享 `cad-agent-review` Skill 生成的审阅报告。完整调用方式见 [`USAGE.md`](USAGE.md)。

一份报告只代表特定 Git 范围和工作区状态下的证据，不替代当前源码、实时部署检查或完整产品验收。

报告必须：

- 记录 reviewer agent、review ID、审阅时间、Git 范围和工作区状态；
- 列出受影响的功能清单域和调用链；
- 区分扫描线索与确认问题；
- 将测试分类为 PASS、FAIL、BLOCKED、SKIPPED、CONTRACT_ONLY 或 REAL_ACCEPTANCE；
- 区分 logic、service、runtime 和 product 证据；
- 写明限制、未验证层和未解决风险；
- 明确声明审阅者没有修改业务代码。

文件名使用：

```text
YYYY-MM-DD-<head>-<review-id>.md
```

不同 Agent 或并发审阅必须使用不同 `review-id`，且不得覆盖已有报告。报告是追加证据，不是当前事实源。

Skill 在普通审阅中只写本目录和 `.agents/skills/cad-agent-review/**`，不会修复应用代码。
