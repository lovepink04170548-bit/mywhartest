# WHartTest 当前改造与问题解决记录

## 1. 基线与范围

- 源项目基线：当前 `master/develop`，提交 `47b8a69`，版本 `v2.5.1`
- 记录日期：2026-08-12
- 当前工作树包含未提交的功能开发与测试代码。
- 相对源项目主要增量约 1.3 万行，核心集中在执行器、AI 规划链、Django UI 自动化 API 和前端页面。

## 2. 新增与修改功能

### 2.1 UI 环境认证配置

- 增加自动登录开关、登录用户名和密码配置。
- 编辑环境时密码为空表示保留已保存密码。
- 关闭自动登录时清理认证信息。
- API 返回遮蔽密码，只返回是否已配置密码。
- 只有下发给执行器时才注入完整密码。
- 支持忽略 HTTPS 证书错误的环境配置。
- 增加环境配置兼容迁移和测试。

### 2.2 自动登录与验证码 OCR

执行器现在可自动完成：

1. 采集登录页。
2. 填写用户名和密码。
3. 识别验证码输入框和相邻图片。
4. 截图并调用 `image_ocr` 运行时解析器。
5. 填写验证码。
6. 勾选协议或隐私确认框。
7. 选择真实登录提交按钮。
8. 通过 URL 和页面状态验证登录结果。

验证码不使用固定文本，OCR 结果和截图会记录到任务证据中。

### 2.3 Playwright 原生页面探索

页面探索已按当前方案改为 Playwright 原生 API，不依赖官方 Playwright MCP 采集会话。当前采集包括：

- DOM 元素、操作类型、可用状态和推荐 locator。
- Accessibility tree 摘要与 AX 引用。
- iframe 元素。
- Shadow DOM 元素。
- 虚拟列表状态。
- 组件上下文和网络请求摘要。
- 页面截图、元素截图区域和视觉 SOM。
- 页面状态、状态迁移、Trace 和 storage state。

同时增加 Angular 常见控件采集：`ng-click`、`data-ng-click`、`x-ng-click`、`onclick` 和可聚焦控件。

### 2.4 iframe 上下文统一

此前 iframe 控件存在于 `state.frames`，但探索只遍历主文档，点击时也从主页面创建 locator。当前已统一：

- 主文档和 iframe 元素进入同一个探索候选集合。
- 保留 `frame_key`、`frame_name`、`frame_url` 和 `context_type`。
- iframe 元素使用带 frame 前缀的稳定元素键。
- 点击和表单判断切换到正确 Frame 上下文。
- 页面签名包含 iframe 元素，避免状态错误去重。
- Django LLM 页面摘要和绑定候选包含 iframe 元素及上下文。

### 2.5 元素地图治理

元素地图已从一次性采集结果扩展为可治理资产：

- 保存页面、元素、locator candidates、状态迁移和采集证据。
- 增加元素地图版本、当前基线和版本状态。
- 保存 locator 稳定性、低置信度和风险信息。
- 自动修复候选进入 review queue。
- 人工确认后可反写元素地图 locator 基线。
- 实时采集结果优先，历史确认 locator 作为自愈候选。
- 元素地图页面提供批量删除能力。

### 2.6 AI 生成任务后台化

AI 生成测试用例已改为后台任务：

- 前端创建后显示生成中状态。
- 执行器后台消费任务。
- WebSocket 和 API 回传任务结果。
- 成功后用例归属当前选中的需求模块。
- 分发记录保存 dispatch id、尝试次数、ACK 时间和错误。
- 通过 ACK、轮询、重试和幂等机制降低 WebSocket 丢消息风险。
- 新增 AI 生成任务列表页面。

### 2.7 LLM 多阶段规划链

AI 规划已拆分为多个职责阶段：

1. 需求分析：提取动作、数据、前置条件和断言。
2. 状态路由：根据观察到的页面和迁移选择路径。
3. 元素绑定：把动作绑定到页面、元素和 locator。
4. 可执行计划生成。
5. TypeScript Playwright spec 生成。
6. 真实执行验证。
7. 失败分类、重新观察和完整重规划。

主要约束：

- 不通过深链接跳过登录、菜单、弹窗和状态迁移。
- 元素步骤必须引用元素地图中的 locator。
- 自定义下拉不能误用原生 `select_option`。
- 验证码等动态值必须使用运行时解析器。
- 用户明确的测试点和校验点必须覆盖。
- required 字段只是证据，不是所有任务的强制规则。

### 2.8 TypeScript Playwright 真实验证

生成结果不再只停留在 LLM 文本：

- 生成 TypeScript Playwright spec。
- 使用 `@playwright/test` 执行真实脚本。
- 记录 stdout、stderr、退出码、失败步骤和失败分类。
- 保存截图、Trace、网络摘要、HTML/JUnit 等执行证据。
- 失败后重新采集当前页面。
- 基于真实证据重新生成完整计划和完整 spec。

### 2.9 自动修复与人审闭环

当前链路形成：

`执行 -> 失败分类 -> 重新观察 -> LLM 完整重规划 -> 重跑`

支持：

- locator candidates 轮换。
- disabled、readonly、not editable 状态依赖处理。
- 记录旧 locator、新 locator、错误和执行证据。
- 修复候选进入 review queue。
- 人工确认后写入元素地图基线。
- 无法自动修复时保留失败页面、截图、Trace 和人工处理信息。

明确禁止针对某个业务用例增加固定步骤或固定 locator 规则。

## 3. 遇到的问题与解决结果

### 3.1 浏览器和执行器连接问题

问题：

- Playwright 浏览器可执行文件缺失。
- 执行器登录连接超时。
- WebSocket 重连后任务丢失或重复入队。
- 多个旧执行器同时在线。
- 后端端口 8000 被占用。

解决：

- 配置并安装本地 Playwright 浏览器。
- 增加执行器认证、心跳、任务 ACK、dispatch id、轮询和重试。
- 清理旧执行器，只保留最新实例。
- 启动服务前检查端口并处理端口冲突。

### 3.2 官方 MCP 会话问题

问题：

- 官方 MCP HTTP session 丢失。
- MCP server 连接失败。
- 采集任务耗时长，重试总耗时超过执行器等待上限。

解决：

- 页面探索改用 Playwright 原生 API。
- 保留 MCP 风格观察数据结构供 LLM 使用。
- 将 LLM 规划拆成多阶段后台任务。
- 增加阶段预算和完整重规划。
- 失败时记录规划阶段和错误，不静默使用不符合需求的规则脚本。

### 3.3 登录后只采集登录页

问题：

- 自动登录成功后业务页元素为 0。
- Angular 菜单是 `div ng-click`，普通选择器无法发现。
- 业务页面控件位于 iframe，探索器看不到。

解决：

- 扩展 Angular 控件采集。
- 登录后重新等待和采集。
- 将 iframe 纳入探索候选。
- 点击 iframe 元素时使用对应 Frame。
- LLM 绑定阶段传递 frame 上下文。

### 3.4 探索乱点菜单

问题：

- 完整 Gherkin 被压缩成一个通用的“打开业务表单”目标。
- 探索预算被无关菜单消耗。
- 搜索、行操作、标签页和添加按钮没有按需求顺序执行。

解决：

- 从需求文本提取显式进入、打开、点击和切换目标。
- 按需求顺序生成探索阶段。
- 到达业务表单后采集完整表单状态。
- 不写入具体业务名称或固定 locator。

### 3.5 弹窗字段缺失

问题：

- 脚本只有弹窗可见性断言，没有字段输入和保存操作。
- 元素数量限制和优先级导致弹窗控件截断。
- “最简必填项”被误当成所有需求的强制字段。

解决：

- 提升弹窗、表单、required、label 和 placeholder 元素的采集优先级。
- 完善 dialog context 和控件操作类型。
- required 仅作为 LLM 规划证据。
- 强制先执行打开弹窗迁移，再执行弹窗字段操作。
- 扩展元素地图和 LLM 规划上下文。

### 3.6 生成脚本只覆盖登录

问题：

- 生成脚本只有登录页 goto 和登录控件断言。
- 业务测试点、输入、提交和结果校验没有执行。

解决：

- 自动区分登录是被测流程还是探索前置条件。
- 登录后保存并复用 storage state。
- 后续业务页必须通过已观察迁移到达。
- 用 explicit action contract 强制覆盖需求动作和断言。
- 以 TypeScript 真实执行结果作为最终裁决。

### 3.7 元素定位失败反复打规则补丁

问题：

- locator 失败后容易增加单一业务特例。
- 脚本通用性下降，错误修复不可复用。

解决：

- 使用 locator candidates 和实时重新观察。
- 将失败步骤、页面快照、AX 树和网络摘要回灌 LLM。
- 重新生成完整计划和完整 spec，而非只替换一行。
- 修复候选进入人审队列，确认后才固化基线。

### 3.8 Trace 缺少业务截图

问题：

- 执行记录成功但 Trace 只有登录页。
- 探索和真实执行使用不同上下文。

解决：

- 探索和真实执行都启用 Trace。
- 保存探索登录态 storage state。
- 真实脚本从目标 URL 开始并复用验证后的登录态。
- 失败时记录 failure reobserve 截图和页面状态。

### 3.9 验证码未填写

问题：

- 生成脚本没有固定验证码，导致登录按钮不可用或登录失败。

解决：

- 接入 OCR 运行时动态输入解析器。
- 动态截取验证码、OCR、填写并验证登录结果。
- 禁止 LLM 生成固定验证码文本。

### 3.10 页面导航超时

问题：

- 企业系统存在轮询或长连接，`networkidle` 永远不能满足。
- 改用 `domcontentloaded` 后，目标系统在部分时段连 DOM 也未在短超时内返回。

解决：

- 探索导航改为 `domcontentloaded`。
- `networkidle` 只作为最多 3 秒的稳定提示。
- 初始导航使用独立环境连接预算，至少 45 秒。
- 将目标环境不可达归类为环境导航问题，而不是 LLM 或元素定位问题。

## 4. 验证情况

已通过或完成的验证：

- 环境认证配置测试：3/3。
- 自动登录 OCR、协议勾选和提交按钮选择测试。
- Angular 点击控件采集测试。
- iframe 元素统一模型测试。
- iframe 上下文点击测试。
- iframe 表单覆盖测试。
- Gherkin 显式导航顺序提取测试。
- DOM ready 导航测试。
- 执行器最近一轮回归测试：5/5。
- Python 编译检查通过。

部分 Django TestCase 因 PostgreSQL 测试数据库连接不稳定，无法完成测试数据库初始化；该错误属于测试环境连接问题，不是断言失败。

## 5. 当前限制

### 5.1 目标环境可达性

任务 42 最近一次失败在页面探索初始导航阶段，未进入登录、业务探索或 LLM 规划。错误为：

```
Playwright 原生页面探索任务失败:
Page.goto: Timeout exceeded
waiting until "domcontentloaded"
```

需要确认：

- 执行器机器能访问目标 IP 和端口。
- VPN、路由、防火墙和代理正常。
- 目标应用服务正在运行。
- HTTPS 反向代理和证书配置正常。

### 5.2 数据库测试环境

需要提供稳定的测试 PostgreSQL 实例，避免测试初始化连接开发数据库。

### 5.3 复杂页面能力边界

当前已支持 iframe、Shadow DOM、Angular、虚拟列表和动态表单，但多层 iframe、虚拟列表深度滚动、新窗口、多标签页、复杂级联下拉和复杂行关系仍需要继续增强。

### 5.4 上游 LLM 依赖

模型响应速度、上下文预算、限流、超时和 JSON 合法性仍会影响任务耗时。当前已有多阶段请求、阶段预算、后台执行和完整重规划，但模型服务不可用时无法生成业务步骤。

## 6. 建议启动和验证顺序

1. 启动 PostgreSQL，确认 Django API 可访问。
2. 启动后端 API/WebSocket。
3. 启动唯一执行器，确认执行器 ID、WebSocket 和心跳。
4. 从执行器所在机器检查目标 URL 可达。
5. 确认环境配置填写账号、密码并具备 OCR 依赖。
6. 创建或重试 AI 生成任务。
7. 依次检查探索覆盖、LLM 规划、TypeScript 真实执行结果。
8. 人工确认 locator 修复候选后再写入元素地图基线。

## 7. 关键文件

- 执行器：`WHartTest_Actuator/executor.py`
- 任务消费和 LLM 调度：`WHartTest_Actuator/consumer.py`
- OCR：`WHartTest_Actuator/runtime_input_resolver.py`
- AI 规划：`WHartTest_Django/ui_automation/ai_planning.py`
- 元素地图治理：`WHartTest_Django/ui_automation/element_map_governance.py`
- UI 自动化 API：`WHartTest_Django/ui_automation/views.py`
- AI 任务页面：`WHartTest_Vue/src/features/ui-automation/views/AiGenerationTaskList.vue`
- 元素地图版本页面：`WHartTest_Vue/src/features/ui-automation/views/ElementMapVersionList.vue`
- 执行器回归测试：`WHartTest_Actuator/test_executor_mcp_collection.py`
- 动态输入测试：`WHartTest_Actuator/test_runtime_input_resolver.py`
- LLM 重新观察测试：`WHartTest_Actuator/test_consumer_ai_reobserve.py`
- CI 检查：`.github/workflows/ai-ui-generation-checks.yml`

