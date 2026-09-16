# AI UI 执行链最终方案

## 目标

把自然语言用例和 Gherkin 用例统一编译成同一份结构化需求合同，再用确定性路由、局部绑定和小步执行闭环完成 UI 自动化。

## 结构

1. 输入层
   - 自然语言和 Gherkin 都进入同一套需求编译器。
   - Gherkin 先做语法解析，再做语义归一化。
   - 最终输出统一的 `Requirement Contract`。

2. 感知层
   - 执行器只采集页面事实，不解释业务意图。
   - 采集内容包括 URL、title、ARIA tree、frames、visible elements、state transitions。

3. 路由层
   - 路由先做确定性图搜索。
   - LLM 只在等价路径里做 tie-break。
   - `ReadTimeout` 和 `524` 走降级，不清空链路。

4. 绑定层
   - 只在路由覆盖页面内绑定元素。
   - 绑定依据是 role、placeholder、label、text、frame context。

5. 编译层
   - `Requirement Contract + Route Contract + Binding Contract` 编译成最终计划。
   - 编译器只做顺序校正、frame 修正、风险门控和动作补全。

6. 执行闭环
   - 每一步执行后重新观察页面。
   - 失败只修局部步骤，不整链重跑。

## 落地顺序

1. 统一需求编译入口，合并自然语言和 Gherkin。
2. 把 `state_route_selection` 改成确定性优先 + LLM 仅做 tie-break。
3. 收窄 binding 和 plan 输入，只保留结构化候选。
4. 执行器只消费合同，不解析自然语言。
5. 补充 route timeout、Gherkin、iframe、局部修复回归。

## 不做的事

- 不再用词表判断登录、认证、业务阶段。
- 不再让单个 LLM 阶段承担整条链路的生死。
- 不再把全量压缩图直接喂给最终计划生成。

## 实现状态

更新时间：2026-08-20

1. 统一需求编译入口：已实现。
   - 自然语言和 Gherkin 统一进入 `requirement_document`。
   - planner 主链持有 `RequirementContract`，并在 route、binding、plan 三个阶段显式传递。

2. 确定性优先路由：已实现。
   - `state_route_selection` 先生成确定性 `route_seed`。
   - LLM 路由超时、`ReadTimeout`、`502/503/524` 等可恢复错误会降级为确定性路由，不清空已完成链路。

3. 结构化绑定和计划输入：已实现。
   - planner 主链持有 `RouteContract` 和 `BindingContract`。
   - binding 只消费路由页、候选元素、显式匹配和真实 state transitions。
   - plan 只消费 `RequirementContract + RouteContract + BindingContract`，不直接读取完整元素图。

4. 执行器只消费合同：已实现。
   - executor 的登录凭据、业务关键词、路由目标、筛选条件、覆盖关键词、认证模式和 MCP 兜底计划均改为结构化合同优先。
   - `requirement` / `gherkin` 原文仅保留为缺少 `requirement_document` 时的兼容兜底。

5. 执行闭环局部修复：已实现基础闭环。
   - 每步执行后按当前页面状态继续，失败时采集失败现场。
   - locator、timeout、state dependency 类错误优先做当前步骤局部修复和重试，不整链重跑。
   - TypeScript/LLM 级修复入口仍保留为后续异步修复能力，但主执行链已经具备局部步骤修复。

6. 回归覆盖：已补齐核心单元/桩回归。
   - 覆盖 route timeout、Gherkin 显式动作、iframe frame context、结构化合同传递、executor 合同消费和局部修复入口。
   - 当前 `/mnt/d/Project` 工作区缺少完整 Django/Playwright 运行依赖；端到端真实任务回归需要在本地服务环境中执行。
