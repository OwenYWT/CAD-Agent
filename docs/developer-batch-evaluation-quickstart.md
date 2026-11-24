# 开发者批量评测快速开始

以下命令均在 PowerShell 中执行。批量评测不需要启动前端；需要数据库、Temporal、Podman、CAD-Agent Worker 和 API 已正常可用。

## 第一步：启动 Worker

打开第一个终端：

```powershell
cd D:\project\projectcad\CAD-Agent\backend
python -X utf8 -m app.workers.run --evaluation-mode observe
```

保持该终端运行。看到 Worker 开始轮询后再继续。

## 第二步：启动 API

打开第二个终端：

```powershell
cd D:\project\projectcad\CAD-Agent\backend
python -X utf8 -m app.run --host 127.0.0.1 --port 8000 --evaluation-mode observe
```

保持该终端运行。批量命令主要依赖 Worker、Temporal 和数据库；API 保持运行便于同时查看系统状态。

## 第三步：切换到后端目录

打开第三个终端：

```powershell
cd D:\project\projectcad\CAD-Agent\backend
```

## 第四步：创建专用评测文档并获取 UUID

每个批量评测工作区使用一个独立文档。执行下面的一行命令：

```powershell
python -X utf8 -c "import asyncio; from uuid import uuid4; from app.domain.identity import local_anonymous_principal; from app.services.durable_submission import ensure_workspace_identity; session_id='benchmark-'+uuid4().hex; panel_id='panel-'+uuid4().hex; identity=asyncio.run(ensure_workspace_identity(local_anonymous_principal(), session_id=session_id, panel_id=panel_id, title='CADTest benchmark', user_id=None)); print('DOCUMENT_ID='+str(identity.branch_id)); print('PROJECT_ID='+str(identity.project_id)); print('BASE_REVISION_ID='+str(identity.head_revision_id))"
```

复制输出中的 `DOCUMENT_ID`。批量命令使用的是这个值，不是任务 ID、Workflow ID 或 Project ID。

## 第五步：检查数据集

先验证数据集完整性：

```powershell
python -X utf8 -m benchmark.cad_test_batch dataset-check `
  --dataset-root "D:\project\projectcad\CAD_test"
```

看到 `"status": "validated"` 后继续。数据集代码由 CAD-Agent 自己执行，`CAD_test` 目录只提供数据和真值文件。

## 第六步：先跑一个 Case

把 `<DOCUMENT_ID>` 替换为第四步输出的 UUID：

```powershell
python -X utf8 -m benchmark.cad_test_batch live `
  --dataset-root "D:\project\projectcad\CAD_test" `
  --cases ctb-00000007 `
  --repeat 1 `
  --document-id <DOCUMENT_ID>
```

命令结束后会打印报告路径。即使命令返回退出码 `1`，也要先查看报告；这表示评测中有失败，不代表报告没有生成。

## 第七步：查看单 Case 报告

报告目录类似：

```text
D:\project\projectcad\CAD-Agent\backend\benchmark\reports\cad_test\20261125T......\
```

打开其中的：

```text
report.html
```

`report.json` 是机器可读原始报告，`report.html` 是开发者阅读报告。HTML 中可以查看最终通过、CADTest、Live 过程、工具调用、阶段耗时、时间线、失败分类和 Case 详情。

## 第八步：运行完整数据集

确认单 Case 流程正常后，再运行全部 Case。下面以每个 Case 重复 5 次为例：

```powershell
python -X utf8 -m benchmark.cad_test_batch live `
  --dataset-root "D:\project\projectcad\CAD_test" `
  --cases all `
  --limit 100 `
  --repeat 5 `
  --document-id <DOCUMENT_ID>
```

完整报告仍会写入：

```text
D:\project\projectcad\CAD-Agent\backend\benchmark\reports\cad_test\<run-id>\report.json
D:\project\projectcad\CAD-Agent\backend\benchmark\reports\cad_test\<run-id>\report.html
```

## 第九步：报告查看顺序

打开 `report.html` 后按以下顺序查看：

1. **最终通过**：严格最终门槛，必须同时满足 Live 完成、产物交付、CADTest 通过和原生产物验证通过。
2. **Case 成功覆盖率**：一个 Case 至少有一次严格通过。
3. **稳定成功率**：一个 Case 的所有重复运行都严格通过。
4. **CADTest 记录覆盖率**：只在已经有可交付产物的运行中计算 CADTest 记录比例。
5. **难度分层**：比较 `basic`、`combined`、`complex` 的退化情况。
6. **失败分类和 Case 详情**：定位最早失败的工具调用、阶段耗时和完整 Live 证据。

## 可选：只对已有 STEP 文件评分

不提交新 Workflow，只评分已有 STEP：

```powershell
python -X utf8 -m benchmark.cad_test_batch score `
  --dataset-root "D:\project\projectcad\CAD_test" `
  --case ctb-00000007 `
  --step "D:\path\to\result.step" `
  --out ".\benchmark\reports\cad_test\manual-ctb-00000007"
```

## 常见注意事项

- `--document-id` 必须是专用评测文档的 `DOCUMENT_ID`，不要填任务 ID。
- 同一个批量运行不要并发使用同一文档；评测工作区只有一个预期分支头。
- `--document-id` 不能和 `--project-id`、`--branch-id`、`--base-revision-id` 同时使用。
- 每次完整批量测试建议新建一个专用评测文档，避免历史分支头变化影响结果。
- 正常前端使用不需要这些 UUID；这些参数只用于开发者批量评测。
