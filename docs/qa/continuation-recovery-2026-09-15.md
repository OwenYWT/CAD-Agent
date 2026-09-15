# 旧“继续”任务恢复修复

日期：2026-09-15。基线：`0fddc3d`。本次处理旧任务丢失具体目标后的恢复，不代表手机壳适配或制造验收通过。

## 已确认的根因

线上任务 `8df61ad0-1536-4787-b225-b27eece7c825` 的幂等键指向旧任务 `b27c266e-f4a7-4276-a8e8-41b4b1f52fdd` 的重试。两者持久目标都是带旧版上下文包装的“继续”。

同项目、同线程更早的任务 `2e1d6db1-eb42-487c-82c0-737a8bb574c4` 才包含“我想做一个手机壳，适配 iphone 17 pro max”的具体目标；该任务因 `ProviderQuotaError` 失败。原始目标仍可通过服务器历史接口恢复。

本次 Worker 栈中，模型服务实际返回：没有可用的基础模型测量事实，也没有具体零件描述、尺寸或需要继续的设计目标。生成器耗尽纠正次数后，用通用 `ValueError` 包装了原因。没有最终候选，不是几何内核已经生成后保存失败。

## 修改范围

- 识别独立的“继续”“重试”“再试一次”及英文命令，兼容旧版上下文包装；包含明确目标的“继续添加直径 5 mm 的孔”仍按正常需求处理。
- 旧空目标失败任务不再提供直接重试。仅从当前线程的用户历史中提取具体需求，显示“恢复历史需求并确认”；没有可用记录时要求补充目标。
- 恢复后进入现有需求/修改确认卡，用户确认前不提交，不读取助手建议作为原需求，不改写旧任务。参数与版本任务仍返回各自入口。
- 统一提交入口在数据库写入、任务派发和 Provider 调用前拒绝空目标命令。直接重试此类任务返回现有输入校验协议 `HTTP 422`。
- 操作计划生成失败保留最后一次拒绝原因和异常链，诊断文本最多追加 1000 字符。

## 验证记录

| 验证 | 结果 |
| --- | --- |
| 修复前回归复现 | 后端 5 个新增场景失败：空目标未被拦截、具体拒绝原因丢失 |
| 后端计划生成、检查与重试专项 | 29 项通过 |
| 后端隔离回归 | 1442 项通过、158 项跳过、1 项未选执行 |
| 前端测试 | 147 项通过，包含旧任务恢复、无可恢复历史和具体修改命令边界 |
| 前端 lint / 生产构建 | 通过；保留现有大资源块提示 |
| 独立代码复核 | 未发现阻塞问题 |
| 真实数据库 / HTTP / WebSocket / Provider / FreeCAD / 浏览器 | 通过：旧任务拒绝直接重试（422），恢复历史需求并确认，新任务目标与工程依据正确持久化，真实候选生成成功，旧任务未改写，页面异常 0 |

命令：

```bash
cd backend
APP_ENVIRONMENT=test DURABLE_CONTROL_PLANE_ENABLED=false python -m pytest -m 'not docker and not llm and not fusion_e2e' -q
cd ../frontend
npm run lint
npm run build
node --test --experimental-strip-types tests/*.test.ts
```

专项入口：[continuation_recovery_browser.py](../../backend/tests/e2e/continuation_recovery_browser.py)。它在隔离本地项目中调用旧版底层任务入口复现真实拒绝，再通过新界面恢复明确的板件需求。未替换接口响应或模型结果；该板件验证不能替代手机壳适配验证。

真实旧输入复现任务：`f225b5c0-af01-4fc4-9e34-89e7468cb7a5`；恢复后成功候选任务：`decbc6e5-16a7-4cb8-b679-4f7906b10815`。原始任务 JSON、浏览器截图和 trace 保存在本机私有 `/private/tmp/cad-continuation-e2e-20260915-v2/`，不提交登录信息或完整 trace。首轮验收脚本错误预期 400，实际接口按既有协议返回 422；修正断言后复用同一真实失败任务完成验收。

本次没有修改线上失败任务的原始需求或状态，也没有替用户确认手机尺寸来源。恢复原需求后，仍须确认工程依据或明确仅生成概念外形。

## GitHub 与腾讯云发布

- [PR #6](https://github.com/OwenYWT/CAD-Agent/pull/6) 已合入 `main`，提交 `8fdbec1c566653babe9c5d6aba28dde2b1222514`；与构建源码 `a8cd9569e35d67354b56dcb8259244503aca7f00` 的 Git tree 相同。
- [GitHub CI](https://github.com/OwenYWT/CAD-Agent/actions/runs/34918744360) 的前端、后端和真实 MCAD 运行时 / 持久控制面端到端三项检查全部通过。
- 发布目录：`/www/releases/cad-20260915-continuation-a8cd956/deploy/tencent`。后端 781 个文件、沿用的 CAD 运行时 467 个文件及前端 9 个文件通过 SHA-256 核对，无不一致。
- 切换前确认没有未结束任务，备份数据库和旧配置到 `/www/backups/cad-20260915-continuation-a8cd956`；数据库备份已通过目录读取校验。本次只更新 API、Worker 和前端，没有执行数据库迁移或修改产物存储。
- 旧镜像及 `/www/releases/cad-20260913-task-state-7d1dd5e/deploy/tencent` 保留，可恢复旧应用；数据库、Temporal 与 MinIO 持续运行。
- 公开 HTTPS `/ready` 返回 `ready`，PostgreSQL、对象存储、Temporal 和两类 Worker 探测正常。
- 两个原测试账户重新密码登录通过，用户身份、共享文档 Head、状态版本、编辑权限和 FCStd / 状态 / 网格文件哈希与发布前一致。
- 线上浏览器打开用户报告的同一任务，确认没有执行动画、没有直接重试空目标入口；“恢复历史需求并确认”正确显示原手机壳需求，未确认依据时执行按钮禁用。旧任务状态、需求及事件序号保持不变；页面异常 0。

发布清单、基线、截图及完整核验记录保存在本机私有 `/Users/wentao/.local/share/cad-agent/continuation-deployment-20260915/`。GitHub GraphQL 合并请求曾出现 EOF，先查明 PR 仍为 OPEN，再通过 REST 合并并核对成功；没有把连接失败当作已发布。
