# AI UI 执行链修复记录

> 后续修复执行链前先读本文件。目标是修链路合同和阶段边界，禁止按具体业务文案堆规则。

## 2026-08-17

### 1. 伪 state_transition 导致计划校验失败

- 现象：计划步骤携带未被元素地图真实观察到的 `state_transition`，静态校验报状态迁移相关错误。
- 根因：LLM 生成或中间整理阶段把“应该发生的页面跳转”当成已观测迁移写进 step，校验层没有区分真实观测和推测。
- 修复：在计划归一化、静态校验、artifact 附加前清理未观测的 `state_transition`；只有元素地图中能匹配到的迁移才保留。
- 回归：`UiAiPlanningElementMapCompactionTests` 中状态迁移保留/清理/绑定校验用例通过。

### 2. 自动登录验证码单次 OCR 失败导致探索停在登录页

- 现象：VPN 和页面可访问时，自动登录偶发停留在登录页，后续业务探索失败。
- 根因：验证码属于动态运行时输入，单次 OCR 失败被当成最终登录失败，没有通用重试闭环。
- 修复：执行器自动登录链路增加可重试登录阻塞判断和有限重试，验证码类运行时输入不写死静态值。
- 回归：真实任务后续已能进入登录后业务页并采集 8 个页面快照。

### 3. 显式动作合同未进入最终计划

- 现象：任务 63 多次失败，报 `用户明确要求点击动作未覆盖: 添加配置对象`、`最终计划缺少必需动作 explicit_action_4`。
- 根因：页面探索已经观测到唯一可点击元素，但 requirement contract 没把唯一匹配证据稳定传给最终计划编译；最终计划只相信 LLM 输出，缺少确定性补全层。
- 修复：
  - `build_requirement_contract` 保留唯一匹配证据：`matched_page_key`、`matched_element_key`、`matched_target_name`、`matched_locator_hint`、frame 信息和 `explicit_match_count`。
  - `standardize_staged_contract_steps` 在 required action 缺 step 且存在唯一匹配证据时，确定性编译为可执行 step；多候选不自动补。
  - staged planner 初次生成和重规划统一走 `_compile_required_actions_into_plan`。
- 回归：新增/通过 `test_unique_explicit_matched_action_is_compiled_into_staged_plan`、`test_ambiguous_explicit_matched_action_is_not_auto_compiled`。

### 4. LLM 二次重规划超时导致已有候选计划被丢弃

- 现象：任务 63 在页面探索成功后，`executable_plan_generation` 第二轮或 focus `element_binding` 阶段超时，最终保存为空计划，`failure_category=llm`。
- 根因：第一轮已经生成候选计划，但 repair loop 在进入第二次 LLM binding/plan 前没有先用最新 bindings 重编译当前候选；后续 LLM 超时后直接整体失败。
- 修复：
  - 新增 `_refresh_staged_candidate_from_bindings`，在 bindings 变化后不调用 LLM，直接重编译候选计划、重新生成脚本 artifact、重新静态校验。
  - repair loop 在二次 binding 前先刷新当前最佳候选；二次 binding 或二次 plan 异常时保留当前最佳候选。
- 回归：新增/通过 `test_binding_refresh_compiles_best_candidate_without_second_llm`。

### 5. 状态迁移整理删除必需合同动作

- 现象：任务 63 在线回归不再空计划，但仍缺少 `explicit_action_4`；离线单独调用 `standardize_staged_contract_steps` 能补出 17 个动作，调用 `_ensure_state_transition_steps_for_plan` 后变成 16 个动作。
- 根因：状态迁移整理是辅助层，却在最终合同编译之后运行；其去重/重排结果可以覆盖 requirement flow 的必需动作，导致已补齐的显式动作再次丢失。
- 修复：`_compile_required_actions_into_plan` 在状态迁移整理之后，再执行一遍 `standardize_staged_contract_steps`，让 requirement contract 成为最终裁决层。
- 回归：
  - 新增/通过 `test_required_contract_survives_state_transition_normalization`。
  - 使用任务 63 保存数据离线回放，修复后输出 17 个动作，包含 `explicit_action_4`，顺序与 requirement flow 一致。

### 6. 相邻 click 去重误删不同合同动作

- 现象：VPN 恢复后重跑任务 63，页面探索和 staged LLM 规划均完成，`requirements.flow` 与 `element_bindings` 都包含 `explicit_action_4`，但最终 `generated_case.steps` 缺少 `explicit_action_4`，报 `最终计划缺少必需动作 explicit_action_4`。
- 根因：`_compile_required_actions_into_plan` 已补回合同动作，但 `_attach_generated_plan_artifacts` 内部调用 `_strip_unobserved_state_transitions`，其末尾复用 `_renumber_plan_steps`；`_renumber_plan_steps` 会把相邻等价 click 去重，且没有区分不同 `action_id`。不同 required action 被当成重复点击折叠，导致合同动作在后置清理层被删除。
- 修复：`_renumber_plan_steps` 保留旧的相邻重复 click 稳定性逻辑，但增加合同边界：两个步骤同时带有非空且不同的 `action_id` 时，不允许去重。
- 回归：
  - 新增/通过 `test_contract_click_steps_with_distinct_action_ids_are_not_deduplicated`。
  - 使用任务 63 保存数据离线回放，`_attach_generated_plan_artifacts` 后仍包含 `explicit_action_4`，`plan_validation.status=success`。

### 7. TypeScript 编译阶段候选 locator 覆盖主绑定

- 现象：任务 63 真实回归已越过计划校验，最终 `generated_case.steps` 包含 `explicit_action_1` 到 `explicit_action_5`，`plan_validation.status=success`；但 TypeScript Playwright 真实执行失败，`generated.spec.ts:30` 在 `fill` 企业编号时使用了 `page.getByRole("button", { name: "搜索" })`，报 `Timeout waiting for visible locator match: matched=0`。
- 根因：计划 step 的主 `locator_hint` 是正确的 `placeholder=请输入子企业编号`，但该 step 的 `locator_candidates` 被后续状态迁移/候选整理污染为“搜索”按钮；`_build_typescript_spec_from_steps` 在候选非空时完全忽略主 `locator_hint`，导致最终 TS 编译破坏计划合同，把输入动作编译到按钮 locator 上。
- 修复：
  - 新增 `_step_locator_candidates`，统一把主 `locator_hint` 放在首位，再追加去重候选。
  - 新增 `_locator_compatible_with_operation`，输入类操作过滤明显不兼容候选，例如 `role=button/link/menuitem/tab` 和纯 `text` locator。
  - `_build_typescript_spec_from_steps` 改为使用统一候选整理函数，不再让候选覆盖主绑定。
- 回归：
  - 新增/通过 `test_typescript_fill_prefers_primary_locator_over_incompatible_candidates`。
  - 使用任务 63 保存数据离线回放，step_sort=4 生成 `page.getByPlaceholder("请输入子企业编号")`，搜索按钮只出现在下一步 click 搜索中。

### 8. TypeScript 编译阶段丢失 iframe 执行上下文

- 现象：任务 63 重跑后旧的“输入框被搜索按钮覆盖”未复发，但 TypeScript 真实执行仍在企业编号输入失败，`generated.spec.ts:30` 报 `Timeout waiting for visible locator match: matched=0`。
- 根因：计划 step 的 `element_key` 已明确来自 iframe（如 `frame_1::frame_1`），元素地图也记录了 `iframe_w` 和 frame URL；但 `_build_typescript_spec_from_steps` 只在 step 显式携带 `context_type='frame'` 时才编译 frame 作用域。部分步骤经过合同编译/重规划后保留了 frame element key，却丢失了 `context_type`，最终 locator 被编译到主 `page` 上查找，导致 iframe 内输入框不可见。
- 修复：
  - 新增 `_find_page_in_element_map` 和 `_step_frame_context`，从 `page_key`、`element_key` 的 `frame_key::inner_key` 结构以及元素地图 frames 事实推导执行上下文。
  - Python 脚本和 TypeScript spec 编译统一使用 `_step_frame_context`，不再只依赖 LLM 或绑定阶段显式填写 `context_type`。
  - 支持两类真实形态：`frame_1::input_1` 前缀键，以及 page frame 元素列表里的未前缀 `element_key`。
- 回归：
  - 新增/通过 `test_typescript_infers_frame_context_from_observed_frame_element_key`。
  - 定向通过 `test_typescript_fill_prefers_primary_locator_over_incompatible_candidates`、`test_typescript_step_uses_all_observed_locator_candidates`。
  - 使用任务 63 保存数据离线回放，企业编号输入生成 `frame4!.getByPlaceholder("请输入子企业编号")`，后续 iframe 内点击/填写步骤也保留 frame 作用域。

## 已执行验证命令

```bash
cd /home/zhangyuan/projects/WHartTest
WHartTest_Django/.venv/bin/python -m py_compile \
  WHartTest_Django/ui_automation/ai_planning.py \
  WHartTest_Django/ui_automation/planning_contract.py \
  WHartTest_Django/ui_automation/tests.py

cd /home/zhangyuan/projects/WHartTest/WHartTest_Django
UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test --keepdb \
  ui_automation.tests.StagedBindingDeterministicFallbackTests.test_required_contract_survives_state_transition_normalization \
  ui_automation.tests.StagedBindingDeterministicFallbackTests.test_binding_refresh_compiles_best_candidate_without_second_llm \
  ui_automation.tests.StagedBindingDeterministicFallbackTests.test_unique_explicit_matched_action_is_compiled_into_staged_plan \
  ui_automation.tests.StagedBindingDeterministicFallbackTests.test_ambiguous_explicit_matched_action_is_not_auto_compiled
```

## 当前真实回归

- 任务：`UiAiGenerationTask id=63`
- 执行器：`actuator-94679`
- 状态：
  - VPN 恢复后真实回归已越过 `explicit_action_4` 缺失问题，最终计划包含全部显式动作且静态校验通过。
  - 已修复 TypeScript 编译阶段候选 locator 覆盖主绑定，以及 iframe 执行上下文丢失问题。
  - 两个根因均已完成单测和任务 63 离线真实数据回放。
  - 2026-08-18 最新真实回归未到达 iframe 输入步骤：新 spec 已正确生成 `frame1!.getByPlaceholder("请输入子企业编号")`，但 LLM 初始计划把“子企业管理”编译为 step 0 的 `assert_visible`，实际 `click` 被排到最后，违反导航状态依赖并在 `generated.spec.ts:23` 失败。随后全量重规划请求遭遇外部 LLM 网关 `HTTP 502 Bad Gateway`，因此本轮不能将 iframe 修复标记为端到端通过。
  - 下一项应修复“路由/状态前置动作在最终计划中的时序合同”，使页面断言只能出现在到达该页面的导航动作之后；这不是业务文案特例。

### 9. 认证合同误判为 test_subject，导致复合任务只停留在登录页

- 现象：任务 66 `查看账户信息` 只采到登录页 5 个元素，后续 `系统管理 / 账号管理 / 搜索 / 详情` 全部无法绑定，最终 `plan_validation` 在 `exploration_coverage_gate` 被拦截。
- 根因：执行器曾在本地用需求词表推断认证模式，把“带登录步骤且后续还有业务流程”的任务误判成 `test_subject`，从而跳过自动登录和登录后业务探索。
- 修复：
  - 认证合同改为由后端 LLM 在下发任务前分类，执行器只消费 `args.authentication_mode` 和 `args.authentication_contract`，不再解析自然语言。
  - 新增 `authentication_then_business` 合同，专门表示“认证是显式第一阶段，后面还有独立业务流程必须继续执行”。
  - 回归测试覆盖纯认证、前置认证和认证后业务三类场景。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_llm_generation_plan_allows_authentication_subject_tasks_to_bypass_login_guard`
  - `UiAiPlanningElementMapCompactionTests.test_task_authentication_contract_detects_authentication_then_business_flow`
  - `PlaywrightExecutorMcpCollectionTest.test_authentication_then_business_flow_continues_past_login_snapshot`

### 10. state_route_selection 外部 LLM 超时导致整链失败

- 现象：任务 66 在需求解析成功后，`state_route_selection` 阶段两次调用外部 LLM，先返回 `LLM HTTP 524`，随后 `ReadTimeout: The read operation timed out`，约 190 秒规划预算耗尽后整条 AI 生成链路失败。
- 根因：路由选择阶段本质上只是在已观察页面和状态迁移图上选择页面范围/迁移键，是可恢复阶段；旧实现把外部 LLM 网关超时当成不可恢复错误，直接回退为空计划，丢弃了已完成的 requirement contract 和元素地图事实。
- 修复：
  - 新增确定性路由兜底：基于已观察页面、真实 `state_transitions`、`target_url` 初始页生成保守 route，不解析业务文案、不使用固定词表。
  - `state_route_selection` 遇到 `TimeoutError`、`httpx.TimeoutException`、`HTTP 502/503/524` 等外部网关类错误时，将阶段标记为 `degraded` 并继续进入 `element_binding` 和最终计划编译。
  - 确定性路由从 `task.target_url` 对应页面开始，避免把业务页排在登录/入口页之前。
- 回归：
  - 新增/通过 `UiAiPlanningElementMapCompactionTests.test_staged_planner_recovers_route_timeout_with_deterministic_fallback`。
  - 通过 `UiAiPlanningElementMapCompactionTests.test_selected_route_compiler_inserts_missing_parent_transition`。
  - 通过认证合同和 staged planner 关键回归 5 个用例。

### 11. route/binding 仍以 raw dict 传递，contract 只停留在辅助函数层

- 现象：`RouteContract`、`BindingContract` 已经存在，但 `ai_planning._StagedUiGenerationPlanner` 仍把 route 和 bindings 当作 raw dict 在 `select_route`、`bind_elements`、`generate_plan`、`compile_route` 之间直接传递；一旦 stage 输出缺字段，后续阶段就只能继续猜，contract 没有真正成为执行链的稳定边界。
- 根因：合同对象只在 `planning_contract.py` 的校验函数里被使用，planner 主链没有保存 typed contract，也没有在阶段间显式转换，导致“合同-first”停在辅助层，未真正落到阶段传递面。
- 修复：
  - 在 `_StagedUiGenerationPlanner` 上持久化 `self.route_contract` 和 `self.binding_contract`。
  - `select_route` 成功/降级后都写入 `RouteContract`。
  - `bind_elements` 和 `generate_plan` 接受 `RouteContract` / `BindingContract`，并在 stage input 前统一转成结构化 dict。
  - `run()` 里后续阶段统一消费 contract 对象，再在校验函数入口转回 dict，避免 raw dict 在链路里反复漂移。
  - `RouteContract.from_result`、`BindingContract.from_result` 现在也支持接收自身实例，避免对象和 dict 混用时退回空合同。
- 回归：
  - 新增/通过 `test_planning_contract_module_exposes_stable_contract_interfaces` 的 RouteContract 断言。
  - 新增/通过 `test_staged_planner_passes_only_structured_results_between_roles`，确认 planner 在 `select_route -> bind_elements -> generate_plan` 过程中传递的是 contract 归一后的结构化输入。
  - 运行时最小回归通过：`select_route`、`bind_elements`、`generate_plan` 三段均能在桩依赖下按 contract 互传，且 `planner.route_contract` / `planner.binding_contract` 均被正确填充。

### 12. executor 兜底计划仍忽略已有 structured test_plan/generated_case

- 现象：`PlaywrightExecutor._build_mcp_test_plan()` 在官方 MCP 或 fallback 场景里，原先会优先从 `requirement` / `gherkin` 重新合成步骤，已经存在的 `generated_case` / `test_plan` 只在脚本生成阶段才被消费。
- 根因：执行器兜底生成器把结构化计划和自然语言需求混在同一层处理，导致“已编译好的合同”没有成为唯一事实源。
- 修复：
  - `_build_mcp_test_plan()` 现在先检查 `generated_case.steps` / `test_plan.steps`。
  - 只要结构化步骤已经存在，就直接规范化返回，不再回头解析 `requirement` / `gherkin`。
  - 自然语言生成逻辑只保留给真正没有 structured plan 的最后兜底。
- 回归：
  - 新增/通过 `test_mcp_test_plan_prefers_structured_contract_over_requirement_text`。
  - 桩依赖下直接调用 `_build_mcp_test_plan()`，即使 `requirement` / `gherkin` 是噪声文本，返回的仍是结构化合同中的 `steps`、`objective`、`preconditions` 和 `cleanup`。

### 13. executor 仍有若干 helper 直读原始 requirement/gherkin，导致结构化合同没真正贯穿

- 现象：`_extract_login_credentials`、`_extract_business_keywords`、`_extract_requirement_exploration_route`、`_extract_list_filter_constraints`、`_extract_exploration_coverage_keywords` 仍直接消费 `requirement` / `gherkin` 原文，导致登录判定、路由抽取、筛选条件、探索目标和覆盖判定在结构化合同已存在时仍会被噪声文本带偏。
- 根因：执行器虽然已经接入 `requirement_document`、`generated_case`、`test_plan` 和 `authentication_contract`，但这些 helper 还停留在“原始文本优先”的旧入口上，合同对象没有成为统一事实源。
- 修复：
  - 在 `PlaywrightExecutor` 增加结构化读取辅助方法，优先从 `requirement_document`、`generated_case`、`test_plan` 和 `authentication_contract` 取值。
  - 将登录账号提取、业务关键词提取、路由抽取、列表筛选条件抽取、探索覆盖关键词抽取和认证模式判断切到结构化合同优先，原始文本仅保留最后兜底。
  - `_build_mcp_test_plan()` 继续保持结构化计划优先，并补足 `generated_case` 元数据兜底。
- 回归：
  - `test_requirement_route_prefers_structured_requirement_document_over_raw_requirement`
  - `test_list_filter_constraints_prefers_structured_requirement_document`
  - `test_exploration_coverage_keywords_prefers_structured_requirement_document`
  - `test_extract_login_credentials_prefers_structured_requirement_document`
  - `test_authentication_mode_prefers_contract_over_login_page_guess`
  - `test_mcp_test_plan_uses_generated_case_metadata_when_test_plan_is_absent`

### 14. RequirementContract 仍停留在辅助函数返回值，未成为 planner 主链显式边界

- 现象：`RouteContract` 和 `BindingContract` 已经持久化在 `_StagedUiGenerationPlanner`，但需求层仍只把 `requirements` raw dict 往后传；`RequirementContract` 对象存在于 `planning_contract.py` 和部分测试中，却没有成为 planner 实例状态，也没有出现在后续 stage input / planning_pipeline 中。
- 根因：之前重构优先修复了路由和绑定的合同化，需求合同仍依赖 `_complete_staged_requirement_contract(...).as_dict()` 的隐式返回，导致“Requirement Contract + Route Contract + Binding Contract”三合同架构没有完全对称落地。
- 修复：
  - `_StagedUiGenerationPlanner` 新增 `self.requirement_contract: RequirementContract`。
  - `analyze_requirements()` 完成需求补全后写入 `RequirementContract`，并以 `as_dict()` 作为后续统一事实源。
  - `state_route_selection`、`element_binding`、`executable_plan_generation` 的 stage input 均显式携带 `requirement_contract`。
  - 多轮 binding merge 后同步 `self.binding_contract`，避免实例合同停留在局部修复返回值。
  - `planning_pipeline` 输出 `requirement_contract`、`route_contract`、`binding_contract`，便于后续失败定位直接看最终合同态。
- 回归：
  - 更新 `test_staged_planner_passes_only_structured_results_between_roles`，断言 requirement contract 进入 route/binding/plan 三个 stage input。
  - 更新 `test_staged_planner_run_keeps_route_and_binding_contracts_through_final_compile`，断言最终 `planning_pipeline` 暴露三类合同。

### 15. 认证合同 LLM 超时被伪装成 prerequisite，导致登录页任务误入前置认证门禁

- 现象：任务在认证合同分类阶段出现 `ReadTimeout` 后，系统没有保留“不确定”状态，而是把认证模式硬降级成 `prerequisite`，执行器随后在登录页触发“前置认证不可用”门禁并终止业务探索。
- 根因：认证合同没有独立的 `unknown/degraded` 结果态，`classify_task_authentication_contract` 的异常兜底直接返回 `prerequisite`，`_build_ai_generation_args` 也把空值继续回填成 `prerequisite`，执行器还会用登录页启发式继续做策略决策。
- 修复：
  - 认证合同新增 `unknown` 语义，LLM 超时/异常返回 `status='degraded'`，不再伪装成 `prerequisite`。
  - `UiAiGenerationTaskViewSet._build_ai_generation_args` 透传 `unknown`，不再回填成 `prerequisite`。
  - 执行器在登录页且认证合同未知时直接停止，并返回明确的 `authentication_contract_unavailable` 原因。
- 回归：
  - `UiEnvironmentAuthConfigTests.test_ai_dispatch_preserves_unknown_authentication_mode`
  - `UiPlanningElementMapCompactionTests.test_task_authentication_contract_llm_failure_returns_unknown_degraded_contract`
  - `PlaywrightExecutorMcpCollectionTest.test_business_exploration_blocks_when_authentication_contract_is_unknown`

### 16. open_business_form 探索只接受 button，漏掉导航菜单型表单入口

- 现象：任务 67 已通过显式 SSO 进入业务站点，并展开到业务导航菜单；trace 显示页面有菜单项型入口，但后续 `open_business_form` 阶段仍未被覆盖，任务最终 failed。
- 根因：执行器的表单入口探针 `_rank_form_entry_probe` 只把 `button` / 原生按钮 input 当作可打开业务表单的候选。真实系统里不少“创建/详情/配置”等表单入口是 `menuitem`、`treeitem`、`tab` 或 `link` 角色；执行器虽然采集到了这些导航项，却在 `open_business_form` 阶段直接过滤掉，导致探索覆盖门禁一直认为表单状态缺失。
- 修复：
  - `open_business_form` 探针允许结构化目标匹配的 `menuitem`、`treeitem`、`tab`、`link` 作为表单入口候选。
  - 导航型候选必须匹配当前 `target` 的结构化关键词或 route path；不靠具体业务文案和固定按钮词表放行。
  - 仍保留危险动作、确认动作、表单内按钮、不可见/无尺寸元素过滤。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_form_entry_probe_allows_navigation_item_that_opens_form`
  - `PlaywrightExecutorMcpCollectionTest.test_form_entry_probe_prefers_visible_button_outside_filter_form`

### 17. 无语义任务名覆盖显式 Gherkin 路由，探索目标退化为 target_module

- 现象：任务 67 的页面探索已完成认证并进入业务站点，但元素地图 `required_stages` 只有 `target_module`，`state_transitions` 全部记录在 `target_module` 阶段，最终报 `页面探索未覆盖必需动作的可绑定元素`，缺失账号管理页面、账号名称搜索条件、列表首条账号名称和详情页断言。
- 根因：`_build_exploration_targets()` 曾把 `requirement_document.task_name` 当作 `target_module` 的兜底来源；任务名为 `111` 时被误当成目标模块，使显式 Gherkin 路由 `系统管理 -> 账号管理` 没有成为探索阶段。执行链合同优先级错误：展示标题/任务名压过了可执行需求。
- 修复：
  - `target_module` 不再回退到 `requirement_document.task_name`。
  - 只要存在显式结构化路由，探索目标始终按 `requirement_route_n` 生成并立即返回，不再受任务名或模块名影响。
  - 修复不增加业务词、按钮词或页面名规则，只调整结构化需求合同的优先级。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_explicit_route_overrides_nonsemantic_task_name`
  - `PlaywrightExecutorMcpCollectionTest.test_form_entry_probe_allows_navigation_item_that_opens_form`
  - `PlaywrightExecutorMcpCollectionTest.test_exploration_targets_merge_backend_coverage_keywords_without_business_rules`

### 18. LLM 覆盖缺口反馈被显式 route early return 丢弃，列表行/详情断言无法补采

- 现象：任务 67 重跑后已不再缺 `账号管理页面` 和 `账号名称搜索条件`，但仍失败：`action_6 搜索结果列表中的第一个账号名称`、`assert_4 账号详情页面中的账号状态信息` 未绑定。
- 根因：
  - `_build_exploration_targets()` 在存在显式 route 时直接 `return`，导致后端 `llm_exploration_coverage.missing_required_actions` 没有进入下一轮探索目标。
  - `_extract_requirement_exploration_route()` 对无引号 BDD 行做 fallback 切分时，把行首 `And` 和普通列表行点击当成导航阶段。
  - `_extract_list_filter_constraints()` 的值正则只允许字母数字下划线和横线，邮箱值 `zhangwenwen@ebupt.com` 被截断为 `zhangwenwen`。
- 修复：
  - 显式 route 目标生成后继续追加 coverage feedback 目标，不再丢弃后端绑定缺口。
  - assertion 缺口生成 observe-only 目标，只观察当前页面是否出现目标/期望值，不为了断言乱点。
  - click 缺口目标带入已解析的筛选值，便于搜索后点击与筛选数据匹配的列表行。
  - 无引号且没有路径分隔/导航动词的普通点击不进入 route；BDD 行首关键字只作为语法，不作为目标。
  - 筛选值正则允许邮箱、域名、点号等非空非分隔符值。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_preserve_email_value`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_appends_targets_after_explicit_route`
  - 使用任务 67 最终失败参数离线回放，targets 包含 `coverage_action_1`、`coverage_assert_2`，且筛选值保留 `zhangwenwen@ebupt.com`。

### 19. Coverage feedback 阶段退回通用导航探针，补采缺口变成侧边栏乱点

- 现象：任务 67 使用最新执行器重跑后，旧问题已消失：`And` 不再进入 route，邮箱完整保留，`coverage_action_1/coverage_action_2` 已进入探索目标；但第二轮补采失败为 `页面探索未覆盖 LLM 规划所需的业务目标阶段: coverage_action_1, coverage_action_2`。
- 根因：
  - `_coverage_feedback_exploration_targets()` 把 `fill` 缺口也生成为可点击探索阶段，未声明 observe-only。
  - coverage feedback 阶段在没有直接匹配目标元素时，会继续走 `_rank_navigation_probe()` 的通用导航兜底，导致执行器点击“批量配置管理/客户管理/下载管理”等无关导航，耗尽 `max_steps`。
  - 这是执行链合同边界问题：后端反馈的是“补采某个未绑定动作的证据”，不是“重新全局探索业务导航”。
- 修复：
  - coverage feedback 目标增加 `strict_target_match=True`。
  - `fill/select/assert` 等非点击动作缺口标记为 `observe_only`，通过当前状态/过滤后状态是否出现目标证据完成阶段，不执行无意义点击。

### 20. runtime_agent 对 iframe 内原生 select 反复重规划，已选中值仍被当作定位失败

- 现象：任务 80（厦门企管 / 任务名称 111）已进入新增内容表单，trace 和截图均显示 `投递方式` 下拉可见且当前值为 `主叫彩印`，但运行时智能模式仍对 `gherkin_step_14` 连续 20 轮报 `Timeout waiting for visible locator match: matched=0, scanned=0`。
- 根因：
  - runtime observation 压缩时丢失 select `options`、当前选中值和部分 frame 上下文，planner 无法判断“选择动作已经被当前页面状态满足”。
  - 表单控件候选 label 优先级低于 `name/accessibile_name/text`，无 id/name 的原生 select 会退化成 `role=combobox` 且 name 为整串 option 文本。
  - runtime 执行时传入空 `element_map`，执行器不能使用当前 observation 里的 mapped element、备用 locator、frame 信息和已选中值。
  - 执行器 `_locator_context()` 只信任 `context_type/frame_name/frame_url`，不能从 `element_key=frame_1::...` 推断 iframe。
- 修复：
  - runtime planner 对表单控件优先使用 `label/form_label`，并对 `select_option/choose` 增加当前选中值满足判定。
  - runtime step 保留 `context_type/frame_key/frame_name/frame_url/source_element_key/options/selected_*`。
  - Actuator runtime observation 压缩保留 select options 与 selected/current value。
  - runtime 执行前用当前 observation 构造临时 element map，不复用旧执行状态。
  - 执行器支持从 `frame_key` 或 `frame_1::...` 推断 iframe；原生 select mapped element 已满足目标值时直接成功，未满足时按 option label 映射真实 value 执行。
- 回归：
  - Django：`test_runtime_agent_treats_selected_option_value_as_completed`、`test_runtime_agent_select_step_preserves_frame_context_and_options`。
  - Actuator：`test_runtime_observation_for_plan_preserves_observed_candidates` 扩展 options/selected 断言、`test_runtime_element_map_for_execution_flattens_frame_elements`、`test_locator_context_infers_frame_from_prefixed_element_key`、`test_select_option_succeeds_when_runtime_selected_value_already_matches`。
  - 严格 coverage 阶段禁止退回通用导航探针；click 缺口只允许直接匹配目标/筛选值的候选元素。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_appends_targets_after_explicit_route`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_fill_is_observe_only_and_strict`
  - `PlaywrightExecutorMcpCollectionTest.test_strict_coverage_target_blocks_generic_navigation_probe`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_preserve_email_value`

### 20. 聚合菜单文本压过叶子菜单，导致路由看似覆盖但实际停在错误页面

- 现象：任务 67 修复 coverage feedback 后，`coverage_action_1` 已完成，只剩 `coverage_action_2`。元素地图显示真实页面存在叶子菜单元素 `账号管理`，但执行链实际停在 `/system/role?systemType=MGMT` 的角色管理页，后续找不到账号列表首行。
- 根因：
  - `_rank_exploration_element_for_target()` 对“父级聚合菜单文本包含多个关键词”的累计得分过高。
  - `系统管理 账号管理 角色管理 登录日志 操作日志` 这种祖先 menuitem 同时包含 `系统管理/账号管理/账号/系统`，得分高于真正的叶子菜单 `账号管理`。
  - 执行链把“文本包含目标”误当成“目标叶子已点击”，破坏了 route contract 的阶段边界。
- 修复：
  - 精确标签命中大幅优先，保证可点击叶子项优先于祖先聚合容器。
  - 非精确且命中多个关键词的长文本/大高度元素降权，避免祖先菜单靠聚合文本冒充业务目标。
  - 修复不依赖具体业务词，仅调整元素匹配的结构化优先级。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_exact_route_leaf_scores_above_aggregate_menu_label`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_fill_is_observe_only_and_strict`
  - `PlaywrightExecutorMcpCollectionTest.test_strict_coverage_target_blocks_generic_navigation_probe`

### 21. 显式认证入口污染业务路由，且 route 阶段混入未来标签导致父菜单再次胜出

- 现象：任务 67 使用最新执行器重跑后仍失败：`页面探索未覆盖 LLM 规划所需的业务目标阶段: coverage_action_1, coverage_action_2`。执行日志显示 SSO 已在认证阶段点击成功，但 `business_exploration_plan` 仍包含 `requirement_route_1=SSO登录`；随后业务探索点击了 `系统管理`，再点击父级聚合菜单 `系统管理 账号管理 角色管理 登录日志 操作日志`，页面停在 `/system/role?systemType=MGMT`。
- 根因：
  - 显式认证动作已被 `_perform_explicit_authentication_step()` 消费，但 `_build_exploration_targets()` 在认证前完成，业务探索阶段没有裁剪已消费的认证入口，导致 route 阶段整体错位。
  - 每个显式 route target 的 `keywords` 混入了整条 route 的未来标签，当前阶段不只匹配自己的目标，还会匹配后续叶子目标；父级聚合菜单因为同时包含当前和未来标签，再次获得高分。
  - 这是执行链合同边界问题：认证合同、当前业务路由阶段、未来路由上下文没有分离。
- 修复：
  - `_build_exploration_targets()` 增加 `consumed_route_labels`，认证成功后重建业务探索目标并移除已消费的 route 前缀。
  - 显式 route 阶段的 `keywords` 只保留当前标签及当前前缀路径，不再注入整条 route 的未来标签；未来阶段仍通过 `next_target` 作为上下文参与相关性判断，而不是作为当前阶段直接得分目标。
  - `business_exploration_plan` 改为在认证决策后输出，确保日志反映最终用于业务探索的合同。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`
  - `PlaywrightExecutorMcpCollectionTest.test_route_target_keywords_do_not_include_future_route_labels`
  - `PlaywrightExecutorMcpCollectionTest.test_exact_route_leaf_scores_above_aggregate_menu_label`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_fill_is_observe_only_and_strict`
  - `PlaywrightExecutorMcpCollectionTest.test_strict_coverage_target_blocks_generic_navigation_probe`

### 22. 列表筛选后只等 networkidle，接口已有数据但 DOM 采集仍是空态

- 现象：任务 67 修复认证/路由合同后，已能进入 `/system/account` 并成功填写 `zhangwenwen@ebupt.com` 后点击查询，但仍失败：`coverage_action_1` 和 `coverage_assert_2` 未覆盖。截图 `ai_generation_67_filter_1.png` 显示表格“暂无数据”，但 Playwright trace 中 `/mngr/system/api/v2/account/list` 响应实际返回了包含 `zhangwenwen@ebupt.com` 的 records。
- 根因：
  - `_apply_list_filter_constraints()` 点击查询后只调用 `_wait_for_business_state_stable()`，该等待主要面向页面加载、弹窗和通用异步稳定，不保证筛选结果值已经渲染到表格。
  - 对动态表格来说，`networkidle` 早于 Vue 表格完成渲染时，执行器会采到“空态 DOM”，后续 strict coverage 阶段自然找不到列表首行或详情入口。
  - 这是“筛选动作合同缺少结果渲染确认”的问题，不是业务词匹配问题。
- 修复：
  - 新增 `_wait_for_filtered_list_result()`：筛选后优先等待目标筛选值出现在页面文本中；若超时，再确认 loading 消失并进入稳定行/空态。
  - `_apply_list_filter_constraints()` 查询成功后改用该等待，再重新采集页面状态。
  - 不增加具体业务词或按钮词，只把筛选动作的完成条件从“请求空闲”提升为“结果证据可见或空态稳定”。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_filtered_list_waits_for_expected_value_before_empty_state`

### 23. 筛选后表格文本结果未进入元素地图，strict coverage 无法绑定列表行点击

- 现象：任务 67 修复筛选等待后，截图 `ai_generation_67_filter_1.png` 已显示筛选结果 `zhangwenwen@ebupt.com`，但任务仍失败：`页面探索未覆盖 LLM 规划所需的业务目标阶段: coverage_action_1`。
- 根因：
  - `_apply_list_filter_constraints()` 返回了筛选后的页面状态，但业务探索循环没有把该状态追加到 `pages`，后端规划拿到的仍是筛选前 DOM。
  - 主 DOM 采集器只采 `a/button/input/role/onclick/tabindex` 等原生交互元素；Element Plus/Vue 表格中“看起来可点击”的主文本可能是普通 `span/div` 加样式或事件，截图可见但元素地图没有可绑定原子。
  - 这是“筛选结果状态和表格文本动作没有成为执行链事实”的问题，不是缺业务词规则。
- 修复：
  - strict coverage 阶段采集时把结构化目标关键词传入 DOM 采集器。
  - 筛选后的 `page_filter_*` 状态立即追加到 `pages`，并记录 `business_exploration_filter_capture` 观测。
  - 主 DOM 采集器增加通用表格文本候选：表格行/单元格、pointer/click/link 语义元素按目标关键词提升优先级，作为可点击文本候选进入元素地图。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_native_collection_includes_priority_table_text_candidate`
  - `PlaywrightExecutorMcpCollectionTest.test_filtered_list_waits_for_expected_value_before_empty_state`

### 24. observe-only 断言要求需求短语完整出现在页面，已到目标页仍判未覆盖

- 现象：任务 67 已通过筛选结果行点击进入 `/system/account/detail/481`，截图显示目标页标题、基础信息、用户状态等字段，但探索覆盖仍失败：`coverage_assert_2` 未覆盖。
- 根因：
  - observe-only 阶段只用原始关键词做字符串包含判断；需求目标短语与真实 UI 可见文本存在表达粒度差异。
  - 旧合同把需求句当成 DOM 原文匹配，导致已到达目标状态仍失败。
- 修复：
  - 删除基于固定动作词的短语归一化方案。
  - `_target_state_contains_keyword()` 与直接目标匹配改为比较需求目标文本和已观测 UI 文本的包含关系、公共片段长度。
  - 不增加业务词或指定指令词规则，只处理自然语言需求到 UI 事实之间的通用文本粒度差异。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_observe_target_matches_normalized_ui_phrase`

### 25. 后端计划校验和执行器探索残留指定指令词，导致合同链路被二次词表解释

- 现象：任务 67 页面探索已成功进入业务详情页，但后端 `plan_validation` 报 `missing_authentication_flow`，误判“账号密码登录”未覆盖；同时执行器中仍有若干基于指定指令词解析路由、页面目标和状态迁移的逻辑。
- 根因：
  - 多阶段方案已经让 LLM 输出 `requirement_contract.flow`，但后续 `_semantic_coverage_issues()` 又从原始需求文本里用固定动词/认证词推断动作缺口，绕过了合同。
  - 执行器探索阶段仍用固定指令词切分业务关键词、判定路由意图和状态迁移入口，导致执行链会随着词表膨胀继续反复误判。
  - 这是阶段合同边界问题：自然语言/Gherkin 只能在需求分析阶段被理解一次，后续阶段必须消费结构化合同和页面事实。
- 修复：
  - `_semantic_coverage_issues()` 改为只按 `requirement_contract.flow` 的 `action_id/operation/phase` 校验 required action；没有合同声明时不从原始中文动词反推出新动作。
  - 多阶段 planner 在调用 `_validate_generated_steps()` 时把 `requirement_contract` 传入安全策略副本，保证校验器可消费合同。
  - 执行器探索关键词、路由提取、状态迁移补全改为依赖结构字段、引号目标、路径分隔符、`state_transition` 和后端缺口合同，不再使用指定指令词作为规则。
  - route fallback 进一步收紧为只消费路径分隔符表达；普通引号动作/业务数据不再进入路由，避免业务数据被当成导航阶段。
  - 状态迁移描述和错误消息改为中性结构化措辞，避免日志/提示词再次污染后续 LLM 上下文。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_plan_validation_fails_when_steps_do_not_cover_user_action_intent`
  - `UiAiPlanningElementMapCompactionTests.test_plan_validation_requires_explicit_authentication_flow`
  - `UiAiPlanningElementMapCompactionTests.test_plan_validation_does_not_infer_password_login_for_sso_business_flow`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_gherkin_metadata_comments_and_then_assertions`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_prefers_structured_requirement_document_over_raw_requirement`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`

### 26. 显式认证入口复用业务路由解析，SSO 被排除后误把业务菜单当认证入口

- 现象：任务 67 使用最新执行器 `actuator-60360` 重跑后快速失败：`入口页没有匹配显式认证步骤的可点击元素: 系统管理`。任务的登录页元素地图实际包含可点击 `SSO登录`，所以不是页面元素缺失。
- 根因：
  - 上一轮为禁止普通引号动作进入 route fallback，将单步 `SSO登录` 从业务路由提取中排除，这是正确的。
  - 但 `_perform_explicit_authentication_step()` 仍复用 `_extract_requirement_exploration_route()` 来选择认证入口；业务路由提取不再包含 `SSO登录` 后，第一个 route 变成 `系统管理 -> 账号管理` 的 `系统管理`。
  - 认证成功后的 `consumed_route_labels` 也默认消费 `explicit_route[0]`，存在把业务第一跳错误标记为认证已消费的风险。
  - 这是“认证入口合同”和“业务路由合同”共用同一 fallback 的职责边界问题，不是需要恢复固定指令词规则。
- 修复：
  - 新增 `_extract_explicit_authentication_entry_labels()`：优先消费 `authentication_contract` 中的结构化入口字段；其次只读取第一个显式业务路径之前的结构化步骤/引号目标，作为登录页认证入口候选。
  - `_perform_explicit_authentication_step()` 改用认证入口候选，不再复用业务 route fallback。
  - 认证成功后只有当认证入口标签与业务 route 第一跳完全相同时才写入 `consumed_route_labels`，避免误删 `系统管理` 这类业务第一跳。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_explicit_authentication_step_uses_pre_route_entry_action`
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`
  - `PlaywrightExecutorMcpCollectionTest.test_route_target_keywords_do_not_include_future_route_labels`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_gherkin_metadata_comments_and_then_assertions`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_prefers_structured_requirement_document_over_raw_requirement`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`

### 27. 按高星项目执行模型收敛阶段合同接口，避免解析器跨阶段复用

- 背景：对照 browser-use、Stagehand、Playwright MCP 等同类项目的执行模型，它们核心都是“结构化任务/当前页面状态 -> 动作接口 -> 执行反馈”的循环；自然语言理解和页面动作选择之间有明确接口，不让同一个文本 fallback 同时承担认证、业务路由、绑定等多个阶段。
- 根因风险：
  - 第 26 条已修复具体错误，但执行器仍暴露多个私有解析函数，调用方可以继续绕过阶段合同，后续容易再次出现“认证入口解析复用业务 route 解析”的问题。
  - 这是模块深度不足：阶段合同的复杂性分散在 `executor.py`，接口不够小，维护者需要知道太多内部顺序约束。
- 修复：
  - 新增 `executor_stage_contracts.py`，提供小接口 `build_execution_stage_contract(args)`。
  - 返回结构化 `authentication_entry_labels` 和 `business_route`，执行器只消费这两个合同，不再直接实现认证入口/业务路由的文本解析细节。
  - 保留 `executor.py` 旧私有方法作为兼容适配层，但其实现只转发到阶段合同模块，避免跨阶段复用解析逻辑。
  - 删除执行器中已无调用的 route intent 私有判断，减少误用入口。
  - 模块不引入指定指令词规则，只识别结构化合同字段、Gherkin 元数据、引号目标和路径分隔符。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_execution_stage_contract_separates_authentication_entry_from_business_route`
  - `PlaywrightExecutorMcpCollectionTest.test_explicit_authentication_step_uses_pre_route_entry_action`
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`
  - `PlaywrightExecutorMcpCollectionTest.test_route_target_keywords_do_not_include_future_route_labels`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_gherkin_metadata_comments_and_then_assertions`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_prefers_structured_requirement_document_over_raw_requirement`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`

### 28. 坚持合同分离：执行器和后端规划不再用动作词表二次推断阶段目标

- 背景：第 27 条已把认证入口和业务路由收敛到阶段合同模块，但生产执行链中仍残留若干从原始需求文本或元素文案中按动作词加分、推断表单入口、放行确认动作的逻辑。这类逻辑会绕过 `requirement_contract.flow`，使后续修复再次滑回“补词表”的循环。
- 根因：
  - 执行器目标生成还会从需求文本里按动作词推断表单探索阶段，和后端 LLM 已输出的动作合同并行存在。
  - 元素排序、frame 候选、状态迁移兜底和后端元素裁剪仍有固定动作词加分，导致页面事实会被词表而不是合同动作牵引。
  - 后端低风险确认动作判断仍用任务文本和步骤文本的固定词组合，未优先消费 `requirement_contract.flow`。
- 修复：
  - `executor_stage_contracts.py` 增加 `flow_actions`，统一从 `requirement_contract.flow` 输出 required 动作合同。
  - `_build_exploration_targets()` 改为把 `flow_actions` 中的 click/choose 动作编译为探索目标；不再从原始需求文本推断表单入口。
  - 删除执行器生产链路中的动作词加分、frame 动作词匹配、状态迁移标签兜底推断；确认动作只按 `safety_policy.allow_form_submit` 与安全策略处理。
  - 后端 `_extract_flow_keywords()` 和低风险确认判断改为基于结构化合同和元素事实，不再从自然语言动作词提取规划目标。
  - 执行链相关生产文件中指定动作词静态搜索为空，避免提示词/日志再次污染后续 LLM 上下文。
  - 模块目标阶段只允许结构化目标本身或导航语义元素参与匹配，避免普通按钮因包含模块文本误命中父阶段。
  - fallback 测试计划中的表单字段筛选只在 `requirement_contract.flow` 明确指定字段时启用；合同为空时按元素地图事实保留必填字段。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_execution_stage_contract_exports_flow_actions_for_exploration`
  - `PlaywrightExecutorMcpCollectionTest.test_execution_stage_contract_separates_authentication_entry_from_business_route`
  - `PlaywrightExecutorMcpCollectionTest.test_explicit_authentication_step_uses_pre_route_entry_action`
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`
  - `PlaywrightExecutorMcpCollectionTest.test_route_target_keywords_do_not_include_future_route_labels`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_gherkin_metadata_comments_and_then_assertions`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_prefers_structured_requirement_document_over_raw_requirement`
  - `PlaywrightExecutorMcpCollectionTest.test_requirement_route_ignores_unquoted_row_selection_and_bdd_keyword`
  - `PlaywrightExecutorMcpCollectionTest.test_observe_target_matches_normalized_ui_phrase`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_fill_is_observe_only_and_strict`
  - `PlaywrightExecutorMcpCollectionTest.test_strict_coverage_target_blocks_generic_navigation_probe`
  - `PlaywrightExecutorMcpCollectionTest.test_mcp_test_plan_prefers_structured_contract_over_requirement_text`
  - `PlaywrightExecutorMcpCollectionTest.test_read_only_target_does_not_use_form_completion_condition`
  - `PlaywrightExecutorMcpCollectionTest.test_dialog_assertion_does_not_create_a_form_exploration_stage`
  - `PlaywrightExecutorMcpCollectionTest.test_exploration_targets_prefer_module_before_create_entry`
  - `PlaywrightExecutorMcpCollectionTest.test_mcp_script_prefers_business_dialog_over_login_page`
  - `PlaywrightExecutorMcpCollectionTest.test_mcp_test_plan_binds_required_form_steps_to_element_map`
  - `PlaywrightExecutorMcpCollectionTest.test_mcp_test_plan_preserves_transition_into_dialog_state`
- 全量回归现状：
  - 宿主侧执行 `./.venv/bin/python -m unittest test_executor_mcp_collection` 可正常启动 Chromium；剩余失败集中在旧测试对官方 MCP 降级文案、导航等待策略和 FakeLocator 行为的断言，不属于本次合同分离链路。

### 29. 最终计划编译允许 LLM 重排真实探索轨迹，生成未观察状态迁移并漏掉必需动作

- 现象：任务 68 页面探索已完成 SSO、业务路由、筛选和结果点击，但后端最终 `plan_validation` 失败：
  - `unobserved_state_transition`：最终步骤声明了元素地图中不存在的状态迁移边。
  - `staged_required_action_missing`：最终计划缺少合同中的必需动作 `action_4`。
- 根因：
  - element binding 和 executable plan generation 仍允许 LLM 把真实探索轨迹重新编排成最终 steps。
  - 后端已有 `_compile_selected_route_steps()` 只覆盖 route 阶段选中的 transition，未按 `requirement_contract.flow.action_id` 对最终步骤做确定性编译。
  - `_standardize_staged_contract_steps()` 会按 binding 再次覆盖 `page_key/element_key/locator_hint`，导致前置编译后的真实 transition 仍可能被错误 binding 覆盖。
- 修复：
  - 新增 `_compile_flow_transition_steps()`：以 `requirement_contract.flow` 的 required click action 为合同，以 `element_map.state_transitions` 为唯一状态迁移证据，按 `action_id` 生成/替换最终 click transition。
  - 对已有步骤中的未观察 `state_transition` 先去除，再用真实 observed transition 重新编译，禁止 LLM 伪造迁移边进入最终计划。
  - `compile_route()` 先执行 route transition 编译，再执行 flow transition 编译。
  - staged 初次生成和 repair 分支在 `_standardize_staged_contract_steps()` 之后再次执行 `compile_route()`，确保最终校验、脚本生成前仍是 observed transition 版本。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action`
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_runs_after_binding_standardization`
  - 真实后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_runs_after_binding_standardization --keepdb`
  - 结果：2 tests OK。
  - 真实任务 68 回归：
    - 已重启 Django/Celery 并使用最新后端代码，执行器 `actuator-66346` 已在线。
    - 任务重新入队后失败点变为 `exploration_coverage`，错误为 `页面探索未覆盖 LLM 规划所需的业务目标阶段: requirement_route_1, requirement_route_2`。
    - 最新 element_map 只有 1 个页面、0 条 `state_transitions`，页面标题为 `502 Bad Gateway`。
    - WSL 与 Windows 侧直接访问 `https://10.1.60.127:6004/login` 均返回 HTTP 502。
    - 结论：真实任务当前被目标业务入口 502 阻断，未进入后端 LLM 最终计划编译阶段；不能用这次真实任务结果判定合同编译修复失败。
  - 真实任务 68 复测：
    - WSL 与 Windows 侧直接访问 `https://10.1.60.127:6004/login` 均已恢复 HTTP 200。
    - 任务重新入队后失败点变为 `permission`，错误为 `显式认证步骤执行后仍停留在认证入口页`。
    - 最新 element_map 仍只有 1 个页面、0 条 `state_transitions`，页面标题为 `CMI管理平台 - 登录`，页面元素包含用户名、密码、`SSO登录`、登录按钮。
    - `verification_result.checked_url` 包含 CAS ticket 参数，但采集页面仍是登录页。
    - 结论：复测已越过 502 阻断，但当前被显式认证前置阻断，尚未进入业务页面探索和后端最终计划编译阶段；不能用这次真实任务结果判定第 29 条合同编译修复失败。

### 30. 最终计划 repair 后仍允许非 transition 动作破坏 flow 顺序

- 现象：任务 68 切换到 `https://10.1.60.127:6006/` 后，认证和业务探索已成功，后端最终 `plan_validation` 失败：
  - `staged_action_order_mismatch`：最终计划实际顺序为 `action_1, action_2, action_3, action_4, action_5, assert_4, explicit_action_1, action_6`。
  - 期望顺序为 `action_1, action_2, action_3, action_4, action_5, action_6, assert_4, explicit_action_1`。
- 根因：
  - 第 29 条只把 observed state transition 的 click 动作确定性编译回来，但没有把 fill/assert/runtime_text 等非 transition 动作一起纳入最终排序编译。
  - `standardize_staged_contract_steps()` 虽然按 `requirement_contract.flow` 排序，但后续 LLM repair/transition 编译/artifact 附加前仍可能把断言放到依赖动作前。
  - 重复认证入口动作和已有认证点击绑定到同一元素时，执行计划应合并执行证据，而不是在业务流程后半段再次点击。
- 修复：
  - 新增 `_compile_final_contract_step_order()`：最终可执行 steps 以 `requirement_contract.flow.action_id` 为主序，`goto/wait` 辅助步骤保持前置，非合同步骤保持原相对位置。
  - 重复 click 不按业务词判断，只按 `operation + page_key + element_key + locator_hint` 身份去重；被合并的合同动作写入 `covered_action_ids`。
  - `staged_plan_contract_issues()` 识别 `covered_action_ids`，允许一个真实执行 step 覆盖等价重复合同动作。
  - `_renumber_plan_steps()` 恢复合同边界保护：不同 `action_id` 的相邻 click 不再被普通去重误删，除非进入最终排序编译并显式记录覆盖关系。
  - `compile_route()` 在 route transition 编译和 flow transition 编译之后统一调用最终顺序编译，确保 LLM plan/repair 只能提供候选，最终 steps 由 deterministic compiler 收口。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click`
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action`
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_runs_after_binding_standardization`
  - 真实后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_runs_after_binding_standardization ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click --keepdb`
  - 结果：3 tests OK。
  - 真实任务 68 回归：
    - 已重启 Django/Celery，执行器 `actuator-66346` 已重新连接新后端。
    - 任务重新入队后执行器已 ack，但失败提前发生在认证阶段：`显式认证步骤执行后仍停留在认证入口页`。
    - 最新元素地图只有登录页 1 页、0 条 `state_transitions`，未进入业务探索和后端最终计划编译阶段。
    - 当前 `target_url` 为 `https://10.1.60.127:6006/`，页面实际元素为用户名、密码、登录、忘记密码；任务需求仍声明“通过 SSO 登录/点击 SSO 登录按钮”，环境配置也未提供账号密码。
    - 结论：本轮真实回归被“用例认证方式与 6006 入口页不一致”阻断，属于认证合同/环境前置失败；不能用它判定第 30 条最终顺序编译修复失败。
  - 真实任务 68 再次回归：
    - 用户确认 6006 存在 SSO 入口后重新入队任务 68，执行器本轮成功采集到 SSO 并进入业务流程。
    - 最终状态：`status=success`，`dispatch_status=completed`，`verification_result.status=success`。
    - 元素地图：`pages=5`，`state_transitions=3`。
    - 最终静态校验：`plan_validation.status=success`，`semantic_issues=[]`，`unresolved_step_count=0`。
    - 最终执行到达：`https://10.1.60.127:6006/system/account/detail/481`。
    - 最终步骤顺序为 `action_1 -> action_2 -> action_3 -> action_4 -> action_5 -> action_7 -> assert_4`，其中 `action_2` 覆盖重复认证动作 `explicit_action_1`，未复现 `staged_action_order_mismatch`。
    - 结论：第 30 条最终顺序编译收口修复已通过任务 68 真实回归。

### 31. AI 生成验证链路与应用后传统 UI 用例执行链路操作合同不一致

- 现象：AI 生成任务真实验证成功，应用为 UI 用例后再执行失败。
- 根因：
  - AI 生成验证走 `generated_case.steps -> verify_ai_generated_flow() -> _execute_ai_plan_step()`，消费 `locator_hint`、`element_map`、`state_transition`、runtime binding 和 AI 计划操作名。
  - 应用为 UI 用例后走 `UiPageStepsDetailed -> execute_test_case() -> _execute_step()`，消费传统 `UiElement.locator_*` 和 `ope_key`。

### 32. 泛化父级路由被品牌链接/无关菜单劫持，coverage repair 在错误页面补采

- 现象：任务 70（333）已通过显式认证进入业务站点，但业务探索第一跳把泛化父级 route 匹配到站点品牌链接，后续又进入无关页面，最终报 `页面探索未覆盖 LLM 规划所需的业务目标阶段: coverage_action_2, coverage_action_3, coverage_action_5`。
- 根因：
  - `requirement_route` 的 `keywords` 混入了 `route_path` 前缀，导致下一跳目标携带父级泛词，所有包含父级片段的导航项都被误判为下一跳相关。
  - 短 route 片段的非精确子串匹配没有结构化约束，带 `href` 的品牌/首页链接也能被当成业务导航阶段的直接命中。
  - route 探索点击后只要 URL 或页面签名变化就提交为真实迁移，即使点击结果没有暴露下一跳证据；错误迁移进入 `element_map.state_transitions` 后，coverage repair 只能在错误页面继续补采。
- 修复：
  - 显式 route target 的 `keywords` 只保留当前阶段标签线索，`route_path` 只作为顺序/前缀元数据，不再参与当前/下一跳元素直接匹配。
  - route 元素匹配统一使用当前标签线索；短片段非精确命中时，带 `href` 的链接不能直接冒充 route 节点，必须是菜单/标签等导航控件。
  - 父级 route 存在下一跳时，点击后必须在新状态中观察到下一跳目标证据才算完成；未完成的探测不会提交为真实 page/transition，并会回退到点击前 URL 继续尝试其他候选。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_generic_route_parent_does_not_match_brand_link`
  - `PlaywrightExecutorMcpCollectionTest.test_parent_route_requires_next_target_evidence_before_completion`
  - `PlaywrightExecutorMcpCollectionTest.test_route_target_keywords_do_not_include_future_route_labels`
  - `PlaywrightExecutorMcpCollectionTest.test_exact_route_leaf_scores_above_aggregate_menu_label`
  - `PlaywrightExecutorMcpCollectionTest.test_consumed_authentication_route_is_removed_from_business_targets`
  - `PlaywrightExecutorMcpCollectionTest.test_llm_coverage_feedback_fill_is_observe_only_and_strict`
  - `PlaywrightExecutorMcpCollectionTest.test_strict_coverage_target_blocks_generic_navigation_probe`
  - `apply_to_ui_case()` 将 AI 操作 `select_option` 原样落库为 `ope_key='select_option'`，但传统执行器 `_execute_step()` 只支持 `select`，导致生成链路能跑、落库用例不能跑。
- 修复：
  - 应用转换层将 AI 计划里的 `select_option` 编译为传统执行器可执行的 `select`，同时保留 `ope_value.value`。
  - 执行器传统 `_execute_step()` 兼容已落库旧数据：运行时将 `select_option` 归一为 `select`。
  - 修复点只处理执行合同兼容，不增加任何业务文案或指定指令词规则。
- 回归：
  - `UiAiGenerationApplyCrossPageTests.test_apply_compiles_ai_select_option_to_legacy_select_operation`
  - `UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_input_binding_metadata`
  - 真实后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_input_binding_metadata ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_compiles_ai_select_option_to_legacy_select_operation --keepdb`
  - 结果：2 tests OK。
  - 已重启 Django 和执行器，当前执行器 ID 为 `actuator-5125`。

### 32. 显式认证动作没有 state_transition 时，最终 transition 编译把业务迁移错配给认证 action

- 现象：此前已成功的任务 68（任务名 `222`）重新启动后失败，错误为：
  - `最终计划动作 action_2 未复用元素绑定阶段的 page_key/element_key`
  - `最终计划动作 action_3 未复用元素绑定阶段的 page_key/element_key`
  - `最终计划缺少必需动作 action_4`
- 根因：
  - 本轮元素地图只有业务菜单迁移 `系统管理`、`账号管理` 两条 `state_transitions`。
  - `SSO登录` 在显式认证阶段执行成功，但没有作为业务 `state_transition` 写入迁移证据。
  - `_compile_flow_transition_steps()` 只按 requirement 文本和 observed transition 编译 click 动作，未受 `binding_contract` 约束；当认证 action 没有 observed transition 时，后续业务 transition 可能被错配到前面的 action_id，导致 action_id 整体前移。
- 修复：
  - `_compile_flow_transition_steps()` 增加可选 `bindings` 参数。
  - 当某个 `action_id` 已有元素绑定时，只允许使用与该绑定 `page_key/element_key` 匹配的 observed transition。
  - 如果绑定动作没有对应 transition，例如显式认证入口，只保留绑定步骤，不用后续业务迁移顶替。
  - staged planner 的 `compile_route()` 将当前 `binding_contract` 传入 flow transition 编译。
  - 修复不依赖 SSO、系统管理、账号管理等业务文案，只使用结构化绑定合同约束迁移编译。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_respects_binding_contract_when_auth_transition_is_not_observed`
  - `UiAiPlanningElementMapCompactionTests.test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click`
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action`
  - 真实后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_respects_binding_contract_when_auth_transition_is_not_observed ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_replaces_unobserved_edge_and_restores_missing_action --keepdb`
  - 结果：3 tests OK。
  - 已重启 Django，任务 68 重新回归成功：
    - `status=success`
    - `dispatch_status=completed`
    - `plan_validation.status=success`
    - `semantic_issues=[]`
    - 最终步骤保持绑定合同：`action_2=page_1/button_3/SSO登录`，`action_3=系统管理`，`action_4=账号管理`。

### 33. 传统 UI 用例文本断言把语义定位降级为过期 XPath

- 现象：任务 68 生成并应用出的 UI 用例 `222` 执行失败，结果为 `通过 6/7`；trace 截图可见期望文本，但最后一步报 `Locator expected to have text ... element(s) not found`，实际使用的是绝对 XPath。
- 根因：
  - AI 生成验证链路使用 `locator_hint/element_map` 的语义定位；应用为传统 UI 用例后，步骤改由 `UiElement.locator_*` 驱动。
  - 应用落库时断言元素主定位器是 `text`，但同时保存了 CSS/绝对 XPath 备用定位器。
  - 传统执行器 `_execute_step()` 对所有操作共用同一套定位器降级策略；当配置定位器不可见时，会把最后一个失败定位器继续交给断言执行。文本断言因此从“页面是否出现期望文本”的语义判断退化成了“某个历史 DOM 坐标是否还存在”。
  - 回归后进一步确认：主 `text` locator 并非找不到，而是在列表/详情中匹配到多个相同文本，Playwright strict mode 阻止直接 `wait_for`。旧执行器把这种“多候选文本集合”当失败处理，继续降级到 CSS/XPath。
- 修复：
  - `_execute_step()` 对无显式下标的 `text` locator 先调用 `_first_visible_locator()`，解析为第一个可见候选，再执行 click/assert，避免 strict mode 把多文本匹配误判为定位失败。
  - `_execute_step()` 在 `assert_text/assert_contain_text` 的所有配置定位器均不可见时，使用 `step.input_value` 构造文本语义 locator 兜底验证。
  - `assert_text` 兜底使用精确文本可见性，`assert_contain_text` 兜底使用包含文本可见性。
  - 如果页面没有期望文本，仍返回失败，并带出最后一个配置定位器错误；不按任何具体业务文案放行。
- 回归：
  - `test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_text_locator_resolves_first_visible_match_before_clicking`
  - `test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_text_assertion_falls_back_to_expected_text_when_configured_locators_are_stale`
  - 真实执行器虚拟环境执行：
    `PYTHONPATH=/mnt/d/Project:/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_text_locator_resolves_first_visible_match_before_clicking test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_text_assertion_falls_back_to_expected_text_when_configured_locators_are_stale test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_runtime_image_input_is_resolved_before_fill test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_structured_role_locator_preserves_accessible_name`
  - 结果：4 tests OK。

### 34. staged planner 识别 runtime_text 语义失败后没有进入 plan repair

- 现象：任务 70（333）已进入新建账号页并完成关键 locator 校验，但最终 `plan_validation` 失败：
  - `runtime_assertion_text_not_observed`
  - `semantic_replan_history=[]`
- 根因：
  - `runtime_assertion_text_not_observed` 已被 `_validate_generated_steps()` 识别为可重规划语义问题。
  - monolithic planner 有 `_should_retry_semantic_planning()` repair loop，但 staged planner 的最终 repair loop 只在 `unresolved_step_count` 或 staged contract issue 存在时继续。
  - 当失败是纯 semantic issue 时，staged planner 直接返回失败候选，导致 LLM 已识别的页面事实 grounding 缺口没有进入最终计划修复。
- 修复：
  - staged planner 最终 repair loop 复用 `_should_retry_semantic_planning()` 判断可重规划语义失败。
  - 对 semantic-only failure 只重跑 plan generation，不重新解释业务词、不新增业务文案规则、不放宽页面事实校验。
- 回归：
  - 新增/通过 `UiAiPlanningElementMapCompactionTests.test_staged_planner_retries_runtime_text_assertion_semantic_failure`。
  - 通过 `test_runtime_text_assertion_must_match_fresh_runtime_page_when_available`。
  - 通过最终顺序编译和绑定合同相关回归 3 个用例。

### 35. TypeScript 真实执行失败后直接退回全链重规划，缺少局部 spec repair 闭环

- 现象：任务 70（333）已经进入目标业务页面，失败点是最终 TypeScript spec 的运行时断言文本没有当前页面事实支撑；旧链路会把这类运行时失败交给外层完整重规划，容易把已真实探索到的页面轨迹重新打乱。
- 根因：
  - `_run_typescript_spec_artifact()` 原先只保证单个 spec 来源可审计，但缺少“失败现场 reobserve -> LLM patch -> 重跑 spec”的局部闭环测试约束。
  - 这会让执行链在局部断言/locator 失败时回到全量 planning，和 GitHub 同类项目中常见的 observe-act-repair 小步闭环不一致。
- 修复：
  - TypeScript spec 执行阶段允许最多 `max_repair_rounds + 1` 次可审计尝试，上限 3 次。
  - 每次可修复失败先采集失败现场 `failure_reobserve`，再请求后端 `llm_typescript_spec_patch`，patch 成功后更新 spec、files、hash 和 artifacts，并继续下一次真实运行。
  - patch 不可用或失败不可修复时，仍保留失败证据交给外层处理；不在执行器里静默改脚本，也不增加任何业务词/动作词规则。
- 回归：
  - `TaskConsumerFullReplanTests.test_typescript_failure_uses_local_llm_patch_before_full_replan`
  - `PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/test_consumer_ai_reobserve.py`
  - 结果：20 tests OK。

### 36. runtime_text 断言缺少结构化 reveal 原语，LLM patch 只能反复换 locator

- 现象：任务 70（333）在新建账号页失败于运行时文本断言。页面事实中没有完整断言文本，但存在同表单相邻的可点击说明入口；第一轮 LLM repair 只把 exact 文本断言改成局部 contains 文本断言，第二轮仍失败。随后 LLM 网关返回 402 余额不足，无法继续 repair。
- 根因：
  - 执行链已有 `runtime_assertion_text_not_observed` 校验，但编译层没有表达“先触发低风险 reveal，再验证文本”的原语。
  - TypeScript spec 仍把 runtime_text 编译为文本 locator 断言；当目标文本需要由当前页的辅助控件展开时，只会在 locator 层反复修补。
  - 这和 GitHub 同类项目的 observe-act-repair 思路不一致：页面事实应先决定可执行动作空间，LLM patch 不能替代确定性的低风险动作原语。
- 修复：
  - 新增结构化 `_runtime_assertion_reveal_candidates()`：只基于 role/actions/href/visible/enabled/bounding_box/form proximity/locator_candidates 选择同页低风险可点击候选，不使用业务词或固定动作词表。
  - `_validate_generated_steps()` 对未观察 runtime_text 断言，如果存在结构化 reveal 候选，则允许计划进入执行；没有候选时仍按 `runtime_assertion_text_not_observed` 失败，不放宽事实校验。
  - `_build_typescript_spec_from_steps()` 对 runtime_text 断言先编译 `app.revealRuntimeText()`，再用页面正文包含断言验证，避免继续对不存在的文本 locator 做 exact/contains 变体补丁。
  - TypeScript helper 增加 `revealRuntimeText()`：若文本已可见直接返回，否则逐个点击编译期给出的低风险候选；点击后仍未出现文本则交给断言真实失败。
- 回归：
  - `UiAiPlanningElementMapCompactionTests.test_runtime_text_assertion_allows_structural_reveal_candidate`
  - `UiAiPlanningElementMapCompactionTests.test_runtime_text_assertion_must_match_fresh_runtime_page_when_available`
  - `UiAiPlanningElementMapCompactionTests.test_staged_planner_retries_runtime_text_assertion_semantic_failure`
  - `UiAiPlanningElementMapCompactionTests.test_final_contract_order_compiler_restores_flow_order_and_covers_duplicate_click`
  - `UiAiPlanningElementMapCompactionTests.test_flow_transition_compiler_respects_binding_contract_when_auth_transition_is_not_observed`
  - 结果：5 tests OK。
  - 使用任务 70 真实 `generated_case + element_map` 离线重编译：`plan_validation.status=success`，生成 spec 包含 `revealRuntimeText(...)` 和 `page.locator('body')` runtime_text 断言。
  - 端到端真实回归暂被外部 LLM 网关 `HTTP 402 insufficient_balance` 阻断，需恢复 LLM 配额后重新入队验证。

### 37. 应用为 UI 用例后没有复用 AI 生成链 artifact，手动执行退回传统 PageStep runner

- 现象：任务 70（333）AI 生成链路已成功，`typescript_spec_execution=success`；应用为 UI 用例后手动执行 `case_id=22` 失败，结果为 `通过 8/10`。失败点包括：
  - Element UI/ARIA combobox 被传统 runner 当原生 `<select>` 调用 `Locator.select_option`。
  - runtime_text 断言没有执行 TypeScript spec 中的 `revealRuntimeText()`，退化成普通文本断言。
- 根因：
  - AI 生成链的最终事实源是 `generated_case/playwright_ts_spec/playwright_ts_files`，并由 TypeScript spec runner 真实执行裁决。
  - `apply_to_ui_case()` 只把生成步骤拆成 `UiPageStepsDetailed`，`UiTestCase.case_flow` 仅保存旧 Python 脚本，手动执行入口 `execute_test_case()` 仍只消费 `testcases/<id>/execute-data/` 的 PageStep 结构。
  - 因此“创建任务执行”和“应用后手动执行”不是同一执行链；修传统控件策略只能缓解单点症状，不能保证 AI 生成通过的执行语义在 UI 用例中保持一致。
- 修复：
  - `apply_to_ui_case()` 新增 AI 执行 artifact 构建，把最终 `playwright_ts_spec/playwright_ts_files/generated_case` 写入 `UiTestCase.result_data.ai_execution_artifact` 和 `UiPageSteps.flow_data.ai_execution_artifact`。
  - `UiTestCaseViewSet.execute_data()` 对修复前已应用的历史用例，按 `page_step.flow_data.source=ai_generation_task/task_id` 动态回填 AI artifact，避免必须重新应用。
  - 执行器 `TestCaseConfig` 增加 `ai_execution_artifact`。
  - `TaskConsumer.execute_test_case()` 和批量执行发现 artifact 时优先复用现有 `_run_typescript_spec_artifact()`，再转换为普通 `CaseResultModel` 返回；没有 artifact 的用例继续走传统 PageStep runner。
  - 修复不新增业务词、动作词、认证词或控件文案规则；同步的是执行链事实源。
- 回归：
  - `TaskConsumerFullReplanTests.test_build_test_case_config_extracts_ai_execution_artifact`
  - `TaskConsumerFullReplanTests.test_ai_applied_case_execution_uses_typescript_artifact_runner`
  - `UiPageStepsExecuteDataTests.test_testcase_execute_data_backfills_ai_execution_artifact_from_source_task`
  - 执行器目录执行：
    `./.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_build_test_case_config_extracts_ai_execution_artifact test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_ai_applied_case_execution_uses_typescript_artifact_runner`
  - 结果：2 tests OK。
  - 后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test --keepdb ui_automation.tests.UiPageStepsExecuteDataTests.test_testcase_execute_data_backfills_ai_execution_artifact_from_source_task`
  - 结果：1 test OK。

### 38. AI artifact 手动执行成功但执行步骤和 trace 证据没有展示

- 现象：应用为 UI 用例后手动执行显示成功，但最新执行记录只有 1 条 `AI TypeScript spec 执行合同` 汇总步骤，`screenshots=[]`，`trace_path=None`，前端详情和 trace 截图均无可展示证据。
- 根因：
  - 第 37 条已把手动执行入口切到 AI TypeScript artifact runner，但结果回填合同没有同步升级。
  - `_run_typescript_spec_artifact()` 内部已经上传 trace，并把 `typescript_spec_execution.trace_path` 设置为服务端相对路径，同时删除本地 trace 文件。
  - 外层 `execute_test_case()` 不区分本地文件路径和服务端相对路径，又对 `result.trace_path` 调用 `_upload_trace_file()`；服务端相对路径在执行器本地不存在，于是被误判为上传失败并清空。
  - `_execute_ai_artifact_test_case()` 只把整个 TypeScript spec 运行结果压成 1 条汇总步骤，未把 `generated_case.steps` 转成执行记录步骤，导致展示层看起来“步骤全没了”。
- 修复：
  - 新增 `_is_local_artifact_path()`，只有 trace 路径确实是执行器本地存在文件时才上传；已经是服务端 URL/相对路径的 trace 直接保留。
  - `execute_test_case()` 和批量执行分支都使用该判定，避免二次上传已上传 trace。
  - 新增 `_build_ai_artifact_step_results()`，把 AI 生成链的 `generated_case.steps` 编译为 `StepResultModel` 列表，手动执行记录不再只有单条汇总。
  - AI artifact 执行记录的 `step_id` 使用展示顺序号，避免生成计划内部从 0/1 混用时前端 key 冲突。
  - 修复只调整执行产物合同，不增加业务词、按钮词、认证词或指定指令规则。
- 回归：
  - `TaskConsumerFullReplanTests.test_ai_applied_case_execution_uses_typescript_artifact_runner`
  - `TaskConsumerFullReplanTests.test_server_trace_path_is_preserved_when_sending_case_result`
  - 执行器目录执行：
    `PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_ai_applied_case_execution_uses_typescript_artifact_runner test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_server_trace_path_is_preserved_when_sending_case_result`
  - 结果：2 tests OK。

### 39. 列表行级目标动作前没有稳定消费实体筛选合同

- 现象：最新生成任务 71（444）已完成显式认证，并成功进入角色管理列表页，但最终失败：
  - `页面探索未覆盖 LLM 规划所需的业务目标阶段: coverage_action_1, coverage_action_2`
  - 元素地图停在列表空状态，未采集到目标实体行、行内操作和后续页面动作。
- 根因：
  - 需求理解阶段已经抽取出 `list_filter_constraints`，但字段合同为“定位/点击某字段名”这类自然语言短语时，执行器只做 `field in observed_control_text` 的单向匹配。
  - 真实页面控件标签是更短、更结构化的字段名；当页面事实字段包含在合同短语内部时，筛选字段绑定失败，导致没有填筛选条件、没有触发查询、没有重采列表。
  - 进一步真实回归确认：任务安全策略里的 `max_steps=2` 被直接当成业务探索总点击预算，两个路由阶段用完后，coverage 阶段根本没有进入 while 循环；因此即使字段匹配能力存在，也没有机会执行筛选原语。
  - 后续 strict coverage action 只能在空列表事实上找行内动作，必然失败。
- 修复：
  - 新增 `_list_filter_field_match_score()`，用结构化页面事实做字段匹配：支持合同字段与真实控件标签双向包含和高置信字符重合。
  - `_apply_list_filter_constraints()` 改为按匹配分数选择 fillable 控件，并在命中后继续执行已有的填值、触发查询、等待列表刷新、重采页面流程。
  - `_auto_explore_business_pages()` 的内部探测预算改为至少覆盖所有 required stage，避免前置路由点击把行级 coverage / 筛选 / 重采集原语饿死。
  - `_coverage_feedback_exploration_targets()` 将筛选值写入 `context_keywords`，作为行上下文，而不是和真实动作目标混为同等语义。
  - `_non_form_target_completed()` 增加点击元素和下一目标证据判断：如果点击的元素只证明上下文实体，不能立即标记 coverage 完成，必须看到下一阶段目标证据或真实状态变化。
  - `_target_keyword_matches_observed_text()` 增加英文短 UI 标签的词边界匹配，允许短按钮/控件标签匹配复合需求短语；这是语言归一化，不是业务词规则。
  - `_target_element_matches_only_context()` 改为检查复合目标里“去掉上下文后的增量证据”，避免上下文实体值通过宽松子串/LCS 匹配冒充目标动作。
  - 修复不增加目标业务词、按钮词或具体用例规则；只补齐“列表实体筛选 -> 重采列表 -> 行级动作覆盖”的通用原语入口。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_field_matches_observed_control_inside_contract_phrase`
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_apply_when_contract_field_contains_observed_label`
  - `PlaywrightExecutorMcpCollectionTest.test_business_exploration_keeps_probe_budget_for_row_level_coverage_after_routes`
  - `PlaywrightExecutorMcpCollectionTest.test_filtered_list_waits_for_expected_value_before_empty_state`
  - `PlaywrightExecutorMcpCollectionTest.test_context_only_entity_click_does_not_complete_coverage_without_next_evidence`
  - `PlaywrightExecutorMcpCollectionTest.test_short_observed_action_label_matches_compound_requirement_phrase`
  - 执行器目录执行：
    `PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_field_matches_observed_control_inside_contract_phrase test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_apply_when_contract_field_contains_observed_label test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_filtered_list_waits_for_expected_value_before_empty_state`
  - 结果：3 tests OK。
  - 追加执行：
    `PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_business_exploration_keeps_probe_budget_for_row_level_coverage_after_routes test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_field_matches_observed_control_inside_contract_phrase test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_apply_when_contract_field_contains_observed_label`
  - 结果：3 tests OK。
  - 再追加执行：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_business_exploration_keeps_probe_budget_for_row_level_coverage_after_routes test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_field_matches_observed_control_inside_contract_phrase test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_apply_when_contract_field_contains_observed_label test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_context_only_entity_click_does_not_complete_coverage_without_next_evidence test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_short_observed_action_label_matches_compound_requirement_phrase`
  - 结果：5 tests OK。

### 40. AI 生成结果通过超大 WebSocket payload 回传时阻塞执行器队列

- 现象：重启最新执行器 `actuator-87964` 后回归 222/333/444，任务 68 已完成浏览器探索、trace 上传和 TypeScript 浏览器执行，但日志长期停在 `active_task_ids=68`，后续任务不能进入执行。
- 根因：
  - 执行器进程无 Chromium/Node 子进程残留，只剩到 Django 的 WebSocket 连接，说明卡点不在页面执行或 LLM，而在结果传输/回填阶段。
  - `WebSocketClient.send()` 没有 timeout，AI 生成结果包含完整元素地图、截图、artifact 和报告，payload 过大时可能让 `await websocket.send(...)` 无限阻塞。
  - 任务 active 集合只有 `_route_task()` 返回后才释放，因此结果传输阻塞会拖死整个执行器队列。
- 修复：
  - 后端抽出 `save_ai_generation_result_sync()`，让 WebSocket 和 HTTP 都复用同一套 AI 生成结果保存逻辑。
  - `UiAiGenerationTaskViewSet` 新增 `report-result` action，执行器可通过本地 HTTP API 上报完整结果。
  - 执行器 `_submit_ai_generation_result()` 优先 HTTP 上报完整结果；HTTP 失败才降级 WebSocket。
  - `WebSocketClient.send()` 增加发送超时，防止任何结果/ACK 传输永久占住任务线程。
  - 修复不改变业务规划、元素绑定或动作匹配规则，只修“结果传输不能阻塞执行链”的通用运行合同。
- 回归：
  - `TaskConsumerFullReplanTests.test_ai_generation_result_prefers_http_report_over_websocket_payload`
  - `TaskConsumerFullReplanTests.test_ai_generation_result_falls_back_to_websocket_when_http_report_fails`
  - `PlaywrightExecutorMcpCollectionTest.test_business_exploration_keeps_probe_budget_for_row_level_coverage_after_routes`
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_field_matches_observed_control_inside_contract_phrase`
  - `PlaywrightExecutorMcpCollectionTest.test_list_filter_constraints_apply_when_contract_field_contains_observed_label`
  - `PlaywrightExecutorMcpCollectionTest.test_context_only_entity_click_does_not_complete_coverage_without_next_evidence`
  - `PlaywrightExecutorMcpCollectionTest.test_short_observed_action_label_matches_compound_requirement_phrase`
  - 结果：2 tests OK + 5 tests OK。

### 41. 模式一人工采集元素地图缺少端到端执行链合同

- 现象：444 这类用例在预探索阶段生成的元素地图，和用户真实操作后的页面状态不一致；继续补通用判据会让执行链臃肿，且新用例仍可能因为 iframe、弹窗、列表刷新、路由变化导致 locator 过期。
- 根因：
  - 元素地图来源没有一等合同字段；已有 `element_map` 只作为确认上下文传入执行器，执行器仍会重新自动探索并覆盖事实源。
  - 缺少人工采集任务入口，用户无法把真实操作路径沉淀为 `pages/state_transitions/action_trace` 后再交给 AI 生成链消费。
  - coverage repair 对所有地图来源一视同仁；人工 map 缺证据时仍可能退回自动 re-explore，违背“稳定优先、缺证据明确失败”的模式一目标。
- 修复：
  - 后端 `UiElementMapViewSet` 新增 `manual-capture-start` 和 `manual-capture-result`，启动时创建草稿地图并下发执行器，回填成功后保存 `capture_mode=manual_capture` 的地图并标记为 `confirmed/current`。
  - `UiAiGenerationTaskViewSet._build_ai_generation_args()` 增加 `element_map_id`、`element_map_source`、`execution_mode`，人工采集地图统一下发为 `execution_mode=manual_map`。
  - 执行器新增 `run_manual_element_map_capture()`：有界面打开浏览器，用户手动操作，Playwright 按页面事实周期采集快照、状态迁移、用户事件轨迹、定位基线、storage state 和 trace。
  - `run_ai_generation_task()` 识别 `manual_capture` 地图后直接返回给 LLM 规划链，跳过自动业务路径探索，避免人工地图被旧预探索覆盖。
  - consumer coverage repair 在人工 map 模式下返回 `manual_element_map_missing_action_evidence`，不再自动 re-explore。
  - 前端元素地图页新增“人工采集”入口，可选择环境、填写采集 URL、采集间隔和最长采集时间。
  - 修复不增加业务词、按钮词、认证词或指定页面词规则；只补齐元素地图来源、采集和消费合同。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_manual_capture_element_map_skips_auto_exploration`
  - `TaskConsumerFullReplanTests.test_manual_capture_map_does_not_trigger_coverage_reexploration`
  - 执行器目录执行：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_manual_capture_element_map_skips_auto_exploration test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_manual_capture_map_does_not_trigger_coverage_reexploration`
  - 结果：2 tests OK。
  - 后端目录执行：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 /home/zhangyuan/projects/WHartTest/WHartTest_Django/.venv/bin/python manage.py test --keepdb ui_automation.tests.UiEnvironmentAuthConfigTests.test_ai_dispatch_marks_manual_capture_element_map_mode`
  - 结果：1 test OK。
  - 语法/类型检查：
    - 执行器 `py_compile`：OK。
    - 后端 `py_compile`：OK。
    - Vue `./node_modules/.bin/vue-tsc --noEmit --skipLibCheck`：OK。

### 42. 人工采集弹窗把“开始采集”误表现为“地图已保存”

- 现象：元素地图页点击人工采集弹窗的确认按钮后，前端立即关闭弹窗并刷新列表；用户还没有在浏览器弹窗完成操作，页面上却已经出现占位元素地图，容易误判为地图已经保存完成。
- 根因：
  - 模式一为了给执行器回填结果创建了 `UiElementMap` 占位记录，但前端把“下发采集任务成功”当成“元素地图保存成功”处理。
  - 前端没有轮询 `map_json.manual_capture.status`，也没有隐藏 `capturing` 状态的占位记录。
- 修复：
  - 人工采集弹窗去掉默认 OK 自动关闭行为，改为显式“开始采集”按钮。
  - 下发成功后弹窗保持打开，显示采集中状态，表单禁用，并轮询元素地图详情。
  - 只有采集状态变为 `completed` 后才关闭弹窗、提示“人工采集完成，元素地图已保存”并刷新列表。
  - 采集中占位记录从元素地图列表中隐藏，避免被误当作可用地图。
- 回归：
  - Vue 目录执行：`./node_modules/.bin/vue-tsc --noEmit --skipLibCheck`
  - 结果：OK。

### 43. 人工采集元素地图把 DOM 抖动误判为新业务页面

- 现象：用户实际只手动经过登录页、首页、账号管理、新建账号页 4 个业务状态，但人工采集结果展示约 35 个页面快照。
- 根因：
  - `_page_state_signature()` 把 `bounding_box`、元素顺序、临时 `element_key` 相近字段和完整元素列表变化纳入页面签名。
  - 人工采集循环每个间隔都用物理 DOM 快照差异决定是否追加 `pages[]`，页面 loading、表格刷新、输入过程、动态 ID、元素轻微位移都会生成新页面。
  - 这不符合模式一目标；人工地图应保存“业务状态页”和真实操作轨迹，而不是保存每一次 DOM 抖动。
- 修复：
  - 页面签名改为业务语义签名：稳定 URL、title、frame 语义和可交互元素的 role/name/label/placeholder/text/actions/dialog/form 上下文。
  - 签名不再使用 `bounding_box`、元素顺序、临时 key、截图路径等易变信息。
  - URL 和文本中的长数字、长 hex、日期时间做归一化，避免动态 ID 或时间戳制造伪页面。
  - 人工采集循环新增 `page_index_by_signature`，同一业务状态重复快照合并到已有 page，不再追加新 page。
  - 合并时保留更丰富的元素快照，并在 `manual_capture.merged_snapshot_count`、`coverage_summary.raw_snapshot_count`、`coverage_summary.deduped_snapshot_count` 中记录压缩效果。
  - 连续输入/变更事件按同一目标压缩，只保留最后状态，避免 action trace 被每个字符输入撑大。
  - 修复不引入页面名、按钮文案、业务词或指定路径规则，只调整人工采集的状态去重合同。
- 回归：
  - `PlaywrightExecutorMcpCollectionTest.test_manual_capture_signature_ignores_dom_jitter`
  - `PlaywrightExecutorMcpCollectionTest.test_manual_capture_signature_separates_dialog_state`
  - `PlaywrightExecutorMcpCollectionTest.test_manual_capture_action_trace_compacts_continuous_input_events`
  - `PlaywrightExecutorMcpCollectionTest.test_manual_capture_element_map_skips_auto_exploration`
  - 执行器目录执行：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_manual_capture_signature_ignores_dom_jitter test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_manual_capture_signature_separates_dialog_state test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_manual_capture_action_trace_compacts_continuous_input_events test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_manual_capture_element_map_skips_auto_exploration`
  - 结果：4 tests OK。
  - 执行器 `py_compile`：OK。
  - 已重启执行器，新执行器 `actuator-35911` 在线。

### 44. UI 自动化用例点击执行后仍显示未执行

- 现象：UI 自动化测试用例 555 点击执行后，列表状态仍展示“未执行”；数据库中 `UiTestCase(id=23, name=555)` 状态为 0，且没有执行记录。
- 根因：
  - UI 自动化用例执行命令依赖前端 WebSocket fire-and-forget 发送；后端只在收到 WS 后更新 `UiTestCase.status`，执行记录要等执行器回传最终结果才创建。
  - 这个合同没有“后端已接受执行命令”的持久化确认点；一旦前端点击入口没有真正走到 WS 发送、WS 消息丢失、页面刷新或浏览器状态和服务端状态不一致，列表只能继续展示数据库里的旧状态 0。
  - 直接模拟同一 WebSocket 消息下发 `case_id=23` 后，后端能把状态置为 1 且执行器能收到任务，说明本次不是执行器链路、元素地图或业务步骤定位问题。
- 修复：
  - `UiTestCaseViewSet` 新增 `POST /ui-automation/testcases/{id}/execute/`，后端收到执行命令后立即创建 `UiExecutionRecord(status=1)`，同步 `UiTestCase.status=1`，再下发执行器。
  - WebSocket 老入口 `handle_execute_test_case()` 同样创建执行中记录，并把 `execution_record_id` 传给执行器，兼容旧调用链。
  - `save_execution_result()` 在收到 `execution_record_id` 时更新已创建的执行记录，不再新建重复记录；同时保留无 `execution_record_id` 的旧结果回填兼容。
  - 执行器 `execute_test_case()` 将启动参数中的 `execution_record_id` 透传到 `CASE_RESULT`，避免后端无法关联启动记录而新建重复完成记录。
  - 前端 UI 自动化用例列表点击执行改为调用后端 execute HTTP 命令入口；WebSocket 继续用于接收执行结果事件。
  - 执行结果保存时间改为 Django timezone，避免时区不一致。
  - 修复只调整执行命令/结果持久化合同，不增加业务词、按钮词、认证词或具体用例规则。
- 回归：
  - 后端：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 /home/zhangyuan/projects/WHartTest/WHartTest_Django/.venv/bin/python manage.py test --keepdb ui_automation.tests.UiEnvironmentAuthConfigTests.test_ai_dispatch_marks_manual_capture_element_map_mode ui_automation.tests.UiEnvironmentAuthConfigTests.test_testcase_execute_creates_running_record_before_dispatch ui_automation.tests.UiEnvironmentAuthConfigTests.test_save_execution_result_updates_started_record`
  - 结果：3 tests OK。
  - 后端 `py_compile`：OK。
  - 执行器：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_server_trace_path_is_preserved_when_sending_case_result`
  - 结果：1 test OK。
  - 执行器 `py_compile`：OK。
  - Vue `./node_modules/.bin/vue-tsc --noEmit --skipLibCheck`：OK。
  - 真实回归：
    - 重启后端、前端、执行器，最新执行器为 `actuator-43812`。
    - 调用 `POST /api/ui-automation/testcases/23/execute/` 回归 555，接口返回 `record_id=63,status=1`。
    - 执行结束后 `UiTestCase(id=23).status=2,last_execution=63`，`UiExecutionRecord(id=63).status=2`，未再产生重复完成记录。
    - 中间未透传修复前产生的调试残留 `record_id=61` 已标记为取消，最终有效记录为 63。

### 45. 人工采集元素地图在 AI 生成结果保存后被自动过期并生成 draft 新版本

- 现象：用户只人工采集了一次元素地图，但执行新的 AI 生成任务后，原人工地图变为 `superseded`，系统自动创建同名 `draft` 新版本。现场数据库中 `账号管理` 地图 73、74 都是 `manual_capture`，但 stale_reason 为 `AI 重新采集发现元素地图内容变化，已生成新的 draft 版本`，最新 75 是自动创建的 draft。
- 根因：
  - 模式一人工采集地图已经在任务下发阶段标记为 `execution_mode=manual_map`，执行器 coverage repair 也不会退回自动探索。
  - 但后端 `save_ai_generation_result_sync()` 保存 AI 生成结果时仍沿用旧版本治理合同：只要执行器回传 `element_map.pages`，且和已确认地图 hash 不同，就把原 confirmed 地图置为 `superseded` 并创建新的 draft。
  - 这把“运行证据快照”和“一等人工地图事实源”混在了一起，破坏了模式一稳定性目标。
- 修复：
  - 新增人工地图结果识别：只根据 `execution_mode=manual_map`、`element_map_source=manual_capture`、结果地图或已绑定地图的 `capture_mode/source=manual_capture` 判断，不引入业务词、按钮词、页面词。
  - AI 生成结果属于人工地图模式时，结果中的 `element_map` 只保存到 `task.element_map_snapshot` 作为运行证据，不反写 `UiElementMap`，不置过期，不自动创建 draft 新版本。
  - 在 `verification_result.element_map_governance` 记录 `status=skipped` 和 `reason=manual_capture_map_is_immutable_for_ai_result`，方便后续排查。
- 回归：
  - 后端新增 `UiEnvironmentAuthConfigTests.test_manual_capture_map_is_not_superseded_by_ai_generation_result`，覆盖 confirmed 人工地图收到不同 AI 结果地图后仍保持 current 且不创建新版本。
  - 后端执行：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 .venv/bin/python manage.py test --keepdb ui_automation.tests.UiEnvironmentAuthConfigTests.test_ai_dispatch_marks_manual_capture_element_map_mode ui_automation.tests.UiEnvironmentAuthConfigTests.test_manual_capture_map_is_not_superseded_by_ai_generation_result ui_automation.tests.UiEnvironmentAuthConfigTests.test_testcase_execute_creates_running_record_before_dispatch ui_automation.tests.UiEnvironmentAuthConfigTests.test_save_execution_result_updates_started_record`
  - 结果：4 tests OK。
  - 后端 `py_compile ui_automation/consumers.py ui_automation/tests.py`：OK。
- 运行态处理：
  - 已重启 Django 后端，PID `48390`。
  - 已重启 UI 执行器，最新执行器 `actuator-48475`。
  - 修正旧逻辑造成的遗留数据：将最新已确认人工地图 `UiElementMap(id=74, name=账号管理, version=2)` 恢复为 `confirmed/current`；将旧逻辑误生成的 draft `UiElementMap(id=75)` 标记为 `superseded`，不删除记录。

### 46. 多个同名行内动作无法按 Gherkin 行上下文唯一绑定

- 现象：最新 AI 生成任务失败，错误为“存在多个同名按钮，但候选数据未提供按钮与目标行之间的关联，无法唯一确定目标按钮”。Gherkin 已表达“点击某字段值所在行的某按钮”，但候选元素仍是平铺列表。
- 根因：
  - LLM 能理解自然语言/Gherkin 的行内动作意图，但元素地图合同没有稳定产出 `row_text`、`row_cells`、`row_index`、`column_*`、`table_name` 等行级结构事实。
  - 后端规划和 TypeScript 编译已有部分字段消费能力，但采集端主页面/iframe 对表格上下文不完整，尤其没有从表格可访问名称中提取 `table_name`，`row_cells` 也缺少列名。
  - 这不是缺少具体业务词规则；继续补业务词会让合同膨胀，仍无法解决其他列表行内动作。
- 修复：
  - 执行器主页面采集和 iframe 采集统一补齐表格行上下文：`row_text`、`row_index`、`row_cells`、`column_index`、`column_name`、`column_text`、`table_name`。
  - `row_cells` 增加 `column_index/column_name`，让候选数据能表达“按钮与同一行实体字段之间的关系”。
  - `table_name` 增加 `caption`、表格标题、`aria-label`、`aria-labelledby`、`title` 的通用提取，不依赖业务文案。
  - 后端回归固定：绑定候选必须保留行结构，最终 TypeScript 计划必须先按 `row_scope.row_text` 收窄到行，再在行内执行原动作 locator。
- 回归：
  - 语法编译覆盖 `WHartTest_Actuator/executor.py`、`WHartTest_Actuator/test_executor_mcp_collection.py`、`WHartTest_Django/ui_automation/ai_planning.py`、`WHartTest_Django/ui_automation/tests.py`，结果 OK。
  - 执行器新增回归：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_executor_mcp_collection.PlaywrightExecutorMcpCollectionTest.test_native_collection_attaches_table_row_context_to_row_actions`
    结果：1 test OK。
  - 后端新增回归：
    `PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiPlanningIframeCandidateTests.test_element_binding_candidates_preserve_table_row_context ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_generated_click_uses_row_scope_when_action_name_is_duplicated --keepdb`
    结果：2 tests OK。
  - 既有回归：
    `test_native_collection_includes_priority_table_text_candidate`、`test_text_locator_resolves_first_visible_match_before_clicking`、`test_text_assertion_falls_back_to_expected_text_when_configured_locators_are_stale` 实际执行通过；按旧日志名称调用的 `test_preferred_action_scores_above_row_context_label` 在当前文件不存在。
    `test_short_observed_action_label_matches_compound_requirement_phrase`、`test_exact_route_leaf_scores_above_aggregate_menu_label`、`test_generic_route_parent_does_not_match_brand_link` 结果：3 tests OK。

### 47. 原始元素地图已有行上下文，但 action-scoped 候选压缩后仍丢失 row_scope

- 现象：任务 75 仍失败在同一业务点，错误为 `manual_element_map_missing_action_evidence`，提示“候选元素未提供目标行的 row_scope”。用户怀疑没有走最新执行器。
- 事实核查：
  - 任务 75 的 `actuator_id=actuator-59301`，执行器日志显示由最新执行器拉取并完成上报，不是旧执行器。
  - 使用的人工地图为 `UiElementMap(id=77)`。
  - 地图 77 原始 `map_json` 中目标行按钮存在：`element_key=button_5`，`name=权限分配`，`row_text=测试权限 权限分配`。
- 根因：
  - 第 46 条修复让采集器产出了 `row_*` 字段，但 `_compact_element_map()` 主元素压缩没有保留这些字段。
  - `_planning_action_binding_candidates()` 消费的是 compact map，不是原始 map；因此 action-scoped candidates 里多个同名按钮仍然没有 `row_text/row_scope`。
  - `_element_planning_excerpt()` 原本只把部分行字段放进 `structural_context`，没有直接暴露为绑定阶段可消费的候选合同字段。
- 修复：
  - `_compact_element_map()` 对主元素保留 `row_text`、`row_index`、`row_cells`、`column_index`、`column_name`、`column_text`、`table_name`。
  - `_element_planning_excerpt()` 直接输出 `row_text/row_cells/column_* /table_name`，并派生 `row_scope`，同时保留 `structural_context` 兼容旧调试视图。
  - 元素绑定阶段 `output_schema.bindings` 允许输出 `row_scope`。
  - `_action_candidate_score()` 对 click 动作增加结构性控件优先：真实 `button/link/menuitem/a` 高于同一行的普通 `td/div` 文本节点；不依赖任何业务词。
- 回归：
  - 新增 `UiPlanningIframeCandidateTests.test_action_scoped_candidates_preserve_row_scope_after_compaction`，覆盖同名行内动作经过 compact 和 action-scoped candidate 后仍保留 `row_scope`，且目标行真实按钮排第一。
  - 执行：
    `PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/uv-cache uv run python manage.py test ui_automation.tests.UiPlanningIframeCandidateTests.test_action_scoped_candidates_preserve_row_scope_after_compaction ui_automation.tests.UiPlanningIframeCandidateTests.test_element_binding_candidates_preserve_table_row_context ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_generated_click_uses_row_scope_when_action_name_is_duplicated --keepdb`
  - 结果：3 tests OK。
  - 真实任务 75 数据复现：
    - 修复后 `action_5` 第一候选为 `button_5`。
    - `score=243`，`row_text=测试权限 权限分配`，`has_row_scope=true`。
    - 同一行普通 `td/div` 文本候选分数为 `198`，不会压过真实按钮。

### 48. 静态元素地图没有状态控件时，集合状态断言被错误阻断在规划校验

- 现象：
  - 最新任务失败在 `llm_plan_validation`，`assert_value` 断言被判定为未绑定。
  - 人工地图已经采到目标业务页面和大量可见集合文本，但没有采到 `input/checkbox/radio` 等可直接读取状态的控件。
- 根因：
  - 执行链仍把断言合同理解成“生成阶段必须绑定到一个具体控件”。
  - 对复杂自定义树、表格、权限类集合，状态可能只体现在运行时 DOM、ARIA、class、data-state 或页面结构变化中，预采集静态地图不一定存在可绑定的状态控件。
  - 继续枚举控件类型会让合同膨胀，并且仍无法覆盖新的自定义组件。
- 修复：
  - 合同层新增 `binding_mode=runtime_state`、`runtime_resolver=state_evidence`，只允许用于断言动作，要求存在已观察页面作用域和 `state_assertion`。
  - 后端绑定阶段新增确定性兜底：当必需断言无法静态绑定、前序已有真实绑定动作、且页面作用域已观察到时，将 unresolved 转成 runtime state evidence binding，不再进入 coverage gate 阻断。
  - `_validate_generated_steps()` 对 `runtime_state` 不再要求 `element_key`，只校验页面证据作用域是否存在。
  - TypeScript runtime helper 在 `click` 前后 best-effort 采样 DOM/ARIA/class/data-state 状态证据；普通点击不会因采样失败而失败，只有后续 `assertRuntimeStateChange()` 需要证据时才严格校验。
  - LLM 阶段 schema 同步新增 `runtime_state`，但修复不依赖 LLM 主动输出该模式；确定性 compiler 会兜底转换。
  - 未引入业务词、页面词或指定控件枚举规则。
- 回归：
  - 新增：
    `UiAiPlanningElementMapCompactionTests.test_unbound_state_assertions_compile_to_runtime_state_evidence_contract`
    `UiAiPlanningElementMapCompactionTests.test_runtime_state_assertion_does_not_require_static_element_binding_and_generates_ts_evidence_call`
  - 实际 Django 工程执行：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 /home/zhangyuan/projects/WHartTest/WHartTest_Django/.venv/bin/python manage.py test ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_unbound_state_assertions_compile_to_runtime_state_evidence_contract ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_runtime_state_assertion_does_not_require_static_element_binding_and_generates_ts_evidence_call --keepdb`
  - 结果：2 tests OK。
  - 相邻合同回归：
    `test_runtime_text_assertion_is_readonly_and_does_not_require_element_map_binding`
    `test_runtime_text_assertion_allows_structural_reveal_candidate`
    `test_runtime_image_ocr_input_is_preserved_without_fake_static_value`
    `test_planning_contract_coverage_gate_blocks_required_unresolved_bindings`
    `test_staged_contract_accepts_declared_unresolved_gap_step`
    以及上面 2 个新增测试。
  - 结果：7 tests OK。
  - 最终目标回归：
    `test_unbound_state_assertions_compile_to_runtime_state_evidence_contract`
    `test_runtime_state_assertion_does_not_require_static_element_binding_and_generates_ts_evidence_call`
    `test_typescript_runtime_waits_for_async_rendered_locator`
  - 结果：3 tests OK。
  - 实际 Django 工程 `py_compile ui_automation/ai_planning.py ui_automation/planning_contract.py ui_automation/tests.py`：OK。

### 49. 应用为 UI 用例阶段未同步 runtime_state 合同，导致成功生成任务应用失败

- 现象：
  - 任务 111 点击详情后执行“应用为 UI 用例”失败。
  - 后端接口 `POST /api/ui-automation/ai-generation-tasks/77/apply-to-ui-case/` 返回 400。
  - 复现响应为：`生成步骤存在无法精确绑定的页面元素，已拒绝应用`，未解析步骤为两个 `binding_mode=runtime_state`、`runtime_resolver=state_evidence` 的 `assert_value` 断言。
- 根因：
  - 第 48 条已经把复杂集合状态断言合同化为运行时状态证据，允许断言步骤没有静态 `element_key`。
  - 但 `apply_to_ui_case` 仍沿用旧合同：所有非 `goto/wait` 步骤都必须解析到 `UiElement`。
  - 因此 AI 生成和执行链已经接受的合法 `runtime_state` 断言，在“应用为 UI 用例”这个后置编译阶段被错误拒绝。
- 修复：
  - 新增通用 `_is_runtime_state_step()` 判定，只基于 `binding_mode/runtime_resolver/operation` 合同，不依赖业务文案或控件枚举。
  - `apply_to_ui_case` 的预解析阶段允许 `runtime_state` 断言跳过静态元素绑定。
  - `_create_details_from_generated_steps()` 允许 `runtime_state` 断言保存为无元素断言步骤，并在 `ope_value` 中保留 `binding_mode`、`runtime_resolver`、`page_key`、`target_name`、`state_assertion`。
- 回归：
  - 新增 `UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_state_assertion_without_static_element`。
  - 定向执行：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 /home/zhangyuan/projects/WHartTest/WHartTest_Django/.venv/bin/python manage.py test ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_state_assertion_without_static_element ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_input_binding_metadata ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_compiles_ai_select_option_to_legacy_select_operation ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_preserves_page_scoped_identity_and_semantic_fallback --keepdb`
  - 结果：4 tests OK。
  - 真实任务 111 复现接口已从 400 变为 200，创建 UI 用例 `test_case_id=25`，`applied_step_count=9`，`applied_execution_mode=typescript_spec`。

### 50. AI 生成任务详情页加载缓慢

- 现象：
  - 任务 111 打开详情页加载缓慢。
  - 真实字段大小统计显示大字段合计约 `7.06 MB`，其中 `element_map_snapshot` 约 `6.08 MB`，`verification_result` 约 `0.91 MB`。
- 根因：
  - 详情接口使用 `fields='__all__'`，每次打开详情都返回完整元素地图、完整验证结果、生成用例、脚本和修复历史。
  - 前端详情抽屉打开后立即对这些大 JSON 执行 `JSON.stringify(..., null, 2)` 并渲染到 `<pre>`，造成网络传输和浏览器主线程渲染双重变慢。
- 修复：
  - 新增 `UiAiGenerationTaskDetailSerializer`，详情默认返回轻量摘要，不返回完整 `element_map_snapshot` 和 `verification_result`。
  - `retrieve` 查询对最大字段使用 `defer`，避免默认详情读取大 JSON。
  - 新增 `detail-payload/?field=...` 按需加载接口，允许前端在用户切换到对应 Tab 时再读取原始大字段。
  - `apply-to-ui-case` 和 `start` 返回的 `task` 也切到轻量 serializer，避免操作成功后再次回传大对象。
  - 前端详情页改为 Tab 级懒加载：计划、生成用例、观察记录、元素地图、脚本、验证结果、修复历史分别按需请求；默认打开详情不下载元素地图。
- 回归：
  - 新增 `UiAiGenerationApplyCrossPageTests.test_retrieve_uses_lightweight_payload_and_lazy_detail_field`。
  - 定向执行：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 /home/zhangyuan/projects/WHartTest/WHartTest_Django/.venv/bin/python manage.py test ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_retrieve_uses_lightweight_payload_and_lazy_detail_field ui_automation.tests.UiAiGenerationApplyCrossPageTests.test_apply_preserves_runtime_state_assertion_without_static_element --keepdb`
  - 结果：2 tests OK。
  - 前端类型检查：
    `./node_modules/.bin/vue-tsc -b`
  - 结果：OK。
  - 真实任务 111 验证：
    - 默认详情接口响应约 `12.66 KB`。
    - 默认详情 `element_map_snapshot` 不包含完整 `pages`。
    - `detail-payload/?field=element_map_snapshot` 可按需返回完整 11 个页面。

### 51. 智能测试模式未真正进入 runtime_agent，仍走旧静态规划链

- 现象：
  - 任务 78 选择智能测试模式后失败，错误为 `LLM 规划未覆盖用户需求或存在未绑定元素，已停止执行: 最终计划缺少必需动作 assert_3`。
  - 数据库中 `safety_policy.execution_mode=runtime_agent`，但执行器日志显示入口仍是 `开始执行 AI UI 生成任务`，没有 `开始执行 runtime_agent AI UI 生成任务`。
- 根因：
  - 后端 `_build_ai_generation_args()` 先从 safety_policy 读取了 `runtime_agent`，但随后又按元素地图来源把顶层 `execution_mode` 覆盖为 `auto_exploration/manual_map`。
  - 真实运行目录的执行器 `consumer.py` 也没有 runtime_agent 顶层分流，导致即使任务选择了智能模式，也继续走“预探索 + LLM 一次性最终计划 + plan_validation”的旧链路。
  - 后端 `build_llm_ui_generation_plan()` 没有 runtime_agent 单步规划入口，运行时模式接口仍会落入静态规划器。
- 修复：
  - 后端 `_build_ai_generation_args()` 保留 `runtime_agent` 顶层执行模式，只有非 runtime_agent 时才根据人工地图切换为 `manual_map/auto_exploration`。
  - 后端 `ai_planning.py` 新增 `_normalize_execution_mode()`、`_task_execution_mode()`、`_build_runtime_agent_plan()`，当 `execution_mode=runtime_agent` 时返回单步 runtime plan。
  - runtime planner 收紧合同：仍有 required pending actions 时，LLM 不能直接返回 completed；必须继续给出下一步或 blocked。
  - 执行器 `execute_ai_generation()` 新增 runtime_agent 分流，直接进入“当前页观察 -> LLM 下一步 -> Playwright 执行 -> 再观察/校验”的循环，不再先跑旧静态规划链。
  - 未引入业务词、按钮词或任务专用规则。
- 回归：
  - 后端：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 .venv/bin/python manage.py test ui_automation.tests.UiEnvironmentAuthConfigTests.test_ai_dispatch_preserves_runtime_agent_mode ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_runtime_agent_rejects_completed_when_required_actions_are_pending --keepdb`
    结果：2 tests OK。
  - 执行器：
    `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_ai_generation_uses_runtime_branch`
    结果：1 test OK。
  - `py_compile` 覆盖后端和执行器修改文件：OK。

### 52. runtime_agent 的需求合同为空，首轮“等待渲染”被当成任务完成

- 现象：
  - 任务 78 已确认进入最新执行器 `actuator-40392` 和 runtime_agent 分支。
  - 执行器日志出现 `开始执行 runtime_agent AI UI 生成任务: task_id=78`。
  - 任务仍失败，`failure_category=runtime_agent`，`verification_result.runtime_agent_execution` 中 `generated_steps=[]`，reason 为页面已加载但暂未观察到可交互元素，需要等待渲染。
- 根因：
  - `build_requirement_document()` 只返回自然语言/Gherkin 原文和 sources，没有把 Gherkin 的 `Given/When/Then/And` 步骤转换成 runtime 可消费的 `flow`。
  - `_runtime_flow_pending_actions()` 只看 `requirement_document.flow`，flow 为空时 pending_actions 为空，runtime planner 误以为没有剩余动作。
  - LLM 对“页面尚未渲染出可交互元素”的合理判断没有对应执行链原语，只能返回空 step 或 completed，执行器随后把空步骤当成失败/完成处理。
- 修复：
  - `planning_contract.build_requirement_document()` 增加通用 flow 构建：
    - Gherkin 模式下只按 `Given/When/Then/And/But` 行生成顺序化原子需求，不解析业务词。
    - 自然语言模式下按非空行生成顺序化原子需求。
  - runtime planner 的 prompt/schema 增加 `wait_and_reobserve` 原语：当前页面还没有可交互候选时，必须返回继续等待并重新观察，不能返回 completed。
  - `_normalize_runtime_step()` 修复空 dict 被补成“非空步骤”的问题。
  - 执行器支持 `wait_and_reobserve`：等待页面加载/网络空闲/短暂 timeout 后重新采集页面；该原语只进入 runtime_history，不写入最终生成用例 steps。
  - 未添加 `登录/SSO/点击/查看/新增` 等业务词或指定动作规则。
- 回归：
  - 后端：
    `UV_CACHE_DIR=/tmp/uv-cache PYTHONDONTWRITEBYTECODE=1 .venv/bin/python manage.py test ui_automation.tests.UiRequirementDocumentTests.test_requirement_document_merges_natural_language_and_gherkin_sources ui_automation.tests.UiAiPlanningElementMapCompactionTests.test_runtime_agent_converts_empty_completed_with_pending_actions_to_reobserve --keepdb`
    结果：2 tests OK。
  - 执行器：
    `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_ai_generation_uses_runtime_branch test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_reobserve_primitive_is_not_case_step`
    结果：2 tests OK。
  - `py_compile` 覆盖后端和执行器修改文件：OK。

### 53. runtime_agent completed 空步骤被执行器结果裁决误写为成功现场

- 现象：
  - 任务 78 当前数据库状态仍为 `failed/runtime_agent`。
  - `verification_result.runtime_agent_execution` 内部却为 `status=success`、`steps=[]`、`generated_steps=[]`。
  - reason 为 `当前为登录页，需求首个待执行动作是点击“SSO登录”按钮。`，说明目标尚未执行，不能被视为完成。
- 根因：
  - 后端 runtime planner 已增加“无真实业务动作历史时不能接受 completed”的保护，但执行器结果裁决层仍有缺口。
  - `_run_runtime_agent_ai_generation()` 在 runtime planner 返回 `completed` 且重观察达到上限后，只要进入 completed 分支就会写 `runtime_agent_execution.status=success`。
  - 该分支没有再次校验 `generated_steps` 是否非空，导致“LLM 标记完成但没有任何可执行步骤”的响应被保存成成功现场，最终外层又因空步骤把任务标为失败。
- 修复：
  - 执行器 completed 分支增加最终裁决保护：当 `generated_steps` 为空且重观察已达上限时，写入 `runtime_agent_plan_invalid`，并把 `runtime_agent_execution.status` 标为 `failed`。
  - 保留等待重观察原语逻辑；只有已经产生真实非内部运行时步骤时，completed 才能转成 success。
  - 后端 planner 增加候选非空场景回归：登录页已有 SSO 候选、历史只有 wait/reobserve 时，LLM 空 completed 仍必须降级为 `wait_and_reobserve`。
- 回归：
  - 执行器新增：
    `TaskConsumerFullReplanTests.test_runtime_agent_completed_without_generated_steps_fails_after_reobserve_limit`
  - 执行器定向执行：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_ai_generation_uses_runtime_branch test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_planner_drops_contract_artifacts test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_reobserve_primitive_is_not_case_step test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_completed_without_generated_steps_fails_after_reobserve_limit`
    结果：4 tests OK。
  - 执行器 `py_compile consumer.py test_consumer_ai_reobserve.py`：OK。
  - 后端新增：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_rejects_completed_without_business_history_even_with_candidates`
  - 后端 `py_compile ui_automation/ai_planning.py ui_automation/tests.py`：OK。
  - 当前环境 PostgreSQL 测试库连接报 `psycopg.OperationalError: connection is bad`；切 SQLite 又受已有 PostgreSQL 专用迁移 SQL 阻断，因此后端 Django runner 未完成，本次以后端语法检查和执行器可运行反馈环为准。

### 54. runtime_agent 停在登录页时缺少合同驱动下一步，反复 reobserve 后失败

- 现象：
  - 任务 78 已进入 `runtime_agent` 分支，截图 `ai_generation_78_runtime_page_1.png` 和 `ai_generation_78_runtime_page_2.png` 均停留在登录页。
  - 当前页面已渲染出 `用户名`、`密码`、`登录`、`SSO登录` 等可交互控件，但最终仍以 `generated_steps=[]` 失败。
  - LLM 曾返回 `completed`，reason 却说明首个待执行动作是点击 `SSO登录`，属于 completed 与执行证据矛盾。
- 根因：
  - runtime planner 的上下文没有把 `requirement_document.flow`、认证合同和待执行动作作为强约束传给模型。
  - `_build_runtime_agent_plan()` 对 LLM 的 completed/blocked/空 step 只做等待降级，没有在 pending action 与当前候选元素可匹配时生成确定性下一步。
  - `_ensure_authenticated_before_ai_flow()` 只返回 observation，runtime 分支没有专属认证门禁，前置认证失败时还会继续进入 LLM 规划循环。
- 修复：
  - 仅修改 `runtime_agent` 链路，不改合同规划模式。
  - 后端 `_runtime_step_prompt_context()` 增加 `requirement_document`、`authentication_contract` 和 `runtime_goal.flow`。
  - 后端 `_build_runtime_agent_plan()` 增加 runtime 专用 pending action 计算和候选匹配纠偏：当 LLM 返回 completed/blocked/空 step/等待 step，但仍有待执行动作且当前候选能匹配时，返回确定性可执行 step。
  - 候选匹配必须命中目标文本或引号目标，并且 locator 必须来自当前候选元素；目标不匹配时仍保持 `wait_and_reobserve`，避免误点第一个候选。
  - 执行器 `_run_runtime_agent_ai_generation()` 增加 runtime 专用认证阻断判断：前置认证仍停留登录页时直接失败为 `permission`，不再继续调用 runtime LLM。
- 回归：
  - 执行器：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_ai_generation_uses_runtime_branch test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_completed_without_generated_steps_fails_after_reobserve_limit test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_blocks_when_prerequisite_authentication_still_on_login_page test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_reobserve_primitive_is_not_case_step`
    结果：4 tests OK。
  - 后端直接 harness：completed 空步骤 + `SSO登录` pending/candidate 被纠偏为 `click sso_button`。
  - `py_compile` 覆盖后端和执行器修改文件：OK。
  - Django test runner 仍受本地 PostgreSQL 测试库连接问题阻断，报 `psycopg.OperationalError: connection is bad`。

### 55. runtime_agent 点击触发导航后立即重观察，页面上下文销毁导致任务失败

- 现象：
  - 任务 78 已进入当前项目执行器和 `runtime_agent` 分支，日志包含 `开始执行 runtime_agent AI UI 生成任务: task_id=78`。
  - 失败点不是旧合同规划执行器，而是 runtime_agent 执行动作后立即采集页面状态。
  - 日志报错：`Page.evaluate: Execution context was destroyed, most likely because of a navigation`。
- 根因：
  - 点击类动作触发页面跳转或重载后，执行器立即调用 `_mcp_collect_page_state()`。
  - Playwright 当前 frame 的 JS execution context 正在被导航销毁，`page.evaluate()` 偶发失败。
  - 同时运行中的执行器进程启动时间早于本轮 `consumer.py` 修复时间，需要重启后才能加载最新代码。
- 修复：
  - 在 `TaskConsumer` 增加 runtime_agent 专用 `_collect_runtime_page_state()` 包装。
  - 仅对导航上下文销毁类错误执行重试：等待 `domcontentloaded`、短暂 `networkidle`、再延迟 300ms，最多采集 3 次。
  - runtime_agent 首次页面采集和每步执行后的重观察统一改走该包装，避免正常异常被吞掉。
- 回归：
  - 新增执行器用例：
    `TaskConsumerFullReplanTests.test_runtime_agent_retries_reobserve_when_navigation_destroys_context`
  - 定向执行：
    `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/zhangyuan/projects/WHartTest/WHartTest_Actuator /home/zhangyuan/projects/WHartTest/WHartTest_Actuator/.venv/bin/python -m unittest test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_retries_reobserve_when_navigation_destroys_context test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_ai_generation_uses_runtime_branch test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_completed_without_generated_steps_fails_after_reobserve_limit test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_agent_blocks_when_prerequisite_authentication_still_on_login_page test_consumer_ai_reobserve.TaskConsumerFullReplanTests.test_runtime_reobserve_primitive_is_not_case_step`
    结果：5 tests OK。
  - 执行器 `py_compile consumer.py test_consumer_ai_reobserve.py`：OK。
- 重启：
  - 2026-08-27 17:32 重启 Django 后端、Celery Worker/Beat、UI 执行器。
  - 当前在线执行器：`actuator-62170`，后端执行器列表接口返回 `count=1`。

### 56. runtime_agent 点击类动作首选 locator 失败后未消费当前观测备选候选

- 现象：
  - 任务 80（项目“厦门企管”，名称 `111`）修复后业务回归已走最新执行器 `actuator-22437`，执行链为 `runtime_agent`。
  - 流程已越过登录和子企业搜索，截图显示表格操作列存在 `名片彩印` 文本链接，但运行日志出现 `locator={'type': 'role', 'value': 'button', 'name': '名片彩印'}` 定位失败后回到重观察。
  - 随后又在 `彩印内容` 标签页出现类似重复重观察，说明问题不是单个业务词，而是点击类动作只执行首选 locator。
- 根因：
  - `runtime_agent` 规划 step 已携带当前观测候选，但执行器 `_execute_ai_plan_step()` 的非 select 路径只用 `locator_hint` 解析一次。
  - `locator_candidates` 主要被旧元素地图自愈路径消费；runtime_agent 快速执行路径首选 locator 失败时没有就地尝试同一观测里的 `text/link` 备选，导致可恢复问题被放大成重规划循环。
- 修复：
  - 后端 `_runtime_step_from_pending_action()` 保证 `locator_hint` 也进入 `locator_candidates`，完整保留当前观测的主备 locator 合同。
  - 执行器新增 `_step_locator_candidates()`，按 `locator_hint`、step 自带候选、mapped element 候选去重执行。
  - 点击、勾选、可见断言类动作根据当前 step 的 `target_name/description` 追加通用 `role=link`、`role=button`、`text` 回退候选；不引入 `名片彩印`、`彩印内容` 等业务硬编码。
  - `_execute_ai_plan_step()` 非 select 路径改为首选失败后就地遍历候选，成功时返回实际使用的 locator。
- 回归：
  - 新增执行器用例：
    `PlaywrightExecutorMcpCollectionTest.test_click_uses_runtime_locator_candidates_when_primary_role_misses`
  - 新增后端用例：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_preserves_primary_locator_in_locator_candidates`
  - `py_compile` 覆盖：
    `WHartTest_Actuator/consumer.py`、`WHartTest_Actuator/executor.py`、`WHartTest_Actuator/test_consumer_ai_reobserve.py`、`WHartTest_Actuator/test_executor_mcp_collection.py`、`WHartTest_Django/ui_automation/ai_planning.py`、`WHartTest_Django/ui_automation/tests.py`
    结果：OK。
  - 后端 runtime planner 定向测试 10 项：
    结果：10 tests OK。
  - 执行器 runtime/executor 定向测试 7 项：
    结果：7 tests OK。
  - 任务 80 业务级重跑：
    2026-08-31 16:45 使用最新执行器 `actuator-22437` 重跑，确认已进入 `runtime_agent` 并越过登录、子企业搜索阶段；回归过程中又暴露点击类 locator 备选未就地消费的问题，本节已修复，需重启执行器后再次业务级重跑。

### 57. runtime_agent 从 Gherkin 文本推断 fill/select 动作时未提取引号值

- 现象：
  - 任务 80 使用补丁执行器重跑后，页面到达“子企业管理”，但 `子企业编号` 输入框为空，列表仍为未过滤状态。
  - 随后运行时尝试验证 `搜索结果列表中应显示该子企业`，以 `text=30002620` 定位失败并持续重规划。
- 根因：
  - 任务 80 的 `requirement_document.flow` 中多数 Gherkin 步骤 `operation` 为空，例如 `我在搜索框中输入子企业编号 "30002620"`。
  - `_runtime_step_from_pending_action()` 能根据当前候选控件推断出 `fill`，但只读取结构化 `pending_action.value`。
  - Gherkin 原文中的 `"30002620"` 没有进入 step.value，导致执行器实际 fill 空字符串。
  - 表单候选匹配只使用单个展示 label，`请输入子企业编号` 与合同文本 `输入子企业编号` 因前缀差异无法稳定命中 deterministic fallback。
- 修复：
  - deterministic fallback 对 `fill/select_option/choose/check/uncheck` 在缺少结构化 value 时调用 `_runtime_action_desired_value()`，从引号文本中提取实际输入/选择值。
  - 增加 `_runtime_candidate_label_variants()`，匹配时同时使用 `label/form_label/name/accessible_name/placeholder/text/test_id`，并加入去掉 `请输入/请选择/请填写/输入/选择` 前缀后的语义名。
  - 该修复仍只依赖当前观测候选和需求合同，不引入项目业务硬编码。
- 回归：
  - 新增后端用例：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_extracts_quoted_value_for_deterministic_fill_step`
  - 后端 runtime planner 定向测试 11 项：
    结果：11 tests OK。
  - 执行器 runtime/executor 定向测试 7 项：
    结果：7 tests OK。
  - `py_compile` 覆盖后端和执行器修改文件：
    结果：OK。

### 58. runtime_agent 表单页状态断言误绑定到表单控件后重复定位失败

- 现象：
  - 任务 80 已进入 `新增内容` 表单，截图显示 `投递方式=主叫彩印`、`内容类型=自定义内容`、`黑白名单=不使用`、运营商多选均已满足。
  - 运行时仍在 `系统应弹出/跳转至彩印内容表单页面` 断言上循环，并把断言误绑定到投递方式 combobox：`role=combobox name=主叫彩印 被叫彩印...`。
- 根因：
  - `_runtime_semantic_core_text()` 对状态断言壳词剥离不足。
  - `系统应弹出/跳转至彩印内容表单页面` 的语义核心仍包含 `系统应/弹出/跳转至/表单` 等非业务词，无法用当前页面观测到的标题、面包屑、字段标签、控件文本等可见证据判定已完成。
  - pending action 未被过滤后，LLM/确定性匹配会把可见表单控件当成断言目标，导致重复定位失败。
- 修复：
  - 状态断言语义归一化新增 `系统应`、`系统应弹出/跳转至`、`弹出/跳转至`、`跳转至`、`表单` 等壳词剥离。
  - 当前页面观测证据已覆盖状态断言的业务核心语义时，页面状态断言直接从 pending action 中移除，继续执行后续真实业务动作。
- 回归：
  - 新增后端用例：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_treats_form_page_state_assertion_as_completed`
  - 后端 runtime planner 定向测试 12 项：
    结果：12 tests OK。
  - 执行器 runtime/executor 定向测试 7 项：
    结果：7 tests OK。
  - `py_compile` 覆盖后端和执行器修改文件：
    结果：OK。

### 59. runtime_agent 后续动作已完成时早期页面状态断言重新阻塞

- 现象：
  - 修复第 58 条后重新回归任务 80，执行器正常上报失败，trace 保存为：
    `WHartTest_Actuator/data/traces/ai_generation_runtime_agent_80_1788168950049.zip`。
  - 当前页面已推进到彩印内容表单页，但任务失败信息显示 pending action 首项回到早期合同动作 `gherkin_step_8`：`我应该进入分组管理页面`。
- 根因：
  - `_runtime_pending_actions()` 每轮从完整 Gherkin flow 重新扫描 pending action，只按已完成 id、当前页面满足、登录页跳过过滤。
  - 当后续合同动作已经成功执行，但更早的页面状态断言没有进入 `completed_action_ids` 或当前页面已无法再证明该中间态时，早期断言会重新成为首个 pending action，阻塞已经推进到后续页面的运行时链路。
- 修复：
  - 新增 `_runtime_latest_completed_flow_index()`，根据 `completed_action_ids`、成功 runtime history、`covered_action_ids` 计算合同进度水位。
  - 新增 `_runtime_action_looks_like_state_assertion()`，仅将位于更晚成功动作之前的 Given/Then/And 页面状态断言视为过期状态验证并跳过。
  - 不跳过位于进度水位之前的真实业务动作，避免吞掉未执行的点击、填写、选择等合同动作。
- 回归：
  - 新增后端用例：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_skips_obsolete_state_assertions_before_later_completed_action`
  - 后端 runtime planner 定向测试 13 项：
    结果：13 tests OK。
  - 后端和执行器 `py_compile`：
    结果：OK。

### 60. runtime_agent 只保存 select current_value 时无法判定目标选项已满足

- 现象：
  - 第 59 条修复后继续回归任务 80，页面已进入 `新增内容` 表单，截图显示 `投递方式=主叫彩印`。
  - 运行时仍多次尝试重新执行 `投递方式` select，locator 在 `name=deliveryWay` 或包含全部选项文本的 combobox role 上定位失败，随后进入重观察/重规划。
- 根因：
  - planner 和执行器的“当前选项已满足”判断只识别 `selected_label/selected_text/selected_value` 或 `option.selected=true`。
  - 部分 native select 观测只稳定提供 `current_value=number:1` 以及 options 列表，未显式提供 `selected_label` 或 `selected=true`，导致无法把 `number:1` 反解为 `主叫彩印`。
- 修复：
  - `_runtime_selected_texts()` 支持将 `current_value/value/selected_value` 与 options 的 `value/label/text/name` 匹配，并把命中 option 的 label/text/name/value 都纳入已选中语义。
  - 执行器 `_select_control_selected_texts()` 同步支持该映射，planner 未拦截时也能在执行前短路成功。
  - 该规则基于标准 select 当前值和 option 集合，不依赖项目字段或站点文案。
- 回归：
  - 新增后端用例：
    `UiAiPlanningElementMapCompactionTests.test_runtime_agent_maps_select_current_value_to_option_label`
  - 新增执行器用例：
    `PlaywrightExecutorMcpCollectionTest.test_select_option_succeeds_when_current_value_matches_option_value`
  - 后端 runtime planner 定向测试 14 项：
    结果：14 tests OK。
  - 执行器 runtime/executor 定向测试 8 项：
    结果：8 tests OK。
  - 后端和执行器 `py_compile`：
    结果：OK。
