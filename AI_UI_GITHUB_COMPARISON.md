# AI 辅助 UI 自动化对照研究

## 对照项目

- [browser-use/browser-use](https://github.com/browser-use/browser-use)
- [Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern)
- [browserbase/stagehand](https://github.com/browserbase/stagehand)
- [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)

## 观察到的共性

- browser-use 把问题定义成“浏览器代理 + 实际浏览器动作空间 + recovery loop”，不是把所有步骤都压成一次性 LLM 决策。
- Stagehand 明确区分 `act`、`extract`、`observe` 和 `agent`，关键路径偏向确定性 primitive，AI 只处理模糊或陌生页面。
- Skyvern 把 LLM + computer vision + Playwright-compatible SDK 结合起来，核心目标是替换脆弱的 DOM/XPath 脚本。
- Playwright MCP 直接暴露结构化 accessibility snapshot，强调 deterministic tool application，而不是截图驱动的纯视觉猜测。

## 对当前执行链的判断

当前 WHartTest 的根因，不是“词表还不够多”，而是执行链把高不确定性的语义决策集中到了少数串行 LLM 阶段里，尤其是 `state_route_selection`。

和上面这些项目相比，当前链路的问题在于：

1. 路由选择、元素绑定、最终计划被拆成多个串行 LLM 关卡，但这些关卡之间没有足够强的结构化合同。
2. `state_route_selection` 本来只应是对已观察页面/迁移图的保守选择，却被实现成必须成功的单点依赖。
3. 上下文压缩后，路由阶段仍然要处理过多信息，超时就直接把整条链路打断。
4. 执行器和 planner 之间缺少像 Playwright MCP 那样清晰的结构化状态边界，也缺少 Stagehand 那种“AI 只负责模糊部分，确定性 primitive 兜底”的分层。

## 结论

最可能的根因是架构边界错了：

- 不是缺少更多业务规则。
- 而是把“语义理解”与“可执行合同”混在同一条串行链路里，导致单个 LLM 阶段超时或误判就能拖垮整任务。

更接近高星项目的做法是：

- AI 负责局部语义歧义；
- 结构化页面状态负责确定性 grounding；
- 执行器只消费强合同；
- 任一语义阶段失败时，系统应能降级，而不是清空已有事实。

## 本次失败的直接修复方向

这次报错的特征是：页面已渲染，但 `current_candidates` 为空，runtime planner 只能不断重观察。对照同类项目，正确做法不是继续加重观察次数，而是把“可见但未入候选池”的结构化观察补进 grounding 层。

建议按下面顺序修：

1. 把 `accessibility_snapshot` 变成第一类候选源。
   - browser-use 更接近“状态快照 + 可点击元素列表”的确定性 grounding。
   - Playwright MCP 也是先拿 `browser_snapshot` / accessibility tree，再做确定性操作。
   - 当前 WHartTest 已采集 AX tree，但 runtime 只看 `current_candidates`，这和同类项目的 grounding 路径不一致。

2. runtime planner 不能只依赖 `current_candidates`。
   - 需要把 `current_observation.elements`、`frames.elements`、`accessibility_snapshot.nodes` 合并成一个候选池。
   - 只要观测里已经出现可交互语义节点，就应该直接生成下一步，而不是回退成 `wait_and_reobserve`。

3. 菜单类目标要支持“父级展开”。
   - Stagehand 的 `observe -> act -> re-observe` 循环意味着，菜单叶子没出现时，应该先点击可见的父级菜单，再重观察。
   - 对于“测试任务”这种左侧树/菜单项，runtime 需要允许先命中上层导航，再进入叶子，而不是等叶子本身成为当前页唯一候选。

4. 继续保留当前的合同分层。
   - 认证入口、业务候选、观察候选不要再共用同一条文本回退。
   - 但在每一层里都要有 AX-first 的兜底，避免观测转译丢元素。

## 当前建议

- 主修复：把 `accessibility_snapshot` 纳入 runtime 候选池，并允许从 AX 节点合成 locator。
- 次修复：在菜单目标上增加父级导航回退，不再只等叶子目标出现。
- 不建议：继续增加 `wait_and_reobserve` 次数，或者只加更多关键词。

## 来源

- browser-use repo: https://github.com/browser-use/browser-use
- Stagehand repo: https://github.com/browserbase/stagehand
- Stagehand docs: https://docs.browserbase.com/welcome/quickstarts/stagehand
- Playwright MCP repo: https://github.com/microsoft/playwright-mcp
- Skyvern repo: https://github.com/Skyvern-AI/skyvern
