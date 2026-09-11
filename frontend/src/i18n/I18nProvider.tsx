import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { I18nContext, type Locale } from "./I18nContext";

export type { Locale } from "./I18nContext";

const STORAGE_KEY = "cad-agent-locale";

// The production UI currently uses Chinese source copy. This exact dictionary
// keeps free-form prompts, generated model data, identifiers, and unknown backend
// messages untouched while translating application chrome and known state copy.
const ZH_EN_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ["CAD Agent 工作台", "CAD Agent Workspace"],
  ["参数化建模工作台", "Parametric modeling workspace"],
  ["正在检查本地登录配置...", "Checking local sign-in configuration..."],
  ["正在恢复工程会话...", "Restoring engineering session..."],
  ["进入工作台", "Enter workspace"],
  ["登录后生成、检查并导出 CAD 模型。", "Sign in to generate, inspect, and export CAD models."],
  ["账号操作", "Account actions"],
  ["登录", "Sign in"],
  ["邀请码注册", "Register with invite"],
  ["手机号或管理员账号", "Phone number or administrator account"],
  ["手机号", "Phone number"],
  ["密码", "Password"],
  ["设置密码", "Create password"],
  ["输入密码", "Enter password"],
  ["至少 8 位", "At least 8 characters"],
  ["显示密码", "Show password"],
  ["隐藏密码", "Hide password"],
  ["至少 8 位字符。", "Use at least 8 characters."],
  ["确认密码", "Confirm password"],
  ["两次输入的密码不一致。", "The passwords do not match."],
  ["邀请码", "Invite code"],
  ["输入已发放的邀请码", "Enter your invite code"],
  ["正在处理", "Processing"],
  ["注册并登录", "Register and sign in"],
  ["操作失败，请稍后重试", "The operation failed. Please try again."],
  ["服务器无响应，请稍后重试", "The server did not respond. Please try again."],
  ["请输入有效手机号", "Enter a valid phone number."],
  ["手机号或密码错误", "Incorrect phone number or password."],
  ["密码至少需要 8 位", "Password must be at least 8 characters."],
  ["邀请码无效、已过期或已用完", "The invite code is invalid, expired, or exhausted."],
  ["渲染出错", "Unable to render workspace"],
  ["当前界面遇到错误。你的工程会话仍保存在本地，可以安全重试。", "The interface encountered an error. Your engineering session remains saved locally, so it is safe to retry."],
  ["未知错误", "Unknown error"],
  ["重试", "Try again"],
  ["切换为英文", "Switch to English"],
  ["切换为中文", "Switch to Chinese"],
  ["切换语言", "Switch language"],
  ["新建设计", "New design"],
  ["打开项目导航", "Open project navigation"],
  ["实时连接中", "Connecting live session"],
  ["已自动保存", "Saved automatically"],
  ["返回项目流程", "Return to project flow"],
  ["询问 Agent", "Ask Agent"],
  ["查看变更", "Review changes"],
  ["检查", "Inspect"],
  ["导出", "Export"],
  ["设置", "Settings"],
  ["预览", "Preview"],
  ["关闭预览", "Close preview"],
  ["返回 Agent", "Return to Agent"],
  ["展开 Agent", "Expand Agent"],
  ["收起 Agent", "Collapse Agent"],
  ["调整 Agent 面板宽度", "Resize Agent panel"],
  ["检查器", "Inspector"],
  ["展开检查器", "Expand inspector"],
  ["收起检查器", "Collapse inspector"],
  ["调整检查器宽度", "Resize inspector"],
  ["机械设计检查器", "Mechanical design inspector"],
  ["显示当前模型的真实参数、版本、代码和文件。", "Shows real parameters, versions, code, and files for the current model."],
  ["关闭", "Close"],
  ["设置分类", "Settings categories"],
  ["DFM 规则", "DFM rules"],
  ["工艺知识图谱", "Process knowledge graph"],
  ["全部能力", "All capabilities"],
  ["运行能力", "Run capability"],
  ["关闭设置", "Close settings"],
  ["DFM 规则配置", "DFM rule configuration"],
  ["收起", "Collapse"],
  ["加载中...", "Loading..."],
  ["精确计算", "Exact calculation"],
  ["AI 推理", "AI reasoning"],
  ["智能推荐", "Smart recommendations"],
  ["供应商", "Suppliers"],
  ["工艺-材料", "Process and material"],
  ["无支持材料记录", "No supported-material records"],
  ["最大尺寸 (mm)", "Maximum size (mm)"],
  ["材料偏好", "Material preference"],
  ["铝合金", "Aluminum alloy"],
  ["压铸成型", "Die casting"],
  ["304 不锈钢", "304 stainless steel"],
  ["316L 不锈钢", "316L stainless steel"],
  ["45号钢", "45 steel"],
  ["6061-T6 铝合金", "6061-T6 aluminum alloy"],
  ["7075-T6 铝合金", "7075-T6 aluminum alloy"],
  ["H62 黄铜", "H62 brass"],
  ["壁厚≥", "Wall thickness ≥"],
  ["尺寸≤", "Dimension ≤"],
  ["推荐", "Recommend"],
  ["材料", "Material"],
  ["删除", "Delete"],
  ["暂无供应商记录", "No supplier records"],
  ["+ 添加供应商", "+ Add supplier"],
  ["输入供应商名称:", "Enter supplier name:"],
  ["能力目录", "Capability catalog"],
  ["只读", "Read only"],
  ["本地计算", "Local compute"],
  ["外部写入", "External write"],
  ["真实设备动作", "Physical device action"],
  ["稳定", "Stable"],
  ["实验性", "Experimental"],
  ["依赖就绪", "Dependencies ready"],
  ["依赖受限", "Dependencies limited"],
  ["（依赖受限）", "(dependencies limited)"],
  ["状态未知", "Status unknown"],
  ["无", "None"],
  ["风险", "Risk"],
  ["风险：", "Risk: "],
  ["项", "items"],
  ["个 action · 展开详情", "actions · Expand details"],
  ["（必需）", "(required)"],
  ["（可选）", "(optional)"],
  ["展开详情", "Expand details"],
  ["当前阻塞原因", "Current blockers"],
  ["Actions（仅说明）", "Actions (description only)"],
  ["当前不可用", "Currently unavailable"],
  ["需显式确认", "Explicit confirmation required"],
  ["输入", "Inputs"],
  ["输出", "Outputs"],
  ["依赖", "Dependencies"],
  ["必需", "required"],
  ["可选", "optional"],
  ["此目录只展示能力、依赖与风险，不会运行 action。CAD / DXF 可从对话入口选择；其他能力通过结构化 action 和所需产物调用。", "This catalog describes capabilities, dependencies, and risk without running actions. Select CAD or DXF from the conversation entry; other capabilities use structured actions and required artifacts."],
  ["正在同步运行依赖状态…", "Syncing runtime dependency status…"],
  ["运行状态同步失败；当前展示内置清单，availability 未知。", "Runtime status sync failed. The built-in catalog is shown with unknown availability."],
  ["运行依赖状态已从服务端同步。", "Runtime dependency status is synced from the server."],
  ["当前展示内置能力清单。", "Showing the built-in capability catalog."],
  ["当前 vendored DXF 包没有独立验证器 CLI。", "The vendored DXF package does not provide a standalone validator CLI."],
  ["账号管理", "Account management"],
  ["打开账号管理", "Open account management"],
  ["关闭账号管理", "Close account management"],
  ["账号", "Account"],
  ["账号：", "Account: "],
  ["刷新登录状态", "Refresh sign-in session"],
  ["退出登录", "Sign out"],
  ["注销账号", "Delete account"],
  ["邀请码管理", "Invite management"],
  ["留空自动生成", "Leave blank to generate"],
  ["邀请码内容", "Invite code value"],
  ["邀请码最大使用次数", "Maximum invite uses"],
  ["创建", "Create"],
  ["已禁用", "Disabled"],
  ["禁用", "Disable"],
  ["暂无邀请码", "No invite codes"],
  ["登录状态已刷新", "Sign-in session refreshed"],
  ["登录状态刷新失败，请重新登录", "Unable to refresh the sign-in session. Please sign in again."],
  ["确认注销当前账号？此操作不可恢复。", "Delete this account? This action cannot be undone."],
  ["新建模型", "New creation"],
  ["工作区", "Workspace"],
  ["项目", "Projects"],
  ["历史记录", "History"],
  ["账户", "Account"],
  ["工作区成员", "Workspace member"],
  ["本地开发模式（无需登录）", "Local development mode (no sign-in required)"],
  ["项目流程", "Project flow"],
  ["机械设计", "Mechanical design"],
  ["电子设计", "Electronics"],
  ["仿真验证", "Simulation"],
  ["固件", "Firmware"],
  ["制造检查", "Manufacturing checks"],
  ["发布", "Release"],
  ["参数", "Parameters"],
  ["特征", "Features"],
  ["代码", "Code"],
  ["文件", "Files"],
  ["版本历史", "Version history"],
  ["暂无参数", "No parameters"],
  ["暂无代码", "No code"],
  ["暂无文件", "No files"],
  ["暂无可导出产物", "No exportable artifacts"],
  ["工程检查与证据", "Engineering checks and evidence"],
  ["当前没有这些领域的真实检查结果，界面仅展示已验证证据，不用推测数据代替结果。", "No real checks are available for these areas. The interface shows verified evidence only and does not substitute inferred data."],
  ["当前结果没有 inspect report。点击“运行工程检查”获取真实分析。", "The current result has no inspection report. Select “Run engineering checks” to obtain a real analysis."],
  ["运行工程检查", "Run engineering checks"],
  ["正在检查", "Inspecting"],
  ["请求 Agent 修复", "Ask Agent to fix"],
  ["完成", "Done"],
  ["下载", "Download"],
  ["下载中", "Downloading"],
  ["不可用", "Unavailable"],
  ["刷新", "Refresh"],
  ["成功", "Succeeded"],
  ["失败", "Failed"],
  ["通过", "Passed"],
  ["警告", "Warning"],
  ["未知", "Unknown"],
  ["未记录", "Not recorded"],
  ["未记录提示词", "No prompt recorded"],
  ["项目初始化", "Project initialization"],
  ["自然语言生成", "Natural-language generation"],
  ["参数化代码执行", "Parametric code execution"],
  ["参数修改", "Parameter edit"],
  ["零件修改", "Part edit"],
  ["几何检查", "Geometry check"],
  ["几何验证器", "Geometry validator"],
  ["视觉检查", "Visual inspection"],
  ["加载中", "Loading"],
  ["暂无已保存版本。", "No saved versions."],
  ["历史版本", "Version history"],
  ["当前零件", "Current parts"],
  ["零件级修改", "Part-level edit"],
  ["修改选中零件", "Modify selected part"],
  ["当前", "Current"],
  ["未选择", "Not selected"],
  ["未分配", "Unassigned"],
  ["位置", "Position"],
  ["对话", "Conversation"],
  ["新对话", "New conversation"],
  ["发送", "Send"],
  ["继续描述你希望如何修改模型…", "Keep iterating on the model…"],
  ["CAD 预览", "CAD preview"],
  ["模型", "Model"],
  ["线框", "Wireframe"],
  ["透视", "Perspective"],
  ["正交", "Orthographic"],
  ["拖动旋转 · Shift + 拖动平移 · 滚轮缩放", "Drag to orbit · Shift + drag to pan · Scroll to zoom"],
  ["适应视图", "Fit view"],
  ["重置等轴视图", "Reset isometric view"],
  ["属性", "Properties"],
  ["连接中", "Connecting"],
  ["展开侧栏", "Expand sidebar"],
  ["折叠侧栏", "Collapse sidebar"],
  ["关闭项目导航", "Close project navigation"],
  ["专业工作区", "Professional workspace"],
  ["工程线程", "Engineering threads"],
  ["历史项目", "Project history"],
  ["刷新历史", "Refresh history"],
  ["正在加载…", "Loading…"],
  ["暂无历史项目", "No project history"],
  ["管理员", "Administrator"],
  ["确认删除这个历史项目？", "Delete this historical project?"],
  ["今天要创建什么工程？", "What do you want to engineer today?"],
  ["工程需求", "Engineering requirements"],
  ["例如：设计一个 IP67 工业传感器外壳，支持壁装，内部预留 PCB、电池和密封圈空间……", "For example: Design an IP67 industrial sensor enclosure for wall mounting, with space for a PCB, battery, and seal…"],
  ["当前生成接口支持文本需求；能力工作区可单独上传工程文件。", "The current generation endpoint accepts text requirements; engineering files can be uploaded separately in the capability workspace."],
  ["制造方式", "Manufacturing process"],
  ["正在连接实时任务", "Connecting live task"],
  ["开始创建", "Start creating"],
  ["设计一个带 M4 安装孔的防水传感器外壳，适合 FDM 打印", "Design a waterproof sensor enclosure with M4 mounting holes for FDM printing"],
  ["创建一个 60 × 40 × 25 mm 的两腔电子盒，壁厚 2 mm", "Create a 60 × 40 × 25 mm two-compartment electronics enclosure with 2 mm walls"],
  ["设计一个可夹在 25 mm 桌板上的耳机挂钩", "Design a headphone hook that clamps to a 25 mm desktop"],
  ["CNC 加工", "CNC machining"],
  ["FDM 打印", "FDM printing"],
  ["SLA 打印", "SLA printing"],
  ["注塑成型", "Injection molding"],
  ["针金加工", "Sheet metal"],
  ["CNC 3轴", "3-axis CNC"],
  ["CNC 5轴", "5-axis CNC"],
  ["注塑", "Injection molding"],
  ["针金", "Sheet metal"],
  ["壁厚", "Wall thickness"],
  ["尺寸", "Size"],
  ["拔模", "Draft"],
  ["结构化能力运行器", "Structured capability runner"],
  ["正在载入能力…", "Loading capabilities…"],
  ["上传输入产物并调用固定 action。参数不能提供任意命令或宿主路径；缺少外部依赖时返回 blocked。", "Upload input artifacts and invoke a fixed action. Parameters cannot contain arbitrary commands or host paths; missing external dependencies return blocked."],
  ["能力", "Capability"],
  ["依赖受限", "Dependencies limited"],
  ["将上传路径写入参数", "Write uploaded path to parameter"],
  ["上传中…", "Uploading…"],
  ["选择并上传文件", "Choose and upload file"],
  ["Action 参数（JSON）", "Action parameters (JSON)"],
  ["Action 参数 JSON", "Action parameters JSON"],
  ["正在运行…", "Running…"],
  ["产物", "Artifacts"],
  ["安全命令预览", "Safe command preview"],
  ["运行数据", "Run data"],
  ["当前部署缺少此 action 所需依赖。", "This deployment is missing dependencies required by the action."],
  ["此 action 可能影响真实设备。默认不会执行；部署级开关、execute 和 action 专用确认必须同时通过。请确认构建板清空、耗材/喷嘴/机型正确且操作员在场。", "This action can affect a physical device. It will not execute by default; deployment controls, execute, and action-specific confirmation must all pass. Confirm the build plate is clear, material, nozzle, and printer are correct, and an operator is present."],
  ["建模与制图", "Modeling and drafting"],
  ["查看与复用", "View and reuse"],
  ["机器人与仿真", "Robotics and simulation"],
  ["制造与打印", "Manufacturing and printing"],
  ["STEP-first 参数化零件与装配的创建、修改、检查和快照验证。", "Create, modify, inspect, and snapshot-verify STEP-first parametric parts and assemblies."],
  ["生成", "Generate"],
  ["快照", "Snapshot"],
  ["生成和检查 2D 轮廓、模板、面板、垫片与切割图。", "Generate and inspect 2D profiles, templates, panels, gaskets, and cutting drawings."],
  ["验证几何", "Validate geometry"],
  ["实验性的 GLSL SDF、晶格与 TPMS 建模、raymarch 预览和网格导出。", "Experimental GLSL SDF, lattice and TPMS modeling, raymarch preview, and mesh export."],
  ["统一预览 CAD、DXF、G-code、隐式 CAD 与机器人描述文件。", "Preview CAD, DXF, G-code, implicit CAD, and robot description files in one viewer."],
  ["启动或复用查看器", "Start or reuse viewer"],
  ["创建审阅链接", "Create review link"],
  ["检索并校验螺钉、轴承、电机、连接器等商品 STEP 零件。", "Search and validate commercial STEP parts such as screws, bearings, motors, and connectors."],
  ["搜索", "Search"],
  ["下载并校验", "Download and validate"],
  ["机器人 link、joint、limit、inertial、visual/collision 与网格引用。", "Robot links, joints, limits, inertials, visual/collision geometry, and mesh references."],
  ["生成并验证", "Generate and validate"],
  ["MoveIt 规划组、末端执行器、姿态与碰撞禁用语义。", "MoveIt planning groups, end effectors, poses, and disabled-collision semantics."],
  ["SDFormat 模型与世界，包括物理、传感器、灯光和插件。", "SDFormat models and worlds, including physics, sensors, lights, and plugins."],
  ["Gazebo 检查", "Gazebo check"],
  ["基于当前官方材料、厚度和工艺规则的 DXF/STEP 上传预检。", "DXF/STEP upload preflight using current official material, thickness, and process rules."],
  ["上传预检", "Upload preflight"],
  ["调用真实切片器 dry-run、切片并静态验证 FDM G-code。", "Use a real slicer to dry-run, slice, and statically validate FDM G-code."],
  ["发现切片器", "Discover slicer"],
  ["检查网格", "Inspect mesh"],
  ["切片预演", "Slicing dry run"],
  ["切片", "Slice"],
  ["验证", "Validate"],
  ["局域网读取状态、上传并在显式确认后谨慎启停 Bambu 打印任务。", "Read status over LAN, upload, and carefully start or stop Bambu print jobs after explicit confirmation."],
  ["交接预演", "Handoff dry run"],
  ["读取序列号", "Read serial number"],
  ["读取状态", "Read status"],
  ["上传", "Upload"],
  ["启印", "Start print"],
  ["暂停", "Pause"],
  ["取消", "Cancel"],
  ["清除错误", "Clear error"],
  ["工程协作", "Engineering collaboration"],
  ["已连接", "Connected"],
  ["正在连接", "Connecting"],
  ["当前上下文", "Current context"],
  ["实时连接", "Live connection"],
  ["描述希望分析或修改的内容。请求会先进入确认，再通过现有实时链路提交。", "Describe what you want to analyze or change. The request is reviewed first, then submitted through the existing live connection."],
  ["你", "You"],
  ["当前 CAD 模型", "Current CAD model"],
  ["个参数", "parameters"],
  ["查看模型", "View model"],
  ["Agent 运行", "Agent run"],
  ["智能体运行时间线", "Agent run timeline"],
  ["记录本次任务的执行进度、自动修复、检查证据和产物信息。", "Tracks task progress, automatic repairs, inspection evidence, and artifacts."],
  ["最新步骤", "Latest step"],
  ["个步骤", "steps"],
  ["重新运行当前代码", "Rerun current code"],
  ["收起时间线", "Collapse timeline"],
  ["需求规划", "Requirements planning"],
  ["生成建模代码", "Generating model code"],
  ["准备建模代码", "Preparing model code"],
  ["执行建模", "Building model"],
  ["执行代码", "Executing code"],
  ["执行 CAD 代码", "Executing CAD code"],
  ["几何校验", "Geometry validation"],
  ["自动修复", "Automatic repair"],
  ["自动修复代码", "Automatic code repair"],
  ["渲染预览", "Rendering preview"],
  ["导出工程文件", "Exporting engineering files"],
  ["保存工程版本", "Saving model version"],
  ["整理工程结果", "Finalizing engineering result"],
  ["任务已完成", "Task completed"],
  ["任务失败", "Task failed"],
  ["持久任务已创建", "Durable task created"],
  ["任务状态已更新", "Task status updated"],
  ["已请求取消任务", "Task cancellation requested"],
  ["建模计划已记录", "Modeling plan recorded"],
  ["任务确认已记录", "Task confirmation recorded"],
  ["工程需求已解析", "Engineering requirements parsed"],
  ["建模步骤已拆解", "Modeling steps decomposed"],
  ["执行计划已生成", "Execution plan generated"],
  ["候选模型已开始构建", "Candidate model build started"],
  ["建模代码已生成", "Modeling code generated"],
  ["自动修复代码已生成", "Automatic repair code generated"],
  ["视觉修复代码已生成", "Visual repair code generated"],
  ["候选工程产物已生成", "Candidate engineering artifacts generated"],
  ["工程验证证据已记录", "Engineering validation evidence recorded"],
  ["候选版本已封装", "Candidate version sealed"],
  ["候选模型状态已更新", "Candidate model state updated"],
  ["执行步骤已创建", "Execution step created"],
  ["步骤状态已更新", "Step status updated"],
  ["执行尝试已创建", "Execution attempt created"],
  ["执行尝试状态已更新", "Execution attempt status updated"],
  ["计算资源已分配", "Compute resources allocated"],
  ["隔离计算已开始", "Isolated compute started"],
  ["隔离计算运行中", "Isolated compute running"],
  ["隔离计算已完成", "Isolated compute completed"],
  ["正在准备建模代码", "Preparing modeling code"],
  ["建模代码已准备", "Modeling code prepared"],
  ["工程产物已保存", "Engineering artifact saved"],
  ["工程产物校验失败", "Engineering artifact validation failed"],
  ["工程验证已完成", "Engineering validation completed"],
  ["变更证据已更新", "Change evidence updated"],
  ["变更已接受", "Change accepted"],
  ["已请求修改变更", "Change revision requested"],
  ["变更已拒绝", "Change rejected"],
  ["版本已提交", "Version committed"],
  ["版本已回滚", "Version rolled back"],
  ["兼容任务进度已更新", "Compatibility task progress updated"],
  ["兼容任务结果已更新", "Compatibility task result updated"],
  ["兼容任务状态已更新", "Compatibility task status updated"],
  ["运行中", "Running"],
  ["已成功", "Succeeded"],
  ["已失败", "Failed"],
  ["待处理", "Pending"],
  ["已取消", "Cancelled"],
  ["已观测", "Observed"],
  ["建模计划与执行后端不匹配", "The modeling plan does not match the execution backend"],
  ["边选择方式无效", "Invalid edge selection mode"],
  ["拓扑选择无法解析", "Unable to resolve topology selection"],
  ["拓扑选择对象不匹配", "Topology selection target mismatch"],
  ["草图约束格式无效", "Invalid sketch constraint"],
  ["草图约束冗余", "Redundant sketch constraints"],
  ["草图约束冲突", "Conflicting sketch constraints"],
  ["草图尚未完全约束", "Sketch is under-constrained"],
  ["草图求解失败", "Sketch solver failed"],
  ["参数状态已过期", "Parameter state is stale"],
  ["缺少参数状态", "Parameter state is missing"],
  ["参数修改存在重复项", "Parameter update contains duplicates"],
  ["参数不可编辑", "Parameter is not editable"],
  ["参数值类型无效", "Invalid parameter value type"],
  ["参数属性类型已变化", "Parameter property type changed"],
  ["参数值超出有效范围", "Parameter value is out of range"],
  ["减材特征没有移除材料", "The subtractive feature removed no material"],
  ["BOM 输入缺失", "BOM input is missing"],
  ["BOM 输入不唯一", "BOM input is ambiguous"],
  ["BOM 输入完整性校验失败", "BOM input integrity check failed"],
  ["BOM 输入读取失败", "Unable to load BOM input"],
  ["当前运行时不支持原生 BOM", "The current runtime does not support native BOM"],
  ["BOM 来源不是有效装配体", "The BOM source is not a valid assembly"],
  ["BOM 来源层级已丢失", "The BOM source hierarchy was lost"],
  ["BOM 部件与装配几何不一致", "BOM component and assembly geometry do not match"],
  ["BOM 生成失败", "BOM generation failed"],
  ["BOM 没有部件行", "The BOM contains no component rows"],
  ["BOM 产物封存失败", "BOM artifact sealing failed"],
  ["BOM 产物封存超时", "BOM artifact sealing timed out"],
  ["隔离运行时响应无效", "Invalid sandbox runtime response"],
  ["FreeCAD 内部错误", "FreeCAD internal error"],
  ["正在执行计划", "Executing plan"],
  ["实时接收步骤与产物事件", "Receiving live step and artifact events"],
  ["待确认请求", "Request awaiting confirmation"],
  ["当前模块", "Current module"],
  ["提交内容", "Request content"],
  ["确认后才会调用后端，具体操作计划以服务端实时返回为准。", "The backend is called only after confirmation; the exact operation plan comes from live server events."],
  ["返回修改", "Back to edit"],
  ["确认并执行", "Confirm and execute"],
  ["继续迭代当前设计…", "Keep iterating on the current design…"],
  ["审查请求", "Review request"],
  ["参数化", "Parametric"],
  ["审查", "Review"],
  ["当前项目", "Current project"],
  ["分支", "Branch"],
  ["阶段", "Stage"],
  ["状态", "Status"],
  ["已完成", "Completed"],
  ["进行中", "In progress"],
  ["等待任务", "Waiting for task"],
  ["属性", "Properties"],
  ["版本", "Versions"],
  ["模型属性", "Model properties"],
  ["显示当前结果解析出的真实参数。", "Shows real parameters parsed from the current result."],
  ["编辑", "Edit"],
  ["当前结果没有可编辑参数。生成参数化模型后会显示真实参数。", "The current result has no editable parameters. Real parameters appear after generating a parametric model."],
  ["生成代码", "Generated code"],
  ["暂无可显示代码", "No code to display"],
  ["生成成功后，这里会显示后端返回的当前模型代码。", "After generation succeeds, the current model code returned by the backend appears here."],
  ["工程文件", "Engineering files"],
  ["当前结果和实时任务返回的产物。", "Artifacts returned by the current result and live task."],
  ["当前结果尚未返回可用工程文件。", "The current result has not returned usable engineering files."],
  ["任务产物事件", "Task artifact events"],
  ["等待工程产物", "Waiting for engineering artifacts"],
  ["工程模型", "Engineering model"],
  ["等待生成", "Waiting for generation"],
  ["当前阶段", "Current stage"],
  ["需求与方案", "Requirements and solution"],
  ["计划", "Plan"],
  ["步", "step"],
  ["打开项目流程", "Open project flow"],
  ["拖动旋转 · 滚轮缩放 · 右键平移", "Drag to orbit · Scroll to zoom · Right-click to pan"],
  ["任务", "Task"],
  ["已闭合", "Watertight"],
  ["正在重新计算模型", "Recomputing model"],
  ["正在等待执行进度", "Waiting for execution progress"],
  ["SVG 预览", "SVG preview"],
  ["STL 预览", "STL preview"],
  ["个工程文件", "engineering files"],
  ["尚无工程文件", "No engineering files yet"],
  ["模型已闭合", "Model is watertight"],
  ["模型已生成", "Model generated"],
  ["正在生成", "Generating"],
  ["几何", "Geometry"],
  ["装配", "Assembly"],
  ["网格闭合", "Mesh watertightness"],
  ["模型网格闭合。", "The model mesh is watertight."],
  ["模型存在非闭合边界。", "The model has non-watertight boundaries."],
  ["当前模型", "Current model"],
  ["文件导出", "File export"],
  ["汇总当前任务的设计简报、执行过程、几何检查、修复记录和历史版本。", "Summarizes the design brief, execution, geometry checks, repair history, and versions for the current task."],
  ["补充确认信息", "Provide confirmation details"],
  ["设计简报等待确认", "Design brief awaiting confirmation"],
  ["暂无可检查结果", "No result to inspect"],
  ["正在运行工程检查", "Running engineering checks"],
  ["工程检查失败", "Engineering checks failed"],
  ["尚未运行检查", "Checks not run"],
  ["影响", "Impact"],
  ["影响：", "Impact: "],
  ["对象", "Object"],
  ["对象：", "Object: "],
  ["建议", "Suggestion"],
  ["建议：", "Suggestion: "],
  ["零件与历史版本", "Parts and version history"],
  ["恢复会重新执行已保存代码，并生成可审查的 Change Set。", "Restore re-executes saved code and creates a reviewable change set."],
  ["刷新版本", "Refresh versions"],
  ["修改会进入持久任务与 Change Set 审查流程。", "The edit enters the durable task and change-set review flow."],
  ["当前结果尚未包含装配零件。生成装配体后会在这里显示子零件。", "The current result has no assembly parts. Child parts appear here after generating an assembly."],
  ["物料清单", "Bill of materials"],
  ["来自当前版本持久化的 FreeCAD Assembly 原生 BOM。", "Native FreeCAD Assembly BOM persisted with the current version."],
  ["当前版本不是装配体，无需生成 BOM。", "The current version is not an assembly, so no BOM is required."],
  ["对比中...", "Comparing..."],
  ["对比当前", "Compare with current"],
  ["当前版本", "Current version"],
  ["提交中...", "Submitting..."],
  ["恢复", "Restore"],
  ["对比结果", "Comparison"],
  ["模型代码", "Model code"],
  ["已变化", "Changed"],
  ["无变化", "No changes"],
  ["检查结果", "Inspection result"],
  ["导出文件", "Export files"],
  ["变更审查", "Change review"],
  ["仅展示版本快照、任务结果、参数、几何指标、文件引用和验证记录能够证实的变化；缺失证据保持未知。", "Shows only changes supported by version snapshots, task results, parameters, geometry metrics, file references, and validation records; missing evidence remains unknown."],
  ["当前为兼容快照审查；持久 Change Set 建立后可执行接受与提交。", "This is a compatibility snapshot review. Accept and commit become available after a durable Change Set is created."],
  ["请求修改", "Request changes"],
  ["拒绝变更", "Reject change"],
  ["接受变更", "Accept change"],
  ["提交版本", "Commit version"],
  ["正在处理", "Processing"],
  ["回滚", "Roll back"],
  ["审查意见", "Review note"],
  ["拒绝或请求修改时必填；接受时可选。", "Required when rejecting or requesting changes; optional when accepting."],
  ["修改目标", "Change objective"],
  ["版本关系", "Version relationship"],
  ["修改对象数", "Modified objects"],
  ["· 请求", "· Request"],
  ["参数变化", "Parameter changes"],
  ["没有父版本参数证据，或已记录参数值未变化。", "No parent-version parameter evidence is available, or the recorded values did not change."],
  ["代码与几何", "Code and geometry"],
  ["有证据表明已变化", "Evidence confirms a change"],
  ["现有指标未变化", "Recorded metrics are unchanged"],
  ["验证与风险", "Validation and risk"],
  ["验证：", "Validation: "],
  ["风险：", "Risk: "],
  ["低", "Low"],
  ["中", "Medium"],
  ["高", "High"],
  ["有警告", "Warnings"],
  ["未通过", "Failed"],
  ["存在问题", "Issues found"],
  ["未能判定", "Indeterminate"],
  ["文件变化", "File changes"],
  ["新增", "Added"],
  ["移除", "Removed"],
  ["替换", "Replaced"],
  ["文件内容变化已由不可变 Artifact 的 SHA-256 证实。", "The immutable artifact SHA-256 confirms the file content changed."],
  ["仅能证实文件引用变化；当前元数据没有 SHA-256，不能声称内容已变化。", "Only the file reference change is proven. Without a SHA-256 in the current metadata, a content change cannot be claimed."],
  ["没有可证实的文件引用变化。", "No provable file-reference changes."],
  ["Agent 操作日志", "Agent activity log"],
  ["没有已持久化的 Agent 操作日志。", "No persisted Agent activity log."],
  ["后端未记录可判定的风险依据。", "The backend recorded no evidence for a determinate risk assessment."],
  ["正在读取当前版本及其明确记录的父版本。", "Reading the current version and its explicitly recorded parent."],
  ["正在加载变更证据", "Loading change evidence"],
  ["变更证据加载失败", "Unable to load change evidence"],
  ["当前面板还没有真实模型快照。完成一次 CAD 生成或参数执行后再查看变更。", "This panel has no real model snapshot yet. Complete a CAD generation or parameter execution before reviewing changes."],
  ["暂无可审查版本", "No version available for review"],
  ["操作失败", "Operation failed"],
  ["新建对话", "New conversation"],
  ["重命名对话", "Rename conversation"],
  ["已连接", "Connected"],
  ["工程任务", "Engineering task"],
  ["折叠 Agent", "Collapse Agent"],
  ["折叠检查器", "Collapse inspector"],
  ["增加孔位", "Add mounting holes"],
  ["边缘倒圆", "Round exposed edges"],
  ["增强强度", "Increase strength"],
  ["调整尺寸", "Adjust dimensions"],
  ["在保持当前外形的基础上，增加可编辑直径、间距和沉孔选项的安装孔。", "Add mounting holes with editable diameter, spacing, and countersink options while preserving the current form."],
  ["将外露尖锐边缘做安全圆角，同时保留所有功能特征。", "Round exposed sharp edges for safety while preserving every functional feature."],
  ["在不改变主要占用空间的前提下，通过加厚壁厚、增加加强筋和放大圆角来提升强度。", "Increase strength with thicker walls, added ribs, and larger fillets without changing the main envelope."],
  ["更新关键尺寸，同时保留当前设计意图并保持模型参数化。", "Update key dimensions while preserving the design intent and keeping the model parametric."],
  ["CAD 模型已生成", "CAD model generated"],
  ["CAD 任务执行失败", "CAD task failed"],
  ["补充约束", "Add constraints"],
  ["当前浏览器无法创建 3D 画布", "This browser cannot create a 3D canvas"],
  ["STL 文件已成功读取，但 WebGL 不可用。请启用浏览器硬件加速后重新打开模型。", "The STL file loaded successfully, but WebGL is unavailable. Enable browser hardware acceleration and reopen the model."],
  ["参数化 CAD ·", "Parametric CAD ·"],
  ["个参数 ·", "parameters ·"],
  ["⌘ Enter 审查", "⌘ Enter to review"],
  ["立方体边长", "Cube edge length"],
  ["高级参数", "Advanced parameters"],
];

const zhToEn = new Map<string, string>(ZH_EN_PAIRS);
const enToZh = new Map<string, string>(ZH_EN_PAIRS.map(([zh, en]) => [en, zh]));

function translatePattern(value: string, locale: Locale): string {
  if (locale === "en") {
    let match = value.match(/^账号：(.+)$/);
    if (match) return `Account: ${match[1]}`;
    match = value.match(/^对话 (\d+)$/);
    if (match) return `Conversation ${match[1]}`;
    match = value.match(/^会话 (.+)$/);
    if (match) return `Session ${match[1]}`;
    match = value.match(/^更新已验证的 FreeCAD 参数：(.+)$/);
    if (match) return `Update verified FreeCAD parameters: ${match[1]}`;
    match = value.match(/^删除项目：(.+)$/);
    if (match) return `Delete project: ${match[1]}`;
    match = value.match(/^(\d+) 项$/);
    if (match) return `${match[1]} capabilities`;
    match = value.match(/^(\d+) 个 action · 展开详情$/);
    if (match) return `${match[1]} actions · Expand details`;
    match = value.match(/^风险：(.+)$/);
    if (match) return `Risk: ${translateCore(match[1], locale)}`;
    match = value.match(/^材料: (.+)$/);
    if (match) return `Material: ${match[1]}`;
    match = value.match(/^当前：(.+)$/);
    if (match) return `Current: ${translateCore(match[1], locale)}`;
    match = value.match(/^(\d+) 个$/);
    if (match) return `${match[1]} items`;
    match = value.match(/^(\d+) 条$/);
    if (match) return `${match[1]} versions`;
    match = value.match(/^返回项目流程：(.+)$/);
    if (match) return `Return to project flow: ${match[1]}`;
    match = value.match(/^(\d+) \/ (\d+) 已完成$/);
    if (match) return `${match[1]} / ${match[2]} completed`;
    match = value.match(/^参数化 CAD · (\d+) 个参数(.*)$/);
    if (match) return `Parametric CAD · ${match[1]} parameters${match[2]}`;
    match = value.match(/^运行 (.+)$/);
    if (match) return `Run ${match[1]}`;
    match = value.match(/^任务 (.+)$/);
    if (match) return `Task ${match[1]}`;
    match = value.match(/^(\d+) 个工程文件$/);
    if (match) return `${match[1]} engineering files`;
    match = value.match(/^当前版本 v(.+)$/);
    if (match) return `Current version v${match[1]}`;
    match = value.match(/^父版本未知 → v(.+)$/);
    if (match) return `Parent version unknown → v${match[1]}`;
    match = value.match(/^审查状态：(.+)$/);
    if (match) {
      const labels: Record<string, string> = {
        pending_review: "Pending review",
        accepted: "Accepted",
        changes_requested: "Changes requested",
        rejected: "Rejected",
        committed: "Committed",
        rolled_back: "Rolled back",
      };
      return `Review status: ${labels[match[1]] || match[1]}`;
    }
    match = value.match(/^验证状态：(.+)，问题 (\d+) 项$/);
    if (match) return `Validation status: ${match[1] === "passed" ? "passed" : match[1]}, ${match[2]} issues`;
    match = value.match(/^ID：(.+)$/);
    if (match) return `ID: ${match[1]}`;
    match = value.match(/^位置：(.+)$/);
    if (match) return `Position: ${match[1]}`;
    match = value.match(/^壁厚≥(.+)$/);
    if (match) return `Wall ≥ ${match[1]}`;
    match = value.match(/^尺寸≤(.+)$/);
    if (match) return `Size ≤ ${match[1]}`;
    match = value.match(/^拔模≥(.+)$/);
    if (match) return `Draft ≥ ${match[1]}`;
    match = value.match(/^关闭(.+)$/);
    if (match) return `Close ${translateCore(match[1], locale)}`;
  } else {
    let match = value.match(/^Account: (.+)$/);
    if (match) return `账号：${match[1]}`;
    match = value.match(/^Conversation (\d+)$/);
    if (match) return `对话 ${match[1]}`;
    match = value.match(/^Session (.+)$/);
    if (match) return `会话 ${match[1]}`;
    match = value.match(/^Update verified FreeCAD parameters: (.+)$/);
    if (match) return `更新已验证的 FreeCAD 参数：${match[1]}`;
    match = value.match(/^Delete project: (.+)$/);
    if (match) return `删除项目：${match[1]}`;
    match = value.match(/^(\d+) capabilities$/);
    if (match) return `${match[1]} 项`;
    match = value.match(/^(\d+) actions · Expand details$/);
    if (match) return `${match[1]} 个 action · 展开详情`;
    match = value.match(/^Risk: (.+)$/);
    if (match) return `风险：${translateCore(match[1], locale)}`;
    match = value.match(/^Material: (.+)$/);
    if (match) return `材料: ${match[1]}`;
    match = value.match(/^Current: (.+)$/);
    if (match) return `当前：${translateCore(match[1], locale)}`;
    match = value.match(/^(\d+) items$/);
    if (match) return `${match[1]} 个`;
    match = value.match(/^(\d+) versions$/);
    if (match) return `${match[1]} 条`;
    match = value.match(/^Return to project flow: (.+)$/);
    if (match) return `返回项目流程：${match[1]}`;
    match = value.match(/^(\d+) \/ (\d+) completed$/);
    if (match) return `${match[1]} / ${match[2]} 已完成`;
    match = value.match(/^Parametric CAD · (\d+) parameters(.*)$/);
    if (match) return `参数化 CAD · ${match[1]} 个参数${match[2]}`;
    match = value.match(/^Run (.+)$/);
    if (match) return `运行 ${match[1]}`;
    match = value.match(/^Task (.+)$/);
    if (match) return `任务 ${match[1]}`;
    match = value.match(/^(\d+) engineering files$/);
    if (match) return `${match[1]} 个工程文件`;
    match = value.match(/^Current version v(.+)$/);
    if (match) return `当前版本 v${match[1]}`;
    match = value.match(/^Parent version unknown → v(.+)$/);
    if (match) return `父版本未知 → v${match[1]}`;
    match = value.match(/^Review status: (.+)$/);
    if (match) {
      const labels: Record<string, string> = {
        "Pending review": "pending_review",
        Accepted: "accepted",
        "Changes requested": "changes_requested",
        Rejected: "rejected",
        Committed: "committed",
        "Rolled back": "rolled_back",
      };
      return `审查状态：${labels[match[1]] || match[1]}`;
    }
    match = value.match(/^Validation status: (.+), (\d+) issues$/);
    if (match) return `验证状态：${match[1]}，问题 ${match[2]} 项`;
    match = value.match(/^ID: (.+)$/);
    if (match) return `ID：${match[1]}`;
    match = value.match(/^Position: (.+)$/);
    if (match) return `位置：${match[1]}`;
    match = value.match(/^Wall ≥ (.+)$/);
    if (match) return `壁厚≥${match[1]}`;
    match = value.match(/^Size ≤ (.+)$/);
    if (match) return `尺寸≤${match[1]}`;
    match = value.match(/^Draft ≥ (.+)$/);
    if (match) return `拔模≥${match[1]}`;
    match = value.match(/^Close (.+)$/);
    if (match) return `关闭${translateCore(match[1], locale)}`;
  }
  return value;
}

function translateCore(value: string, locale: Locale): string {
  return (locale === "en" ? zhToEn.get(value) : enToZh.get(value))
    ?? translatePattern(value, locale);
}

function translateUiText(value: string, locale: Locale): string {
  const leading = value.match(/^\s*/)?.[0] ?? "";
  const trailing = value.match(/\s*$/)?.[0] ?? "";
  const core = value.trim();
  if (!core) return value;
  return `${leading}${translateCore(core, locale)}${trailing}`;
}

function getInitialLocale(): Locale {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "en" || stored === "zh" ? stored : "zh";
}

function shouldSkip(element: Element | null): boolean {
  return Boolean(element?.closest("script, style, svg, canvas, code, pre, [data-i18n-skip], .ww-agent-message--user"));
}

function translateElement(root: ParentNode, locale: Locale) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    if (!shouldSkip(node.parentElement) && node.nodeValue?.trim()) nodes.push(node);
  }
  for (const node of nodes) {
    const next = translateUiText(node.nodeValue ?? "", locale);
    if (next !== node.nodeValue) node.nodeValue = next;
  }

  const elements = root instanceof Element
    ? [root, ...root.querySelectorAll<HTMLElement>("*")]
    : [...root.querySelectorAll<HTMLElement>("*")];
  for (const element of elements) {
    if (shouldSkip(element)) continue;
    for (const attribute of ["aria-label", "placeholder", "title"] as const) {
      const value = element.getAttribute(attribute);
      if (!value) continue;
      const next = translateUiText(value, locale);
      if (next !== value) element.setAttribute(attribute, next);
    }
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }, []);
  const toggleLocale = useCallback(() => {
    setLocale(locale === "zh" ? "en" : "zh");
  }, [locale, setLocale]);
  const translate = useCallback((value: string) => translateUiText(value, locale), [locale]);

  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
    document.title = translateUiText(document.title, locale);
    translateElement(document.body, locale);

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "attributes") {
          const element = mutation.target as Element;
          const attribute = mutation.attributeName;
          if (!attribute || shouldSkip(element)) continue;
          const value = element.getAttribute(attribute);
          if (!value) continue;
          const next = translateUiText(value, locale);
          if (next !== value) element.setAttribute(attribute, next);
          continue;
        }
        if (mutation.type === "characterData") {
          const node = mutation.target as Text;
          if (shouldSkip(node.parentElement)) continue;
          const next = translateUiText(node.nodeValue ?? "", locale);
          if (next !== node.nodeValue) node.nodeValue = next;
          continue;
        }
        for (const node of mutation.addedNodes) {
          if (node instanceof Text) {
            if (shouldSkip(node.parentElement)) continue;
            const next = translateUiText(node.nodeValue ?? "", locale);
            if (next !== node.nodeValue) node.nodeValue = next;
          } else if (node instanceof Element && !shouldSkip(node)) {
            translateElement(node, locale);
          }
        }
      }
    });
    observer.observe(document.body, {
      attributeFilter: ["aria-label", "placeholder", "title"],
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
    return () => observer.disconnect();
  }, [locale]);

  const value = useMemo(
    () => ({ locale, setLocale, toggleLocale, translate }),
    [locale, setLocale, toggleLocale, translate],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
