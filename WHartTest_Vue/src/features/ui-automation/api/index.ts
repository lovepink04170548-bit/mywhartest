/**
 * UI 自动化 API 服务
 */

import request from '@/utils/request'
import type {
  UiModule,
  UiPage,
  UiPageDetail,
  UiElement,
  UiPageSteps,
  UiPageStepsDetail,
  UiPageStepsDetailed,
  UiTestCase,
  UiTestCaseDetail,
  UiCaseStepsDetailed,
  UiExecutionRecord,
  UiBatchExecutionRecord,
  UiElementMap,
  UiAiGenerationTask,
  UiAiGenerationTaskForm,
  UiAiReviewQueueItem,
  UiPublicData,
  UiEnvironmentConfig,
  UiModuleForm,
  UiPageForm,
  UiElementForm,
  UiPageStepsForm,
  UiTestCaseForm,
  UiPublicDataForm,
  UiEnvironmentConfigForm,
  PaginatedResponse,
  TraceData,
} from '../types'

const BASE_URL = '/ui-automation'

// ==================== 模块管理 ====================
export const moduleApi = {
  list: (params?: { project?: number; parent?: number }) =>
    request.get<PaginatedResponse<UiModule>>(`${BASE_URL}/modules/`, { params }),

  tree: (projectId: number) =>
    request.get<UiModule[]>(`${BASE_URL}/modules/tree/`, { params: { project: projectId } }),

  get: (id: number) => request.get<UiModule>(`${BASE_URL}/modules/${id}/`),

  create: (data: UiModuleForm) => request.post<UiModule>(`${BASE_URL}/modules/`, data),

  update: (id: number, data: Partial<UiModuleForm>) =>
    request.patch<UiModule>(`${BASE_URL}/modules/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/modules/${id}/`),

  move: (id: number, data: { target_id: number | null; drop_position: number }) =>
    request.post<UiModule>(`${BASE_URL}/modules/${id}/move/`, data),
}

// ==================== 页面管理 ====================
export const pageApi = {
  list: (params?: { project?: number; module?: number; search?: string }) =>
    request.get<PaginatedResponse<UiPage>>(`${BASE_URL}/pages/`, { params }),

  get: (id: number) => request.get<UiPageDetail>(`${BASE_URL}/pages/${id}/`),

  create: (data: UiPageForm) => request.post<UiPage>(`${BASE_URL}/pages/`, data),

  update: (id: number, data: Partial<UiPageForm>) =>
    request.patch<UiPage>(`${BASE_URL}/pages/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/pages/${id}/`),
}

// ==================== 元素管理 ====================
export const elementApi = {
  list: (params?: { page?: number; locator_type?: string; search?: string }) =>
    request.get<PaginatedResponse<UiElement>>(`${BASE_URL}/elements/`, { params }),

  get: (id: number) => request.get<UiElement>(`${BASE_URL}/elements/${id}/`),

  create: (data: UiElementForm) => request.post<UiElement>(`${BASE_URL}/elements/`, data),

  update: (id: number, data: Partial<UiElementForm>) =>
    request.patch<UiElement>(`${BASE_URL}/elements/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/elements/${id}/`),
}

// ==================== 页面步骤管理 ====================
export const pageStepsApi = {
  list: (params?: { project?: number; page?: number; module?: number; search?: string }) =>
    request.get<PaginatedResponse<UiPageSteps>>(`${BASE_URL}/page-steps/`, { params }),

  get: (id: number) => request.get<UiPageStepsDetail>(`${BASE_URL}/page-steps/${id}/`),

  create: (data: UiPageStepsForm) => request.post<UiPageSteps>(`${BASE_URL}/page-steps/`, data),

  update: (id: number, data: Partial<UiPageStepsForm>) =>
    request.patch<UiPageSteps>(`${BASE_URL}/page-steps/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/page-steps/${id}/`),
}

// ==================== 步骤详情管理 ====================
export const pageStepsDetailedApi = {
  list: (params?: { page_step?: number; step_type?: number }) =>
    request.get<PaginatedResponse<UiPageStepsDetailed>>(`${BASE_URL}/page-steps-detailed/`, { params }),

  get: (id: number) => request.get<UiPageStepsDetailed>(`${BASE_URL}/page-steps-detailed/${id}/`),

  create: (data: Omit<UiPageStepsDetailed, 'id' | 'created_at' | 'updated_at'>) =>
    request.post<UiPageStepsDetailed>(`${BASE_URL}/page-steps-detailed/`, data),

  update: (id: number, data: Partial<UiPageStepsDetailed>) =>
    request.patch<UiPageStepsDetailed>(`${BASE_URL}/page-steps-detailed/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/page-steps-detailed/${id}/`),

  batchUpdate: (pageStepId: number, steps: Omit<UiPageStepsDetailed, 'id' | 'page_step' | 'created_at' | 'updated_at'>[]) =>
    request.post(`${BASE_URL}/page-steps-detailed/batch_update/`, { page_step: pageStepId, steps }),
}

// ==================== 测试用例管理 ====================
export const testCaseApi = {
  list: (params?: { project?: number; module?: number; level?: string; status?: number; search?: string }) =>
    request.get<PaginatedResponse<UiTestCase>>(`${BASE_URL}/testcases/`, { params }),

  get: (id: number) => request.get<UiTestCaseDetail>(`${BASE_URL}/testcases/${id}/`),

  create: (data: UiTestCaseForm) => request.post<UiTestCase>(`${BASE_URL}/testcases/`, data),

  update: (id: number, data: Partial<UiTestCaseForm>) =>
    request.patch<UiTestCase>(`${BASE_URL}/testcases/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/testcases/${id}/`),

  execute: (id: number, data: { env_config_id?: number; actuator_id?: string }) =>
    request.post<{ message: string; record_id: number; test_case_id: number; status: number }>(
      `${BASE_URL}/testcases/${id}/execute/`,
      data,
    ),

  batchDelete: (ids: number[]) => request.post(`${BASE_URL}/testcases/batch-delete/`, { ids }),
}

// ==================== 用例步骤管理 ====================
export const caseStepsApi = {
  list: (params?: { test_case?: number; status?: number }) =>
    request.get<PaginatedResponse<UiCaseStepsDetailed>>(`${BASE_URL}/case-steps/`, { params }),

  get: (id: number) => request.get<UiCaseStepsDetailed>(`${BASE_URL}/case-steps/${id}/`),

  create: (data: Omit<UiCaseStepsDetailed, 'id' | 'status' | 'error_message' | 'result_data' | 'created_at' | 'updated_at'>) =>
    request.post<UiCaseStepsDetailed>(`${BASE_URL}/case-steps/`, data),

  update: (id: number, data: Partial<UiCaseStepsDetailed>) =>
    request.patch<UiCaseStepsDetailed>(`${BASE_URL}/case-steps/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/case-steps/${id}/`),

  batchUpdate: (testCaseId: number, steps: Omit<UiCaseStepsDetailed, 'id' | 'test_case' | 'status' | 'error_message' | 'result_data' | 'created_at' | 'updated_at'>[]) =>
    request.post(`${BASE_URL}/case-steps/batch_update/`, { test_case: testCaseId, steps }),
}

// ==================== 执行记录管理 ====================
export const executionRecordApi = {
  list: (params?: { project?: number; test_case?: number; status?: number; trigger_type?: string }) =>
    request.get<PaginatedResponse<UiExecutionRecord>>(`${BASE_URL}/execution-records/`, { params }),

  get: (id: number) => request.get<UiExecutionRecord>(`${BASE_URL}/execution-records/${id}/`),

  create: (data: Partial<UiExecutionRecord>) =>
    request.post<UiExecutionRecord>(`${BASE_URL}/execution-records/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/execution-records/${id}/`),

  /** 获取执行记录的 Trace 数据 */
  getTrace: (id: number, refresh?: boolean) =>
    request.get<TraceData>(`${BASE_URL}/execution-records/${id}/trace/`, { params: refresh ? { refresh: '1' } : {} }),
}

// ==================== 批量执行记录管理 ====================
export const batchRecordApi = {
  list: (params?: { project?: number; status?: number; trigger_type?: string }) =>
    request.get<PaginatedResponse<UiBatchExecutionRecord>>(`${BASE_URL}/batch-records/`, { params }),

  get: (id: number) => request.get<UiBatchExecutionRecord>(`${BASE_URL}/batch-records/${id}/`),

  delete: (id: number) => request.delete(`${BASE_URL}/batch-records/${id}/`),
}

// ==================== 元素地图 ====================
export const elementMapApi = {
  list: (params?: {
    project?: number
    environment_config?: number
    status?: string
    stale_status?: string
    role_key?: string
    permission_key?: string
    version_group?: string
    search?: string
    page?: number
    page_size?: number
  }) =>
    request.get<PaginatedResponse<UiElementMap>>(`${BASE_URL}/element-maps/`, { params }),

  get: (id: number) => request.get<UiElementMap>(`${BASE_URL}/element-maps/${id}/`),

  create: (data: Partial<UiElementMap>) =>
    request.post<UiElementMap>(`${BASE_URL}/element-maps/`, data),

  update: (id: number, data: Partial<UiElementMap>) =>
    request.patch<UiElementMap>(`${BASE_URL}/element-maps/${id}/`, data),

  confirm: (id: number) =>
    request.post<UiElementMap>(`${BASE_URL}/element-maps/${id}/confirm/`),

  startManualCapture: (data: {
    project: number
    environment_config?: number | null
    base_url: string
    name?: string
    interval_ms?: number
    max_duration_seconds?: number
    actuator_id?: string
  }) =>
    request.post<{ message: string; dispatch_id: string; element_map: UiElementMap }>(
      `${BASE_URL}/element-maps/manual-capture-start/`,
      data,
    ),

  delete: (id: number) => request.delete(`${BASE_URL}/element-maps/${id}/`),

  batchDelete: (ids: number[]) => request.post(`${BASE_URL}/element-maps/batch-delete/`, { ids }),
}

// ==================== AI 高成功率生成任务 ====================
export const aiGenerationTaskApi = {
  list: (params?: { project?: number; environment_config?: number; element_map?: number; status?: string; search?: string }) =>
    request.get<PaginatedResponse<UiAiGenerationTask>>(`${BASE_URL}/ai-generation-tasks/`, { params }),

  get: (id: number) => request.get<UiAiGenerationTask>(`${BASE_URL}/ai-generation-tasks/${id}/`),

  getDetailPayload: (
    id: number,
    field: 'test_plan' | 'mcp_observations' | 'element_map_snapshot' | 'generated_case' | 'generated_script' | 'verification_result' | 'repair_history',
  ) =>
    request.get<{
      field: string
      value: unknown
    }>(`${BASE_URL}/ai-generation-tasks/${id}/detail-payload/`, { params: { field } }),

  create: (data: UiAiGenerationTaskForm) =>
    request.post<UiAiGenerationTask>(`${BASE_URL}/ai-generation-tasks/`, data),

  update: (id: number, data: Partial<UiAiGenerationTaskForm>) =>
    request.patch<UiAiGenerationTask>(`${BASE_URL}/ai-generation-tasks/${id}/`, data),

  start: (id: number, payload?: { actuator_id?: string }) =>
    request.post<{ message: string; task: UiAiGenerationTask }>(`${BASE_URL}/ai-generation-tasks/${id}/start/`, payload || {}),

  applyToUiCase: (id: number) =>
    request.post<{
      message: string
      module_id: number
      page_id: number
      page_step_id: number
      test_case_id: number
      task: UiAiGenerationTask
    }>(`${BASE_URL}/ai-generation-tasks/${id}/apply-to-ui-case/`),

  getReviewQueue: (id: number) =>
    request.get<{
      task_id: number
      element_map_id: number | null
      pending_count: number
      items: UiAiReviewQueueItem[]
    }>(`${BASE_URL}/ai-generation-tasks/${id}/review-queue/`),

  resolveReviewQueue: (id: number, payload: { action: 'approve' | 'reject'; indexes?: number[]; comment?: string }) =>
    request.post<{
      message: string
      processed: number[]
      errors: Array<{ index: number; error: string }>
      pending_count: number
      task: UiAiGenerationTask
      element_map_id: number | null
    }>(`${BASE_URL}/ai-generation-tasks/${id}/review-queue/resolve/`, payload),

  downloadArtifact: (id: number, type: 'html' | 'junit' | 'json' | 'ts' | 'ts_bundle' | 'python') =>
    request.get<Blob>(`${BASE_URL}/ai-generation-tasks/${id}/download-artifact/`, {
      params: { type },
      responseType: 'blob',
    }),

  delete: (id: number) => request.delete(`${BASE_URL}/ai-generation-tasks/${id}/`),
}

// ==================== 公共数据管理 ====================
export const publicDataApi = {
  list: (params?: { project?: number; type?: number; is_enabled?: boolean; search?: string }) =>
    request.get<PaginatedResponse<UiPublicData>>(`${BASE_URL}/public-data/`, { params }),

  get: (id: number) => request.get<UiPublicData>(`${BASE_URL}/public-data/${id}/`),

  create: (data: UiPublicDataForm) => request.post<UiPublicData>(`${BASE_URL}/public-data/`, data),

  update: (id: number, data: Partial<UiPublicDataForm>) =>
    request.patch<UiPublicData>(`${BASE_URL}/public-data/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/public-data/${id}/`),
}

// ==================== 环境配置管理 ====================
export const envConfigApi = {
  list: (params?: { project?: number; browser?: string; is_default?: boolean; search?: string }) =>
    request.get<PaginatedResponse<UiEnvironmentConfig>>(`${BASE_URL}/env-configs/`, { params }),

  get: (id: number) => request.get<UiEnvironmentConfig>(`${BASE_URL}/env-configs/${id}/`),

  create: (data: UiEnvironmentConfigForm) =>
    request.post<UiEnvironmentConfig>(`${BASE_URL}/env-configs/`, data),

  update: (id: number, data: Partial<UiEnvironmentConfigForm>) =>
    request.patch<UiEnvironmentConfig>(`${BASE_URL}/env-configs/${id}/`, data),

  delete: (id: number) => request.delete(`${BASE_URL}/env-configs/${id}/`),
}

// ==================== 执行器管理 ====================
export interface ActuatorInfo {
  id: string
  name: string
  ip: string
  type: string
  is_open: boolean
  debug: boolean
  browser_type: string
  headless: boolean
  connected_at: string
  last_seen_at?: string
  version?: string
  authenticated?: boolean
  status?: string
  online_ttl_seconds?: number
}

export interface ActuatorStatus {
  total_actuators: number
  available_actuators?: number
  has_available: boolean
  web_users: number
}

export interface ActuatorPackageInfo {
  platform: 'windows' | 'linux' | 'macos' | string
  url: string
  sha256: string
  file_name: string
  available: boolean
}

export interface ActuatorOnboardingInfo {
  api_url: string
  ws_url: string
  registration_token_required: boolean
  registration_token: string
  online_ttl_seconds: number
  heartbeat_interval_seconds: number
  packages: ActuatorPackageInfo[]
  install_steps: Array<{ title: string; content: string }>
  config_template: string
  notes: string[]
}

export const actuatorApi = {
  list: () =>
    request.get<{ count: number; items: ActuatorInfo[] }>(`${BASE_URL}/actuators/list_actuators/`),

  status: () => request.get<ActuatorStatus>(`${BASE_URL}/actuators/status/`),

  onboarding: () => request.get<ActuatorOnboardingInfo>(`${BASE_URL}/actuators/onboarding/`),
}
