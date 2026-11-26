# 逐项功能对照

目录基准为仓库功能清单。所有行均对照原有入口、服务源码、数据合同。实测栏给出本轮确有运行记录的场景。部分实测表示只覆盖说明中的子路径。原清单标记保留产品原有的可选、受限及待验收边界。证据键来自同目录的 regression-evidence.json，另列文件名的项目使用对应报告。

共 237 项。实测 149 项，外部待实测 16 项，部分实测 31 项，基线对照 41 项。

| 功能 ID | 原有功能 | 原清单标记 | 本轮验证 | 场景及证据 |
| --- | --- | --- | --- | --- |
| 01.01 | 密码登录 | 已接入 | 实测 | 浏览器账号余额、切换身份、刷新凭据及 WebSocket 重新连接。billing |
| 01.02 | 邀请码注册 | 已接入 | 实测 | 真实邀请码注册、登入/登出、刷新及临时账号注销后登录被拒。accounts |
| 01.03 | 登录状态恢复 | 已接入 | 实测 | 浏览器账号余额、切换身份、刷新凭据及 WebSocket 重新连接。billing |
| 01.04 | 刷新登录凭据 | 已接入 | 实测 | 浏览器账号余额、切换身份、刷新凭据及 WebSocket 重新连接。billing |
| 01.05 | 退出登录 | 已接入 | 实测 | 浏览器账号余额、切换身份、刷新凭据及 WebSocket 重新连接。billing |
| 01.06 | 注销账户 | 已接入 | 实测 | 真实邀请码注册、登入/登出、刷新及临时账号注销后登录被拒。accounts |
| 01.07 | 注册邀请码管理 | 已接入 | 实测 | 真实邀请码注册、登入/登出、刷新及临时账号注销后登录被拒。accounts |
| 01.08 | 验证码账户流程 | 可选 / 接口 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 01.09 | 短信发送接入 | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 02.01 | 新建设计 | 已接入 | 实测 | 同一空对话重命名，创建第二线程、切回、关闭、刷新核对。after_threads |
| 02.02 | 多线程切换 | 已接入 | 实测 | 同一空对话重命名，创建第二线程、切回、关闭、刷新核对。after_threads |
| 02.03 | 对话重命名 | 受限 | 实测 | 同一空对话重命名，创建第二线程、切回、关闭、刷新核对。after_threads |
| 02.04 | 关闭工程线程 | 已接入 | 实测 | 同一空对话重命名，创建第二线程、切回、关闭、刷新核对。after_threads |
| 02.05 | 历史项目列表 | 已接入 | 实测 | 分页查找历史项目、恢复视口和已提交任务。after_parity / layout |
| 02.06 | 历史上下文恢复 | 已接入 | 实测 | 分页查找历史项目、恢复视口和已提交任务。after_parity / layout |
| 02.07 | 删除历史项目 | 受限 | 实测 | 删除本次新建的测试历史后核对列表和服务器的项目状态。history_delete |
| 02.08 | 项目流程总览 | 已接入 | 实测 | 浏览器从项目流程进入结构仿真并查看已完成状态。fea |
| 03.01 | 四区工作台 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.02 | 收起与展开 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.03 | 布局保留现场 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.04 | 窄屏操作 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.05 | 检查器内容排序 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.06 | 专题入口 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.07 | 统一操作入口 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.08 | 连接状态提示 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 03.09 | 中英文切换 | 受限 | 实测 | 对同一原生修订切换中英文，核对控件、输入和版本身份。product |
| 03.10 | 操作辅助 | 已接入 | 实测 | 对话预览、工程工具、抽屉、键盘关闭、界面状态与七档屏宽；部分入口在展开后核验。chat_native / layout / product |
| 04.01 | 自然语言需求 | 已接入 | 实测 | 对比两版 WebSocket 需求、经确认的尺寸来源与制造设置以及持久化任务。model-runs.json |
| 04.02 | 执行前需求卡 | 已接入 | 实测 | 对比两版 WebSocket 需求、经确认的尺寸来源与制造设置以及持久化任务。model-runs.json |
| 04.03 | 尺寸来源记录 | 已接入 | 实测 | 对比两版 WebSocket 需求、经确认的尺寸来源与制造设置以及持久化任务。model-runs.json |
| 04.04 | 开孔与装配依据 | 已接入 | 实测 | 实际输入配合说明和测量来源，取消后核对持久化请求。cancel |
| 04.05 | 概念外形确认 | 已接入 | 部分实测 | 确认同一真实设计默认保持未勾选，几何任务按用户定义尺寸提交。model-runs.json |
| 04.06 | 制造设置 | 已接入 | 实测 | 对比两版 WebSocket 需求、经确认的尺寸来源与制造设置以及持久化任务。model-runs.json |
| 04.07 | 设计简报 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 04.08 | 关键缺项确认 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 04.09 | 依据随任务保存 | 受限 | 实测 | 对比两版 WebSocket 需求、经确认的尺寸来源与制造设置以及持久化任务。model-runs.json |
| 05.01 | 首次生成 | 已接入 | 实测 | 浏览器首建、真实规划和生成、自动修复、验收、历史回放。model-runs.json |
| 05.02 | 继续修改 | 已接入 | 实测 | 自然语言改厚及选定原生拉伸特征后修改均实际执行并保存。独立重开 FCStd/STEP，60 × 40 × 10 mm；范围错误报拒且原件保留。natural_edits |
| 05.03 | 指定修改对象 | 已接入 | 实测 | 自然语言改厚及选定原生拉伸特征后修改均实际执行并保存。独立重开 FCStd/STEP，60 × 40 × 10 mm；范围错误报拒且原件保留。natural_edits |
| 05.04 | 修改前确认 | 已接入 | 实测 | 修改前确认尺寸来源，读取真实提交绑定的目标、依据和基线。natural_edits |
| 05.05 | 需求拆解与计划 | 已接入 | 实测 | 浏览器首建、真实规划和生成、自动修复、验收、历史回放。model-runs.json |
| 05.06 | 过程展示 | 已接入 | 实测 | 浏览器首建、真实规划和生成、自动修复、验收、历史回放。model-runs.json |
| 05.07 | 按需查询模型 | 已接入 | 实测 | 实际模型服务查询现有原生拉伸和草图；改厚 2 mm 后复核其余草图参数、约束及几何。test-summary.json → direct_provider_and_kernel_cases |
| 05.08 | 自动修复 | 受限 | 部分实测 | 浏览器首建、真实规划和生成、自动修复、验收、历史回放。model-runs.json |
| 05.09 | 检查驱动修改 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 05.10 | 工程结果上下文 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 05.11 | 建模案例检索 | 已接入 / 可选 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 05.12 | 结果与历史对话 | 已接入 | 实测 | 浏览器首建、真实规划和生成、自动修复、验收、历史回放。model-runs.json |
| 06.01 | 五种权威状态 | 已接入 | 实测 | 真实运行、候选、保存、失败和取消；刷新后对应服务端任务。cancel / chat_native / other-model-runs.json |
| 06.02 | 后台持续执行 | 已接入 | 实测 | 真实运行、候选、保存、失败和取消；刷新后对应服务端任务。cancel / chat_native / other-model-runs.json |
| 06.03 | 取消任务 | 受限 | 实测 | 真实浏览器取消请求，核对 PostgreSQL 终态、候选和文件。cancel |
| 06.04 | 失败可读提示 | 已接入 | 实测 | 真实运行、候选、保存、失败和取消；刷新后对应服务端任务。cancel / chat_native / other-model-runs.json |
| 06.05 | 原需求重试 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 06.06 | 按类型恢复 | 已接入 | 实测 | 真实失败草图诊断、可接管基线及原生恢复提交。product |
| 06.07 | 未知受理恢复 | 已接入 | 实测 | 真实的服务端已受理后丢失回执，通过同一请求重查，只落一个任务。fault_replay |
| 06.08 | 过期任务保护 | 已接入 | 实测 | 取消意图落库后，迟到的 worker 完成事件无法推进任务。test-summary.json → postgres_tests → test_cancellation_intent_blocks_late_worker_completion |
| 06.09 | 故障详情 | 已接入 | 实测 | 真实运行、候选、保存、失败和取消；刷新后对应服务端任务。cancel / chat_native / other-model-runs.json |
| 06.10 | 管理员交接 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 07.01 | FreeCAD 原生模型 | 已接入 | 实测 | 独立 FreeCAD 重开 FCStd/STEP，校验尺寸、体积、实体数与原生建模树。model-runs.json |
| 07.02 | 原生基础草图 | 已接入 | 实测 | 独立 FreeCAD 重开 FCStd/STEP，校验尺寸、体积、实体数与原生建模树。model-runs.json |
| 07.03 | 拉伸与挖槽 | 已接入 | 实测 | 原生拉伸、槽、孔、圆角和倒角实际执行并重新打开。kernel-tests.xml |
| 07.04 | 孔特征 | 已接入 | 实测 | 原生拉伸、槽、孔、圆角和倒角实际执行并重新打开。kernel-tests.xml |
| 07.05 | 圆角与倒角 | 受限 | 实测 | 原生拉伸、槽、孔、圆角和倒角实际执行并重新打开。kernel-tests.xml |
| 07.06 | 原生检查与重算 | 已接入 | 实测 | 独立 FreeCAD 重开 FCStd/STEP，校验尺寸、体积、实体数与原生建模树。model-runs.json |
| 07.07 | CadQuery 代码建模 | 已接入 | 实测 | 真实 CadQuery 实体转原生装配 BOM，浏览器显示来自 CAD 的 SVG。kernel-tests.xml / product |
| 07.08 | 二维制图 | 已接入 | 实测 | 真实 CadQuery 实体转原生装配 BOM，浏览器显示来自 CAD 的 SVG。kernel-tests.xml / product |
| 07.09 | 确定性代码执行 | 已接入 | 部分实测 | 真实 CadQuery 实体转原生装配 BOM，浏览器显示来自 CAD 的 SVG。kernel-tests.xml / product |
| 07.10 | 建模格式范围 | 受限 | 实测 | 独立 FreeCAD 重开 FCStd/STEP，校验尺寸、体积、实体数与原生建模树。model-runs.json |
| 08.01 | 特征树 | 已接入 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.02 | 特征属性 | 已接入 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.03 | 手动尺寸编辑 | 受限 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.04 | 输入与单位校验 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 08.05 | 精确修改 | 已接入 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.06 | 草稿保护 | 已接入 | 实测 | 草稿和未确认请求的保护经真实任务、跨面板及折叠往返核对。fault_replay |
| 08.07 | AI 运行中保留草稿 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 08.08 | 草稿处理 | 受限 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.09 | 编辑租约 | 已接入 | 实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 08.10 | 用途与设计意图 | 已接入 | 实测 | 实际分支继承原生特征的用途标注及版本。branch |
| 08.11 | 内核详情查询 | 已接入 | 部分实测 | 真实参数与租约、提交前草稿、撤销重做、折叠恢复、原生内核属性。manual_parameters / layout / product |
| 09.01 | 草图读取 | 已接入 | 实测 | 实际失败草图的停靠与接管，展开草图视图，核验恢复和过期基线保护。product |
| 09.02 | 本地预览 | 受限 | 部分实测 | 实际失败草图的停靠与接管，展开草图视图，核验恢复和过期基线保护。product |
| 09.03 | 拖动圆尺寸 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 09.04 | 数值约束编辑 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 09.05 | 云端最终求解 | 已接入 | 实测 | 实际失败草图的停靠与接管，展开草图视图，核验恢复和过期基线保护。product |
| 09.06 | 求解失败与过期保护 | 已接入 | 实测 | 实际失败草图的停靠与接管，展开草图视图，核验恢复和过期基线保护。product |
| 09.07 | 完整草图绘制器 | 未完成 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 10.01 | 三维预览 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.02 | 相机操作 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.03 | 二维预览 | 已接入 | 实测 | 实际工程图 SVG 缩放、适应视图及窄屏调整。product |
| 10.04 | 视图身份 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.05 | 同版本内容 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.06 | 部件选择联动 | 受限 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.07 | AI 对象定位 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.08 | 精度切换与场景缓存 | 已接入 | 实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.09 | 场景异步加载 | 已接入 | 部分实测 | 对照同一画布、像素级相机、缓存、LOD、选择和历史修订。layout / chat_native |
| 10.10 | 加载异常提示 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 10.11 | 任意孔/面拾取 | 未完成 | 部分实测 | 原生圆边语义选择器在真实内核操作中验证。kernel-tests.xml |
| 10.12 | 三维检查标注 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 11.01 | 多零件生成 | 已接入 | 部分实测 | 经真实内核生成两体原生测试模型，验证可独立编辑；此项没有对自然语言多件规划单独验收。collaboration |
| 11.02 | 零件清单 | 已接入 | 实测 | 本次发布包含原生 BOM、源修订和实际零件统计。release_http / release_browser |
| 11.03 | 零件级修改 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 11.04 | 创建原生实例 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 11.05 | 实例位置编辑 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 11.06 | 原生 BOM | 已接入 | 实测 | 本次发布包含原生 BOM、源修订和实际零件统计。release_http / release_browser |
| 11.07 | 装配工程能力 | 未完成 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 12.01 | 几何检查 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.02 | 需求尺寸核验 | 受限 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.03 | 视觉一致性检查 | 可选 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.04 | 可制造性检查 | 受限 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.05 | 打印适用性提示 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 12.06 | 手动运行检查 | 已接入 | 实测 | 浏览器发起异步工程检查，刷新后核对冻结工艺、所用修订及检查结果。product |
| 12.07 | 检查门禁 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.08 | 检查对象追溯 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.09 | 中间与最终证据 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.10 | 修复记录 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 12.11 | 来源完整性校验 | 已接入 | 实测 | 读取实际验证记录和选定 evidence，复核产物哈希、修订归属和 CAD 量测；视觉结果按实际 outcome 记录。model-runs.json |
| 13.01 | 内置工艺规则 | 受限 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.02 | 规则启停 | 已接入 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.03 | 阈值调整 | 已接入 / 接口 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.04 | 自定义规则集 | 接口 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.05 | 量测与人工提示区分 | 受限 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.06 | DFM 分析摘要 | 已接入 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.07 | 规则快照 | 已接入 | 部分实测 | 真实 DFM 检查、规则快照、自定义阈值和设置入口；网络失败与恢复走实际后端。product |
| 13.08 | 工艺与材料查询 | 已接入 | 实测 | 两版材料目录与同一工艺推荐在真实浏览器和真实服务返回中一致。knowledge |
| 13.09 | 工艺推荐 | 已接入 | 实测 | 两版材料目录与同一工艺推荐在真实浏览器和真实服务返回中一致。knowledge |
| 13.10 | 供应商名录 | 受限 | 部分实测 | 两版供应商目录与服务返回条目完全一致；未在此场景内新增供应商。knowledge |
| 13.11 | 知识图谱管理 | 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 13.12 | 内置标准件参数 | 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 14.01 | 候选预览 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 14.02 | 变更明细 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 14.03 | 风险与检查 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 14.04 | 接受候选 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 14.05 | 提交为当前版本 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 14.06 | 拒绝候选 | 已接入 | 实测 | 通过浏览器拒绝首次导入候选，数据库确认原始分支第零代。history_delete |
| 14.07 | 要求修改 | 已接入 | 实测 | 原版、新版均对真实 STEP 导入候选提出修改；原分支头和代次保持。imports |
| 14.08 | 继续处理提交失败 | 已接入 | 实测 | 在真实接受后断开提交连接；分支头和代次不变，恢复网络后同一候选提交一次。commit_retry |
| 14.09 | 候选与草稿隔离 | 已接入 | 实测 | 候选可见、审阅草稿零写入、显式接受/提交后分支只前进一代。chat_native / model-runs.json / manual_parameters |
| 15.01 | 历史版本查看 | 已接入 | 实测 | 切换已保存与历史修订，核对图形对比、相机、缓存与身份。layout / product |
| 15.02 | 版本差异 | 已接入 | 实测 | 切换已保存与历史修订，核对图形对比、相机、缓存与身份。layout / product |
| 15.03 | 恢复历史版本 | 已接入 | 实测 | 从历史生成新候选、确认并审核，新修订恢复原有参数，历史原件保留。restore |
| 15.04 | 当前提交回退 | 已接入 | 部分实测 | 真实 PostgreSQL 验证接受、提交、回滚和审计；本次未从 UI 操作当前提交的回滚。test-summary.json → postgres_tests → test_accept_commit_and_rollback_are_distinct_audited_operations |
| 15.05 | 创建与切换分支 | 已接入 | 实测 | 真实派生分支、重复提交返回原操作、新分支保留特征标注。branch |
| 15.06 | 分支语义比较 | 已接入 | 实测 | 真实双分支参数合并、同参冲突、幂等与目标保护。merge |
| 15.07 | 独立参数合并 | 受限 | 实测 | 真实双分支参数合并、同参冲突、幂等与目标保护。merge |
| 15.08 | 采用来源几何 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 15.09 | 双方版本保护 | 已接入 | 实测 | 真实双分支参数合并、同参冲突、幂等与目标保护。merge |
| 15.10 | 自动参数重放 | 受限 / 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 16.01 | 项目邀请 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.02 | 共享文档 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.03 | 成员管理 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.04 | 版本与特征评论 | 已接入 | 实测 | 评论真实落库后丢回执，重试核对只有一条评论。product |
| 16.05 | 在线状态 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 16.06 | 实时文档同步 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.07 | 编辑并发保护 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.08 | 操作队列与记录 | 已接入 | 部分实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.09 | 撤权生效 | 已接入 | 实测 | 两个 Chromium 身份编辑及审核、所有者提交、降权清理租约、已连接页面实时撤权。collaboration |
| 16.10 | 跨项目访问限制 | 已接入 | 部分实测 | 实际只读成员的下载/发布/提交被拒；不同账户的计费状态隔离。billing / release_http / collaboration |
| 17.01 | 选取分析对象 | 已接入 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.02 | 材料输入 | 已接入 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.03 | 载荷与约束 | 受限 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.04 | 网格与求解 | 已接入 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.05 | 结果可视化 | 已接入 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.06 | 数值与平衡核验 | 已接入 | 部分实测 | 实际求解返回应力位移；镜头显示倍率变化后原值相同。fea |
| 17.07 | 分析记录与下载 | 已接入 | 实测 | 真实浏览器选择分析、输入材料与边界条件；CalculiX 求解、场显示、证据包下载。fea |
| 17.08 | 分析适用范围 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 18.01 | 原生轮廓计算 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.02 | 刀具设置 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.03 | 加工参数 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.04 | GRBL 程序 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.05 | 刀路播放 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.06 | 加工报告 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.07 | 失败与证据 | 已接入 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 18.08 | 加工范围 | 受限 | 实测 | 实际计算三层外轮廓、192 段刀路，浏览器回放、下载 GRBL 并校验哈希，短刀真实失败。cam |
| 19.01 | 产物列表 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.02 | CAD 文件下载 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.03 | 图片与源码 | 已接入 | 部分实测 | 核对该次实际 visual_render 文件和工程源码/参数入口；未单测所有图像格式。model-runs.json / product |
| 19.04 | 修订一致性 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.05 | 命名发布 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.06 | 原生 BOM 导出 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.07 | 工程证据随发布 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.08 | 发布归档 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.09 | 发布追溯 | 已接入 | 实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.10 | 发布输入保护 | 已接入 | 部分实测 | 检查原生文件、不可变 BOM、发布清单、工程任务修订与发布包 SHA-256。model-runs.json / release_http / release_browser |
| 19.11 | BOM 范围 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 20.01 | 客户端下载 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 20.02 | 本地配对 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.03 | 连接管理 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.04 | 选择发布交付 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.05 | 主动领取与恢复 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.06 | 本地文件落盘 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.07 | 交付结果确认 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.08 | 已有文件保护 | 已接入 | 实测 | 真实本地 CLI 配对、15 个发布文件落盘、真实租约过期恢复与哈希校验。bridge |
| 20.09 | 平台与目标范围 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 21.01 | CAD | 可选 | 部分实测 | 实际 Podman 运行本地 CAD/STEP、DXF、隐式 CAD；仅本次合同中列举的操作。kernel-tests.xml |
| 21.02 | DXF | 受限 | 部分实测 | 实际 Podman 运行本地 CAD/STEP、DXF、隐式 CAD；仅本次合同中列举的操作。kernel-tests.xml |
| 21.03 | Implicit CAD | 可选 | 部分实测 | 实际 Podman 运行本地 CAD/STEP、DXF、隐式 CAD；仅本次合同中列举的操作。kernel-tests.xml |
| 21.04 | CAD Viewer | 可选 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 21.05 | step.parts | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 21.06 | URDF | 可选 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 21.07 | SRDF / MoveIt | 可选 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 21.08 | SDF / Gazebo | 可选 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 21.09 | SendCutSend | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 21.10 | FDM G-code | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 21.11 | Bambu Labs | 可选 / 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.01 | Onshape 发布 | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.02 | Onshape 发布追踪 | 可选 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.03 | Onshape 文档查询 | 可选 / 接口 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.04 | Fusion 连接状态 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.05 | Fusion 上下文 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.06 | Fusion 受控修改 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.07 | Fusion 预览与批准 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.08 | Fusion 保存与导出 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.09 | Fusion 恢复控制 | 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 22.10 | Autodesk APS 浏览 | 可选 / 待验收 | 外部待实测 | 需要对应服务、短信目的地或实机；本轮核对集成代码和接口定义。before-contract.json / after-contract.json |
| 23.01 | 建模 API | 接口 | 部分实测 | 真实浏览器和 API 调用所覆盖的建模、任务、文档、工程、发布接口；全量 HTTP 合同另做哈希比对。model-runs.json / regression-evidence.json |
| 23.02 | 批量生成 | 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 23.03 | 任务控制 API | 接口 | 部分实测 | 真实浏览器和 API 调用所覆盖的建模、任务、文档、工程、发布接口；全量 HTTP 合同另做哈希比对。model-runs.json / regression-evidence.json |
| 23.04 | 文档与版本 API | 接口 | 部分实测 | 真实浏览器和 API 调用所覆盖的建模、任务、文档、工程、发布接口；全量 HTTP 合同另做哈希比对。model-runs.json / regression-evidence.json |
| 23.05 | 工程与交付 API | 接口 | 部分实测 | 真实浏览器和 API 调用所覆盖的建模、任务、文档、工程、发布接口；全量 HTTP 合同另做哈希比对。model-runs.json / regression-evidence.json |
| 23.06 | 使用反馈 | 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 23.07 | 工具插件框架 | 已接入 / 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 23.08 | 已暴露的业务工具 | 可选 / 接口 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 23.09 | 框架基础工具 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 23.10 | 插件接入方式 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 24.01 | 持久任务编排 | 已接入 | 实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.02 | 自动派发恢复 | 已接入 | 实测 | 真实持久化派发意图在无客户端重试时启动内核。test-summary.json → postgres_tests → test_committed_intent_starts_real_kernel_without_client_retry |
| 24.03 | 重复执行保护 | 已接入 | 实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.04 | 隔离 CAD 执行 | 已接入 | 实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.05 | 执行资源控制 | 受限 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 24.06 | 数据持久化 | 已接入 | 实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.07 | 访问与审计 | 已接入 | 实测 | PostgreSQL 与 RLS 验证身份、成员、权限和使用情况。test-summary.json → postgres_tests → test_personal_tenant_project_membership_audit_and_usage_are_real |
| 24.08 | 模型服务适配 | 已接入 | 实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.09 | 健康与就绪检查 | 已接入 | 部分实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.10 | 容器与腾讯云部署 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 24.11 | 数据备份与恢复工具 | 已接入 | 基线对照 | 沿用对应实现与接口；本轮没有独立复演该项的全部产品行为。before-contract.json / after-contract.json |
| 24.12 | 自动化质量检查 | 已接入 | 部分实测 | 真实 Temporal 工作流、独立 PostgreSQL 和版本化对象桶、模型服务及就绪探测。model-runs.json / other-model-runs.json |
| 24.13 | 用量统计基础 | 受限 | 实测 | 真实 PostgreSQL 聚合计量、失败空值以及跨工作空间的账户隔离。test-summary.json → postgres_tests → test_actual_usage_nullable_failures_and_cross_workspace_account_grouping |
