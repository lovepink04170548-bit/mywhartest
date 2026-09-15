<template>
  <div class="ai-generation-task-list">
    <div class="page-header">
      <div class="search-box">
        <a-select
          v-model="filters.status"
          :placeholder="pageText.status"
          allow-clear
          style="width: 140px"
          @change="onSearch"
        >
          <a-option value="pending">{{ pageText.statusPending }}</a-option>
          <a-option value="running">{{ pageText.statusRunning }}</a-option>
          <a-option value="success">{{ pageText.statusSuccess }}</a-option>
          <a-option value="failed">{{ pageText.statusFailed }}</a-option>
          <a-option value="cancelled">{{ pageText.statusCancelled }}</a-option>
        </a-select>
        <a-input-search
          v-model="filters.search"
          :placeholder="pageText.search"
          allow-clear
          style="width: 240px"
          @search="onSearch"
          @clear="onSearch"
        />
        <a-button type="outline" @click="refresh">
          <template #icon><icon-refresh /></template>
          {{ pageText.refresh }}
        </a-button>
      </div>
      <div class="action-buttons">
        <a-button type="primary" @click="openCreateModal">
          <template #icon><icon-plus /></template>
          {{ pageText.createTask }}
        </a-button>
      </div>
    </div>

    <a-table
      :columns="columns"
      :data="tasks"
      :pagination="pagination"
      :loading="loading"
      :scroll="{ x: 1820 }"
      row-key="id"
      @page-change="onPageChange"
      @page-size-change="onPageSizeChange"
    >
      <template #status="{ record }">
        <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
      </template>
      <template #duration="{ record }">
        {{ formatTaskDurationMinutes(record) }}
      </template>
      <template #source_type="{ record }">
        <a-tag color="blue">{{ sourceTypeLabel(record.source_type) }}</a-tag>
      </template>
      <template #target="{ record }">
        <div class="target-cell">
          <div>{{ record.target_module || '-' }}</div>
          <div class="muted">{{ record.target_url || '-' }}</div>
        </div>
      </template>
      <template #dispatch_status="{ record }">
        <div class="dispatch-cell">
          <a-tag :color="dispatchStatusColor(record.dispatch_status)">
            {{ dispatchStatusLabel(record.dispatch_status) }}
          </a-tag>
          <div class="muted">ACK {{ record.last_ack_at ? formatTime(record.last_ack_at) : '-' }}</div>
          <div v-if="record.dispatch_attempts" class="muted">#{{ record.dispatch_attempts }}</div>
        </div>
      </template>
      <template #updated_at="{ record }">
        {{ formatTime(record.updated_at) }}
      </template>
      <template #review_queue_count="{ record }">
        <a-badge :count="record.review_queue_count || 0" :dot="false">
          <a-tag :color="record.review_queue_count ? 'orangered' : 'gray'">
            {{ record.review_queue_count ? pageText.pendingReview : pageText.noPendingReview }}
          </a-tag>
        </a-badge>
      </template>
      <template #operations="{ record }">
        <a-space :size="4">
          <a-button type="text" size="mini" @click="viewDetail(record)">
            <template #icon><icon-eye /></template>
            {{ pageText.detail }}
          </a-button>
          <a-button
            type="text"
            size="mini"
            :disabled="!record.review_queue_count"
            @click="viewDetail(record, 'review')"
          >
            {{ pageText.review }}
          </a-button>
          <a-button
            type="text"
            size="mini"
            :loading="startingId === record.id"
            :disabled="record.status === 'running'"
            @click="startTask(record)"
          >
            <template #icon><icon-play-arrow /></template>
            {{ record.status === 'running' ? pageText.running : pageText.start }}
          </a-button>
          <a-popconfirm :content="pageText.deleteConfirm" @ok="deleteTask(record)">
            <a-button type="text" size="mini" status="danger">
              <template #icon><icon-delete /></template>
              {{ pageText.delete }}
            </a-button>
          </a-popconfirm>
        </a-space>
      </template>
    </a-table>

    <a-modal
      v-model:visible="createVisible"
      :title="pageText.createTask"
      width="820px"
      :ok-loading="submitting"
      @before-ok="submitCreate"
      @cancel="resetCreateForm"
    >
      <a-form ref="formRef" :model="formData" layout="vertical">
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item field="name" :label="pageText.taskName" required>
              <a-input v-model="formData.name" :placeholder="pageText.taskNamePlaceholder" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item field="source_type" :label="pageText.sourceType" required>
              <a-select v-model="formData.source_type">
                <a-option value="natural_language">{{ pageText.sourceNatural }}</a-option>
                <a-option value="gherkin">{{ pageText.sourceGherkin }}</a-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="24">
            <a-form-item field="execution_mode" :label="pageText.executionMode" required>
              <a-radio-group v-model="executionMode">
                <a-radio v-for="item in executionModeOptions" :key="item.value" :value="item.value">
                  {{ item.label }}
                </a-radio>
              </a-radio-group>
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item field="environment_config" :label="pageText.environment">
              <a-select
                v-model="formData.environment_config"
                :placeholder="pageText.selectEnvironment"
                allow-clear
                @focus="loadEnvConfigs"
              >
                <a-option v-for="env in envConfigs" :key="env.id" :value="env.id">
                  {{ env.name }}
                </a-option>
              </a-select>
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item field="element_map" :label="pageText.existingElementMap">
              <a-select
                v-model="formData.element_map"
                :placeholder="pageText.selectElementMap"
                allow-clear
                @focus="loadElementMaps"
              >
                <a-option v-for="map in elementMaps" :key="map.id" :value="map.id">
                  {{ elementMapOptionLabel(map) }}
                </a-option>
              </a-select>
            </a-form-item>
          </a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item field="target_url" :label="pageText.targetUrl">
              <a-input v-model="formData.target_url" :placeholder="pageText.targetUrlPlaceholder" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item field="target_module" :label="pageText.targetModule">
              <a-input v-model="formData.target_module" :placeholder="pageText.targetModulePlaceholder" />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item v-if="formData.source_type === 'natural_language'" field="source_requirement" :label="pageText.requirement" required>
          <a-textarea v-model="formData.source_requirement" :auto-size="{ minRows: 4, maxRows: 8 }" />
        </a-form-item>
        <a-form-item v-else field="gherkin" :label="pageText.gherkin" required>
          <a-textarea v-model="formData.gherkin" :auto-size="{ minRows: 4, maxRows: 8 }" />
        </a-form-item>
        <a-form-item :label="pageText.safetyPolicy">
          <a-textarea
            v-model="safetyPolicyText"
            :auto-size="{ minRows: 6, maxRows: 10 }"
          />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-drawer
      v-model:visible="detailVisible"
      :title="pageText.detailTitle"
      width="860px"
      unmount-on-close
    >
      <a-spin :loading="detailLoading">
        <template v-if="currentTask">
          <a-descriptions :column="2" bordered size="small">
            <a-descriptions-item :label="pageText.taskName">{{ currentTask.name }}</a-descriptions-item>
            <a-descriptions-item :label="pageText.status">
              <a-tag :color="statusColor(currentTask.status)">{{ statusLabel(currentTask.status) }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.dispatch">
              <div class="dispatch-cell">
                <a-tag :color="dispatchStatusColor(currentTask.dispatch_status)">
                  {{ dispatchStatusLabel(currentTask.dispatch_status) }}
                </a-tag>
                <span class="muted">#{{ currentTask.dispatch_attempts || 0 }}</span>
              </div>
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.executionMode">
              <a-tag :color="currentTask.execution_mode === 'runtime_agent' ? 'purple' : 'blue'">
                {{ currentTask.execution_mode || 'contract_planner' }}
              </a-tag>
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.environment">{{ currentTask.environment_name || '-' }}</a-descriptions-item>
            <a-descriptions-item :label="pageText.elementMap">{{ currentTask.element_map_name || '-' }}</a-descriptions-item>
            <a-descriptions-item v-if="currentTask.last_dispatch_error" :label="pageText.dispatchError" :span="2">
              {{ currentTask.last_dispatch_error }}
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.scriptHash" :span="2">
              <span class="mono">{{ currentTask.generated_script_hash || '-' }}</span>
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.appliedCase" :span="2">
              {{ getAppliedCaseId(currentTask) || '-' }}
            </a-descriptions-item>
            <a-descriptions-item :label="pageText.errorMessage" :span="2">
              {{ currentTask.error_message || '-' }}
            </a-descriptions-item>
          </a-descriptions>
          <a-space class="detail-actions">
            <a-button
              type="primary"
              status="success"
              :loading="applyingId === currentTask.id"
              :disabled="currentTask.status !== 'success' || Boolean(getAppliedCaseId(currentTask))"
              @click="applyToUiCase(currentTask)"
            >
              {{ getAppliedCaseId(currentTask) ? pageText.applied : pageText.applyToUiCase }}
            </a-button>
          </a-space>

          <a-tabs class="detail-tabs" v-model:active-key="detailActiveTab" @change="handleDetailTabChange">
            <a-tab-pane key="plan" :title="pageText.testPlan">
              <a-spin :loading="isDetailFieldLoading('test_plan')">
                <pre class="json-block">{{ formatJson(currentTask.test_plan) }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="case" :title="pageText.generatedCase">
              <a-spin :loading="isDetailFieldLoading('generated_case')">
                <pre class="json-block">{{ formatJson(currentTask.generated_case) }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="observations" :title="pageText.observations">
              <a-spin :loading="isDetailFieldLoading('mcp_observations')">
                <pre class="json-block">{{ formatJson(currentTask.mcp_observations) }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="map" :title="pageText.elementMap">
              <a-space class="map-actions">
                <a-button
                  v-if="currentTask.element_map"
                  size="small"
                  type="primary"
                  status="success"
                  @click="confirmElementMap(currentTask.element_map)"
                >
                  {{ pageText.confirmMap }}
                </a-button>
              </a-space>
              <a-spin :loading="isDetailFieldLoading('element_map_snapshot')">
                <pre class="json-block">{{ formatJson(currentTask.element_map_snapshot) }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="script" :title="pageText.script">
              <a-spin :loading="isDetailFieldLoading('generated_script')">
                <pre class="script-block">{{ currentTask.generated_script || '-' }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="verification" :title="pageText.verification">
              <a-spin :loading="isDetailFieldLoading('verification_result')">
                <pre class="json-block">{{ formatJson(currentTask.verification_result) }}</pre>
              </a-spin>
            </a-tab-pane>
            <a-tab-pane key="report" :title="pageText.report">
              <div class="report-actions">
                <a-space wrap>
                  <a-button
                    size="small"
                    type="primary"
                    :loading="downloadingArtifact === 'html'"
                    :disabled="!hasHtmlReport"
                    @click="downloadArtifact('html')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadHtml }}
                  </a-button>
                  <a-button
                    size="small"
                    :loading="downloadingArtifact === 'junit'"
                    :disabled="!hasJunitReport"
                    @click="downloadArtifact('junit')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadJunit }}
                  </a-button>
                  <a-button
                    size="small"
                    :loading="downloadingArtifact === 'json'"
                    :disabled="!currentTask"
                    @click="downloadArtifact('json')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadJson }}
                  </a-button>
                  <a-button
                    size="small"
                    :loading="downloadingArtifact === 'ts'"
                    :disabled="!hasTsSpec"
                    @click="downloadArtifact('ts')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadTs }}
                  </a-button>
                  <a-button
                    size="small"
                    :loading="downloadingArtifact === 'ts_bundle'"
                    :disabled="!hasTsBundle"
                    @click="downloadArtifact('ts_bundle')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadTsBundle }}
                  </a-button>
                  <a-button
                    size="small"
                    :loading="downloadingArtifact === 'python'"
                    :disabled="!hasPythonScript"
                    @click="downloadArtifact('python')"
                  >
                    <template #icon><icon-download /></template>
                    {{ pageText.downloadPython }}
                  </a-button>
                </a-space>
              </div>
              <a-descriptions :column="2" bordered size="small" class="report-summary">
                <a-descriptions-item label="状态">{{ reportSummary.status || '-' }}</a-descriptions-item>
                <a-descriptions-item label="失败分类">{{ reportSummary.failure_category || '-' }}</a-descriptions-item>
                <a-descriptions-item label="步骤数">{{ reportSummary.total_steps ?? '-' }}</a-descriptions-item>
                <a-descriptions-item label="失败数">{{ reportSummary.failed_steps ?? '-' }}</a-descriptions-item>
                <a-descriptions-item label="修复数">{{ reportSummary.repaired_steps ?? '-' }}</a-descriptions-item>
                <a-descriptions-item label="耗时">{{ reportSummary.duration ?? '-' }}s</a-descriptions-item>
                <a-descriptions-item label="Trace" :span="2">
                  <span class="mono">{{ reportSummary.trace_path || reportSummary.typescript_spec_trace_path || '-' }}</span>
                </a-descriptions-item>
                <a-descriptions-item label="TypeScript" :span="2">
                  {{ reportSummary.typescript_spec_validation_status || '-' }} /
                  {{ reportSummary.typescript_spec_execution_status || '-' }}
                </a-descriptions-item>
                <a-descriptions-item label="CI Gate" :span="2">
                  {{ ciQualityGate.status || '-' }}
                  <span v-if="ciQualityGate.requires_review" class="muted"> / 需要人审</span>
                </a-descriptions-item>
                <a-descriptions-item label="运行证据" :span="2">
                  Trace {{ reportSummary.typescript_runtime_trace_path ? '1' : '0' }} /
                  截图 {{ reportSummary.typescript_runtime_screenshot_count ?? 0 }} /
                  视频 {{ reportSummary.typescript_runtime_video_count ?? 0 }} /
                  网络失败 {{ reportSummary.typescript_runtime_network_failed ?? 0 }}
                </a-descriptions-item>
              </a-descriptions>
              <div class="report-section-title">{{ pageText.ciGateDetails }}</div>
              <pre class="json-block">{{ formatJson(ciQualityGate) }}</pre>
              <div class="report-section-title">{{ pageText.evidencePreview }}</div>
              <div class="evidence-grid">
                <a-empty v-if="runtimeScreenshots.length === 0 && runtimeVideos.length === 0 && !runtimeTracePath" description="暂无运行证据" />
                <a-image
                  v-for="item in runtimeScreenshots"
                  :key="item.path || item.url || item.name"
                  class="evidence-image"
                  :src="artifactUrl(item)"
                  :title="item.name || 'screenshot'"
                  fit="contain"
                  width="160"
                  height="100"
                />
                <a-link
                  v-for="item in runtimeVideos"
                  :key="item.path || item.url || item.name"
                  :href="artifactUrl(item)"
                  target="_blank"
                >
                  {{ item.name || pageText.videoArtifact }}
                </a-link>
                <a-link v-if="runtimeTracePath" :href="artifactPathUrl(runtimeTracePath)" target="_blank">
                  Trace
                </a-link>
              </div>
              <div class="report-section-title">{{ pageText.networkFailures }}</div>
              <a-table
                :columns="networkFailureColumns"
                :data="networkFailures"
                :pagination="false"
                size="mini"
                row-key="__key"
              >
                <template #url="{ record }">
                  <span class="mono">{{ record.url }}</span>
                </template>
              </a-table>
              <div class="report-section-title">{{ pageText.repairDiff }}</div>
              <a-table
                :columns="repairDiffColumns"
                :data="repairDiffRows"
                :pagination="false"
                size="mini"
                row-key="__key"
              >
                <template #reason="{ record }">
                  <span>{{ record.reason }}</span>
                </template>
              </a-table>
              <div class="report-section-title">{{ pageText.runtimeArtifacts }}</div>
              <pre class="json-block">{{ formatJson(runtimeArtifacts) }}</pre>
              <div class="report-section-title">{{ pageText.dataLifecycle }}</div>
              <pre class="json-block">{{ formatJson(dataLifecycle) }}</pre>
              <div class="report-section-title">{{ pageText.aiDecisionSummary }}</div>
              <pre class="json-block">{{ formatJson(aiDecisionSummary) }}</pre>
              <div class="report-section-title">{{ pageText.typescriptExecution }}</div>
              <pre class="json-block">{{ formatJson(typescriptExecution) }}</pre>
              <div class="report-section-title">{{ pageText.networkSummary }}</div>
              <pre class="json-block">{{ formatJson(networkSummaries) }}</pre>
            </a-tab-pane>
            <a-tab-pane key="review" :title="`${pageText.reviewQueue} (${pendingReviewCount})`">
              <div class="review-toolbar">
                <a-space>
                  <a-button
                    type="primary"
                    status="success"
                    size="small"
                    :loading="resolvingReview"
                    :disabled="pendingReviewCount === 0"
                    @click="resolveReviewQueue('approve')"
                  >
                    {{ pageText.approveAll }}
                  </a-button>
                  <a-button
                    type="outline"
                    status="danger"
                    size="small"
                    :loading="resolvingReview"
                    :disabled="pendingReviewCount === 0"
                    @click="resolveReviewQueue('reject')"
                  >
                    {{ pageText.rejectAll }}
                  </a-button>
                  <a-button size="small" :loading="reviewLoading" @click="reloadReviewQueue">
                    {{ pageText.refresh }}
                  </a-button>
                </a-space>
              </div>
              <a-empty v-if="reviewQueueItems.length === 0" :description="pageText.emptyReviewQueue" />
              <div v-else class="review-list">
                <div
                  v-for="item in reviewQueueItems"
                  :key="item.__index"
                  class="review-item"
                >
                  <div class="review-main">
                    <div class="review-title">
                      <a-tag :color="reviewStatusColor(item.status)">{{ reviewStatusLabel(item.status) }}</a-tag>
                      <span class="mono">#{{ item.step_sort ?? item.__index }}</span>
                      <strong>{{ item.target_name || item.element_key || '-' }}</strong>
                    </div>
                    <div class="review-meta">
                      {{ item.type }} · {{ item.page_key || '-' }} · {{ item.element_key || '-' }}
                    </div>
                    <div class="review-reason">{{ item.reason || item.message || '-' }}</div>
                    <a-row :gutter="12" class="locator-row">
                      <a-col :span="12">
                        <div class="locator-title">{{ pageText.oldLocator }}</div>
                        <pre class="locator-json">{{ formatJson(item.old_locator || {}) }}</pre>
                      </a-col>
                      <a-col :span="12">
                        <div class="locator-title">{{ pageText.newLocator }}</div>
                        <pre class="locator-json">{{ formatJson(item.new_locator || {}) }}</pre>
                      </a-col>
                    </a-row>
                  </div>
                  <div class="review-actions">
                    <a-button
                      type="primary"
                      status="success"
                      size="small"
                      :disabled="item.status !== 'pending'"
                      :loading="resolvingReview"
                      @click="resolveReviewQueue('approve', [item.__index])"
                    >
                      {{ pageText.approve }}
                    </a-button>
                    <a-button
                      type="outline"
                      status="danger"
                      size="small"
                      :disabled="item.status !== 'pending'"
                      :loading="resolvingReview"
                      @click="resolveReviewQueue('reject', [item.__index])"
                    >
                      {{ pageText.reject }}
                    </a-button>
                  </div>
                </div>
              </div>
            </a-tab-pane>
            <a-tab-pane key="repair" :title="pageText.repair">
              <a-spin :loading="isDetailFieldLoading('repair_history')">
                <pre class="json-block">{{ formatJson(currentTask.repair_history) }}</pre>
              </a-spin>
            </a-tab-pane>
          </a-tabs>
        </template>
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Message } from '@arco-design/web-vue'
import { IconDelete, IconDownload, IconEye, IconPlayArrow, IconPlus, IconRefresh } from '@arco-design/web-vue/es/icon'
import { useProjectStore } from '@/store/projectStore'
import { aiGenerationTaskApi, elementMapApi, envConfigApi } from '../api'
import { uiWebSocket, UiSocketEnum, type SocketDataModel } from '../services/websocket'
import type {
  AiGenerationSourceType,
  AiExecutionMode,
  AiGenerationTaskStatus,
  UiAiGenerationTask,
  UiAiGenerationTaskForm,
  UiAiReviewQueueItem,
  UiElementMap,
  UiEnvironmentConfig,
} from '../types'
import { extractPaginationData, extractResponseData } from '../types'

const projectStore = useProjectStore()
const projectId = computed(() => projectStore.currentProject?.id)

const loading = ref(false)
const submitting = ref(false)
const detailLoading = ref(false)
const reviewLoading = ref(false)
const startingId = ref<number | null>(null)
const applyingId = ref<number | null>(null)
const resolvingReview = ref(false)
const downloadingArtifact = ref<AiArtifactType | null>(null)
const createVisible = ref(false)
const detailVisible = ref(false)
const detailActiveTab = ref('plan')
const tasks = ref<UiAiGenerationTask[]>([])
const currentTask = ref<UiAiGenerationTask | null>(null)
const detailPayloadLoading = ref<AiDetailPayloadField | null>(null)
const loadedDetailFields = ref<Record<number, Partial<Record<AiDetailPayloadField, boolean>>>>({})
const envConfigs = ref<UiEnvironmentConfig[]>([])
const elementMaps = ref<UiElementMap[]>([])

const filters = reactive({
  status: undefined as AiGenerationTaskStatus | undefined,
  search: '',
})

const pagination = reactive({
  current: 1,
  pageSize: 10,
  total: 0,
  showTotal: true,
  showPageSize: true,
})

const defaultSafetyPolicy = {
  url_allowlist: [],
  url_blocklist: [],
  dangerous_action_keywords: [],
  require_confirmation_keywords: [],
  max_steps: 50,
  max_pages: 20,
  max_repair_rounds: 2,
  allow_form_submit: true,
  allow_destructive_actions: false,
  headless: true,
}
const defaultExecutionMode: AiExecutionMode = 'contract_planner'
const executionModeOptions: Array<{ label: string; value: AiExecutionMode }> = [
  { label: '合同规划模式', value: 'contract_planner' },
  { label: '运行时智能模式', value: 'runtime_agent' },
]

const formData = reactive<UiAiGenerationTaskForm>({
  project: 0,
  name: '',
  source_type: 'natural_language',
  execution_mode: defaultExecutionMode,
  source_requirement: '',
  gherkin: '',
  element_map: undefined,
  target_url: '',
  target_module: '',
  safety_policy: { ...defaultSafetyPolicy },
  max_repair_rounds: 2,
})
const executionMode = ref<AiExecutionMode>(defaultExecutionMode)
const safetyPolicyText = ref(JSON.stringify(defaultSafetyPolicy, null, 2))

const pageText = {
  status: '状态',
  statusPending: '待执行',
  statusRunning: '执行中',
  statusSuccess: '成功',
  statusFailed: '失败',
  statusCancelled: '已取消',
  search: '搜索任务名称/目标',
  refresh: '刷新',
  createTask: '创建任务',
  taskName: '任务名称',
  taskNamePlaceholder: '例如：新建任务最简必填项流程',
  sourceType: '输入类型',
  sourceNatural: '自然语言',
  sourceGherkin: 'Gherkin',
  executionMode: '执行方案',
  environment: '环境',
  selectEnvironment: '选择执行环境',
  existingElementMap: '已有元素地图',
  selectElementMap: '可选：选择已确认元素地图',
  targetUrl: '目标 URL',
  targetUrlPlaceholder: '为空时使用环境 base_url',
  targetModule: '目标模块',
  targetModulePlaceholder: '例如：任务管理',
  requirement: '自然语言需求',
  gherkin: 'Gherkin',
  safetyPolicy: '安全边界策略 JSON',
  dispatch: '分发',
  dispatchPending: '待下发',
  dispatchSent: '已下发',
  dispatchAcked: '已入队',
  dispatchDuplicate: '重复入队',
  dispatchCompleted: '已完成',
  dispatchFailed: '下发失败',
  dispatchError: '分发错误',
  detail: '详情',
  start: '启动',
  running: '运行中',
  delete: '删除',
  deleteConfirm: '确认删除该 AI 生成任务？',
  detailTitle: 'AI 生成任务详情',
  elementMap: '元素地图',
  scriptHash: '脚本 Hash',
  appliedCase: '已应用测试用例',
  errorMessage: '错误信息',
  testPlan: '测试计划',
  generatedCase: '用例规划',
  observations: 'MCP观察',
  script: '脚本',
  verification: '验证',
  report: '报告',
  downloadHtml: 'HTML报告',
  downloadJunit: 'JUnit XML',
  downloadJson: '完整JSON',
  downloadTs: 'TS Spec',
  downloadTsBundle: 'TS文件包',
  downloadPython: 'Python脚本',
  ciGateDetails: 'CI 门禁详情',
  evidencePreview: '运行证据预览',
  networkFailures: '网络失败请求',
  repairDiff: '修复变更',
  videoArtifact: '视频证据',
  runtimeArtifacts: '运行证据产物',
  dataLifecycle: '数据生命周期',
  aiDecisionSummary: 'AI 决策摘要',
  typescriptExecution: 'TypeScript 真实执行',
  networkSummary: '网络摘要',
  review: '人审',
  reviewQueue: '人审队列',
  pendingReview: '待审',
  noPendingReview: '无待审',
  emptyReviewQueue: '暂无人审项',
  approve: '确认',
  reject: '驳回',
  approveAll: '确认全部待审',
  rejectAll: '驳回全部待审',
  oldLocator: '原定位',
  newLocator: '新定位',
  reviewSuccess: '人审队列已处理',
  repair: '修复',
  confirmMap: '确认地图',
  applyToUiCase: '应用为UI用例',
  applied: '已应用',
  generationSuccess: 'AI 生成任务已完成',
  generationFailed: 'AI 生成任务失败',
}

const columns = [
  { title: 'ID', dataIndex: 'id', width: 80 },
  { title: '任务名称', dataIndex: 'name', width: 220, ellipsis: true, tooltip: true },
  { title: '状态', slotName: 'status', width: 110 },
  { title: '执行时间(min)', slotName: 'duration', width: 130 },
  { title: '来源', slotName: 'source_type', width: 120 },
  { title: '目标', slotName: 'target', width: 300 },
  { title: '环境', dataIndex: 'environment_name', width: 160 },
  { title: '分发', slotName: 'dispatch_status', width: 150 },
  { title: '失败分类', dataIndex: 'failure_category', width: 120 },
  { title: '人审', slotName: 'review_queue_count', width: 120 },
  { title: '更新时间', slotName: 'updated_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 250, fixed: 'right' },
]

const statusLabel = (status: AiGenerationTaskStatus) => ({
  pending: pageText.statusPending,
  running: pageText.statusRunning,
  success: pageText.statusSuccess,
  failed: pageText.statusFailed,
  cancelled: pageText.statusCancelled,
}[status] || status)

const statusColor = (status: AiGenerationTaskStatus) => ({
  pending: 'gray',
  running: 'blue',
  success: 'green',
  failed: 'red',
  cancelled: 'orange',
}[status] || 'gray')

const sourceTypeLabel = (type: AiGenerationSourceType) => ({
  natural_language: pageText.sourceNatural,
  gherkin: pageText.sourceGherkin,
  testcase: '测试用例',
  requirement: '需求',
}[type] || type)

const dispatchStatusLabel = (status?: string) => ({
  pending: pageText.dispatchPending,
  sent: pageText.dispatchSent,
  acked: pageText.dispatchAcked,
  duplicate: pageText.dispatchDuplicate,
  completed: pageText.dispatchCompleted,
  failed: pageText.dispatchFailed,
}[status || 'pending'] || status || pageText.dispatchPending)

const dispatchStatusColor = (status?: string) => ({
  pending: 'gray',
  sent: 'blue',
  acked: 'cyan',
  duplicate: 'orange',
  completed: 'green',
  failed: 'red',
}[status || 'pending'] || 'gray')

const formatTime = (time?: string) => {
  if (!time) return '-'
  return new Date(time).toLocaleString()
}

const formatTaskDurationMinutes = (task: UiAiGenerationTask) => {
  const startSource = task.started_at || task.last_dispatched_at || task.created_at
  if (!startSource) return '-'
  const startTime = new Date(startSource).getTime()
  const endTime = task.completed_at
    ? new Date(task.completed_at).getTime()
    : task.status === 'running'
      ? Date.now()
      : NaN
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime) || endTime < startTime) return '-'
  return ((endTime - startTime) / 60000).toFixed(2)
}

const formatJson = (data: unknown) => JSON.stringify(data || {}, null, 2)

const markDetailFieldLoaded = (taskId: number, field: AiDetailPayloadField) => {
  loadedDetailFields.value = {
    ...loadedDetailFields.value,
    [taskId]: {
      ...(loadedDetailFields.value[taskId] || {}),
      [field]: true,
    },
  }
}

const isDetailFieldLoaded = (field: AiDetailPayloadField) => {
  if (!currentTask.value) return false
  return Boolean(loadedDetailFields.value[currentTask.value.id]?.[field])
}

const isDetailFieldLoading = (field: AiDetailPayloadField) => detailPayloadLoading.value === field

const loadDetailPayload = async (field: AiDetailPayloadField, force = false) => {
  if (!currentTask.value) return
  const taskId = currentTask.value.id
  if (!force && isDetailFieldLoaded(field)) return
  detailPayloadLoading.value = field
  try {
    const res = await aiGenerationTaskApi.getDetailPayload(taskId, field)
    const payload = extractResponseData<{ field?: string; value?: unknown }>(res)
    currentTask.value = {
      ...currentTask.value,
      [field]: payload?.value,
    } as UiAiGenerationTask
    markDetailFieldLoaded(taskId, field)
  } catch (err: any) {
    Message.error(err?.message || '加载详情数据失败')
  } finally {
    if (detailPayloadLoading.value === field) {
      detailPayloadLoading.value = null
    }
  }
}

const loadActiveDetailPayload = async (tab = detailActiveTab.value) => {
  const target = DETAIL_TAB_FIELD_MAP[tab]
  if (!target || !currentTask.value) return
  if (target === 'review_queue') {
    await reloadReviewQueue()
    return
  }
  await loadDetailPayload(target)
}

const handleDetailTabChange = (key: string | number) => {
  loadActiveDetailPayload(String(key))
}

type ReviewQueueItemWithIndex = UiAiReviewQueueItem & { __index: number }
type AiArtifactType = 'html' | 'junit' | 'json' | 'ts' | 'ts_bundle' | 'python'
type AiDetailPayloadField =
  | 'test_plan'
  | 'mcp_observations'
  | 'element_map_snapshot'
  | 'generated_case'
  | 'generated_script'
  | 'verification_result'
  | 'repair_history'

const DETAIL_TAB_FIELD_MAP: Record<string, AiDetailPayloadField | 'review_queue' | undefined> = {
  plan: 'test_plan',
  case: 'generated_case',
  observations: 'mcp_observations',
  map: 'element_map_snapshot',
  script: 'generated_script',
  verification: 'verification_result',
  report: 'verification_result',
  review: 'review_queue',
  repair: 'repair_history',
}

const asRecord = (value: unknown): Record<string, any> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {}

const verificationResult = computed(() => asRecord(currentTask.value?.verification_result))
const reportArtifacts = computed(() => asRecord(verificationResult.value.report_artifacts))
const reportSummary = computed(() => asRecord(reportArtifacts.value.summary))
const ciQualityGate = computed(() => asRecord(reportSummary.value.ci_quality_gate))
const runtimeArtifacts = computed(() => asRecord(reportSummary.value.runtime_artifacts))
const runtimeTracePath = computed(() => {
  const trace = asRecord(runtimeArtifacts.value.trace)
  return String(trace.path || reportSummary.value.typescript_runtime_trace_path || reportSummary.value.typescript_spec_trace_path || '')
})
const runtimeScreenshots = computed(() => {
  const screenshots = runtimeArtifacts.value.screenshots
  return Array.isArray(screenshots)
    ? screenshots.filter((item): item is Record<string, any> => Boolean(item) && typeof item === 'object')
    : []
})
const runtimeVideos = computed(() => {
  const videos = runtimeArtifacts.value.videos
  return Array.isArray(videos)
    ? videos.filter((item): item is Record<string, any> => Boolean(item) && typeof item === 'object')
    : []
})
const networkFailures = computed(() => {
  const network = asRecord(runtimeArtifacts.value.network_summary)
  const failed = Array.isArray(network.failed) ? network.failed : []
  return failed
    .filter((item): item is Record<string, any> => Boolean(item) && typeof item === 'object')
    .map((item, index) => ({ ...item, __key: `${index}-${item.url || ''}` }))
})
const dataLifecycle = computed(() => asRecord(verificationResult.value.data_lifecycle))
const scriptArtifacts = computed(() => asRecord(verificationResult.value.script_artifacts))
const typescriptExecution = computed(() => asRecord(scriptArtifacts.value.typescript_spec_execution))
const generatedCase = computed(() => asRecord(currentTask.value?.generated_case))
const hasHtmlReport = computed(() => Boolean(reportArtifacts.value.html))
const hasJunitReport = computed(() => Boolean(reportArtifacts.value.junit_xml))
const hasTsSpec = computed(() => Boolean(
  scriptArtifacts.value.playwright_ts_spec
  || generatedCase.value.playwright_ts_spec
  || scriptArtifacts.value.playwright_ts_spec_hash
  || generatedCase.value.playwright_ts_spec_hash
  || currentTask.value?.generated_script_hash,
))
const hasTsBundle = computed(() => {
  const files = asRecord(scriptArtifacts.value.playwright_ts_files || generatedCase.value.playwright_ts_files)
  return hasTsSpec.value || Object.keys(files).length > 0
})
const hasPythonScript = computed(() => Boolean(currentTask.value?.generated_script))
const networkSummaries = computed(() => {
  const map = asRecord(currentTask.value?.element_map_snapshot)
  const pages = Array.isArray(map.pages) ? map.pages : []
  return pages
    .filter((page: unknown): page is Record<string, any> => Boolean(page) && typeof page === 'object')
    .map((page) => ({
      page_key: page.page_key || page.url || page.title || '',
      url: page.url || '',
      title: page.title || '',
      network_summary: page.network_summary || {},
    }))
})

const reviewQueueItems = computed<ReviewQueueItemWithIndex[]>(() => {
  const verification = currentTask.value?.verification_result as Record<string, unknown> | undefined
  const queue = verification?.review_queue
  if (!Array.isArray(queue)) return []
  return queue
    .filter((item): item is UiAiReviewQueueItem => Boolean(item) && typeof item === 'object')
    .map((item, index) => ({ ...item, __index: index }))
})

const pendingReviewCount = computed(() =>
  reviewQueueItems.value.filter((item) => item.status === 'pending').length
)

const aiDecisionSummary = computed(() => ({
  intent: generatedCase.value.intent_analysis || {},
  test_plan: currentTask.value?.test_plan || {},
  plan_validation: asRecord(verificationResult.value.plan_validation),
  report_summary: reportSummary.value,
  repair_rounds: Array.isArray(currentTask.value?.repair_history) ? currentTask.value?.repair_history.length : 0,
  review_queue_count: pendingReviewCount.value,
}))

const repairDiffRows = computed(() => {
  const history = Array.isArray(currentTask.value?.repair_history) ? currentTask.value?.repair_history : []
  const rows: Array<Record<string, any>> = []
  history.forEach((item: any, historyIndex: number) => {
    if (!item || typeof item !== 'object') return
    const changed = Array.isArray(item.changed_steps) ? item.changed_steps : []
    if (changed.length) {
      changed.forEach((step: any, index: number) => {
        rows.push({
          __key: `${historyIndex}-${index}`,
          type: item.type || '',
          step_sort: step?.step_sort ?? '-',
          reason: step?.reason || item.message || '',
          old: step?.old || '',
          new: step?.new || '',
        })
      })
      return
    }
    if (item.type === 'ai_plan_step_locator_patch' || item.type === 'llm_typescript_spec_patch') {
      rows.push({
        __key: `${historyIndex}`,
        type: item.type || '',
        step_sort: item.step_sort ?? '-',
        reason: item.message || '',
        old: JSON.stringify(item.old_locator || {}),
        new: JSON.stringify(item.new_locator || item.review_notes || {}),
      })
    }
  })
  return rows
})

const networkFailureColumns = [
  { title: '方法', dataIndex: 'method', width: 90 },
  { title: '状态', dataIndex: 'status', width: 90 },
  { title: '类型', dataIndex: 'resource_type', width: 120 },
  { title: 'URL', slotName: 'url', ellipsis: true, tooltip: true },
]

const repairDiffColumns = [
  { title: '类型', dataIndex: 'type', width: 180 },
  { title: '步骤', dataIndex: 'step_sort', width: 80 },
  { title: '原因', slotName: 'reason', ellipsis: true, tooltip: true },
  { title: '旧', dataIndex: 'old', ellipsis: true, tooltip: true },
  { title: '新', dataIndex: 'new', ellipsis: true, tooltip: true },
]

const artifactPathUrl = (path?: string) => {
  if (!path) return ''
  if (/^https?:\/\//i.test(path) || path.startsWith('data:') || path.startsWith('/media/')) return path
  return `/media/${path.replace(/^\/+/, '')}`
}

const artifactUrl = (item: Record<string, any>) => artifactPathUrl(String(item.url || item.path || ''))

const reviewStatusLabel = (status?: string) => ({
  pending: '待审',
  approved: '已确认',
  rejected: '已驳回',
}[status || ''] || status || '-')

const reviewStatusColor = (status?: string) => ({
  pending: 'orangered',
  approved: 'green',
  rejected: 'gray',
}[status || ''] || 'gray')

const loadEnvConfigs = async () => {
  if (!projectId.value) return
  const res = await envConfigApi.list({ project: projectId.value })
  envConfigs.value = extractPaginationData(res).items as UiEnvironmentConfig[]
}

const loadElementMaps = async () => {
  if (!projectId.value) return
  const res = await elementMapApi.list({ project: projectId.value, status: 'confirmed', stale_status: 'current' })
  elementMaps.value = extractPaginationData(res).items as UiElementMap[]
}

const elementMapOptionLabel = (map: UiElementMap) => {
  const scope = [map.role_key, map.permission_key].filter(Boolean).join('/')
  const stale = map.stale_status && map.stale_status !== 'current' ? ` ${map.stale_status}` : ''
  return `${map.name} v${map.version}${scope ? ` ${scope}` : ''}${stale}`
}

let statusPollTimer: ReturnType<typeof setTimeout> | null = null

const stopStatusPolling = () => {
  if (statusPollTimer) {
    clearTimeout(statusPollTimer)
    statusPollTimer = null
  }
}

const scheduleStatusPolling = () => {
  stopStatusPolling()
  const hasRunningTask = tasks.value.some(task => task.status === 'running')
    || currentTask.value?.status === 'running'
  if (!hasRunningTask) return
  statusPollTimer = setTimeout(() => {
    statusPollTimer = null
    refresh()
  }, 5000)
}

const refresh = async () => {
  if (!projectId.value) return
  loading.value = true
  try {
    const res = await aiGenerationTaskApi.list({
      project: projectId.value,
      status: filters.status,
      search: filters.search || undefined,
    })
    const { items, count } = extractPaginationData(res)
    tasks.value = items as UiAiGenerationTask[]
    pagination.total = count
    if (currentTask.value) {
      const latest = tasks.value.find(task => task.id === currentTask.value?.id)
      if (latest) currentTask.value = { ...currentTask.value, ...latest }
      if (!latest && currentTask.value.status === 'running') {
        const detailRes = await aiGenerationTaskApi.get(currentTask.value.id)
        currentTask.value = extractResponseData<UiAiGenerationTask>(detailRes) || currentTask.value
      }
    }
  } catch (err: any) {
    Message.error(err?.message || '加载 AI 生成任务失败')
  } finally {
    loading.value = false
    scheduleStatusPolling()
  }
}

const onSearch = () => {
  pagination.current = 1
  refresh()
}

const onPageChange = (page: number) => {
  pagination.current = page
  refresh()
}

const onPageSizeChange = (size: number) => {
  pagination.pageSize = size
  pagination.current = 1
  refresh()
}

const resetCreateForm = () => {
  formData.project = projectId.value || 0
  formData.name = ''
  formData.source_type = 'natural_language'
  formData.execution_mode = defaultExecutionMode
  formData.source_requirement = ''
  formData.gherkin = ''
  formData.element_map = undefined
  formData.target_url = ''
  formData.target_module = ''
  formData.safety_policy = { ...defaultSafetyPolicy }
  formData.max_repair_rounds = 2
  executionMode.value = defaultExecutionMode
  safetyPolicyText.value = JSON.stringify(defaultSafetyPolicy, null, 2)
}

const openCreateModal = async () => {
  if (!projectId.value) {
    Message.warning('请先选择项目')
    return
  }
  resetCreateForm()
  await loadEnvConfigs()
  await loadElementMaps()
  createVisible.value = true
}

const submitCreate = async () => {
  if (!projectId.value) return false
  if (!formData.name?.trim()) {
    Message.warning('请输入任务名称')
    return false
  }
  if (formData.source_type === 'natural_language' && !formData.source_requirement?.trim()) {
    Message.warning('请输入自然语言需求')
    return false
  }
  if (formData.source_type === 'gherkin' && !formData.gherkin?.trim()) {
    Message.warning('请输入 Gherkin')
    return false
  }
  submitting.value = true
  try {
    formData.project = projectId.value
    formData.execution_mode = executionMode.value
    let parsedPolicy: Record<string, unknown>
    try {
      parsedPolicy = JSON.parse(safetyPolicyText.value || '{}')
    } catch {
      Message.error('安全边界策略必须是合法 JSON')
      return false
    }
    formData.safety_policy = {
      ...parsedPolicy,
      execution_mode: executionMode.value,
    }
    const res = await aiGenerationTaskApi.create(formData)
    const created = extractResponseData<UiAiGenerationTask>(res)
    Message.success('AI 生成任务已创建')
    createVisible.value = false
    await refresh()
    if (created) {
      await startTask(created)
    }
    return true
  } catch (err: any) {
    Message.error(err?.message || '创建 AI 生成任务失败')
    return false
  } finally {
    submitting.value = false
  }
}

const startTask = async (task: UiAiGenerationTask) => {
  startingId.value = task.id
  try {
    await aiGenerationTaskApi.start(task.id)
    Message.success('任务已发送给执行器')
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '启动任务失败')
  } finally {
    startingId.value = null
  }
}

const viewDetail = async (task: UiAiGenerationTask, tab = 'plan') => {
  detailVisible.value = true
  detailActiveTab.value = tab
  detailLoading.value = true
  try {
    const res = await aiGenerationTaskApi.get(task.id)
    currentTask.value = extractResponseData<UiAiGenerationTask>(res) || task
    loadedDetailFields.value = {
      ...loadedDetailFields.value,
      [task.id]: {},
    }
    await loadActiveDetailPayload(tab)
  } catch (err: any) {
    Message.error(err?.message || '加载任务详情失败')
    currentTask.value = task
  } finally {
    detailLoading.value = false
  }
}

const reloadReviewQueue = async () => {
  if (!currentTask.value) return
  reviewLoading.value = true
  try {
    const res = await aiGenerationTaskApi.getReviewQueue(currentTask.value.id)
    const payload = extractResponseData<{
      items: UiAiReviewQueueItem[]
      pending_count: number
    }>(res)
    const verification = {
      ...(currentTask.value.verification_result || {}),
      review_queue: payload?.items || [],
    }
    currentTask.value = {
      ...currentTask.value,
      verification_result: verification,
      review_queue_count: payload?.pending_count || 0,
    }
  } catch (err: any) {
    Message.error(err?.message || '加载人审队列失败')
  } finally {
    reviewLoading.value = false
  }
}

const resolveReviewQueue = async (action: 'approve' | 'reject', indexes?: number[]) => {
  if (!currentTask.value) return
  resolvingReview.value = true
  try {
    const res = await aiGenerationTaskApi.resolveReviewQueue(currentTask.value.id, { action, indexes })
    const payload = extractResponseData<{
      task?: UiAiGenerationTask
      pending_count?: number
      errors?: Array<{ index: number; error: string }>
    }>(res)
    if (payload?.task) {
      currentTask.value = payload.task
    } else {
      await viewDetail(currentTask.value, 'review')
    }
    if (payload?.errors?.length) {
      Message.warning(`已处理部分人审项，${payload.errors.length} 项失败`)
    } else {
      Message.success(pageText.reviewSuccess)
    }
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '处理人审队列失败')
  } finally {
    resolvingReview.value = false
  }
}

const confirmElementMap = async (id: number) => {
  try {
    await elementMapApi.confirm(id)
    Message.success('元素地图已确认')
    if (currentTask.value) await viewDetail(currentTask.value)
  } catch (err: any) {
    Message.error(err?.message || '确认元素地图失败')
  }
}

const getAppliedCaseId = (task: UiAiGenerationTask) => {
  const generatedCase = task.generated_case as Record<string, unknown> | undefined
  const id = generatedCase?.applied_test_case_id
  return typeof id === 'number' || typeof id === 'string' ? id : ''
}

const applyToUiCase = async (task: UiAiGenerationTask) => {
  applyingId.value = task.id
  try {
    const res = await aiGenerationTaskApi.applyToUiCase(task.id)
    const payload = extractResponseData<{ task?: UiAiGenerationTask; test_case_id?: number }>(res)
    Message.success(`已应用为 UI 测试用例${payload?.test_case_id ? ` #${payload.test_case_id}` : ''}`)
    if (payload?.task) {
      currentTask.value = payload.task
    } else {
      await viewDetail(task)
    }
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '应用到 UI 测试用例失败')
  } finally {
    applyingId.value = null
  }
}

const defaultArtifactFilename = (task: UiAiGenerationTask, type: AiArtifactType) => {
  const safeName = (task.name || `task_${task.id}`).replace(/[^\w.-]+/g, '_').slice(0, 80)
  const suffixMap: Record<AiArtifactType, string> = {
    html: '_report.html',
    junit: '_junit.xml',
    json: '_artifact.json',
    ts: '.spec.ts',
    ts_bundle: '_playwright_ts_bundle.zip',
    python: '.py',
  }
  return `${safeName}${suffixMap[type]}`
}

const filenameFromDisposition = (disposition?: string) => {
  if (!disposition) return ''
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      return utf8Match[1]
    }
  }
  const match = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/)
  return match?.[1]?.replace(/['"]/g, '') || ''
}

const saveBlob = (blob: Blob, filename: string) => {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}

const downloadArtifact = async (type: AiArtifactType) => {
  if (!currentTask.value) return
  downloadingArtifact.value = type
  try {
    const res = await aiGenerationTaskApi.downloadArtifact(currentTask.value.id, type)
    const blob = extractResponseData<Blob>(res)
    if (!(blob instanceof Blob)) {
      Message.error('下载失败：服务端未返回有效文件')
      return
    }
    const filename = filenameFromDisposition((res as any).headers?.['content-disposition'])
      || defaultArtifactFilename(currentTask.value, type)
    saveBlob(blob, filename)
  } catch (err: any) {
    Message.error(err?.message || '下载产物失败')
  } finally {
    downloadingArtifact.value = null
  }
}

const deleteTask = async (task: UiAiGenerationTask) => {
  try {
    await aiGenerationTaskApi.delete(task.id)
    Message.success('已删除')
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '删除失败')
  }
}

const handleAiGenerationResult = async (data: SocketDataModel) => {
  const result = data.data?.func_args || {}
  const taskId = result.task_id
  if (result.status === 'success') {
    Message.success(pageText.generationSuccess)
  } else {
    Message.error(result.message || pageText.generationFailed)
  }
  await refresh()
  if (detailVisible.value && currentTask.value && currentTask.value.id === taskId) {
    await viewDetail(currentTask.value)
  }
}

const handleAiGenerationAck = async () => {
  await refresh()
  if (detailVisible.value && currentTask.value) {
    await viewDetail(currentTask.value, detailActiveTab.value)
  }
}

let offAiGenerationAck: (() => void) | null = null
let offAiGenerationResult: (() => void) | null = null

watch(projectId, () => {
  refresh()
}, { immediate: true })

onMounted(async () => {
  offAiGenerationAck = uiWebSocket.on(UiSocketEnum.AI_GENERATION_ACK, handleAiGenerationAck)
  offAiGenerationResult = uiWebSocket.on(UiSocketEnum.AI_GENERATION_RESULT, handleAiGenerationResult)
  if (!uiWebSocket.connected.value) {
    try {
      await uiWebSocket.connect()
    } catch {
      // 任务结果仍可通过手动刷新获取，WebSocket 失败不阻塞列表使用。
    }
  }
})

onUnmounted(() => {
  stopStatusPolling()
  offAiGenerationAck?.()
  offAiGenerationResult?.()
})

defineExpose({ refresh })
</script>

<style scoped>
.ai-generation-task-list {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.search-box,
.action-buttons {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.target-cell,
.dispatch-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  line-height: 1.35;
}

.muted {
  color: var(--color-text-3);
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  word-break: break-all;
}

.detail-tabs {
  margin-top: 16px;
}

.detail-actions {
  margin-top: 12px;
}

.json-block,
.script-block {
  max-height: 560px;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  background: var(--color-fill-1);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
}

.map-actions {
  margin-bottom: 10px;
}

.report-actions {
  margin-bottom: 12px;
}

.report-summary {
  margin-bottom: 14px;
}

.report-section-title {
  margin: 14px 0 8px;
  font-weight: 600;
  color: var(--color-text-1);
}

.evidence-grid {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px;
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  background: var(--color-bg-2);
}

.evidence-image {
  border: 1px solid var(--color-border-2);
  border-radius: 4px;
  background: var(--color-fill-1);
}

.review-toolbar {
  margin-bottom: 12px;
}

.review-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.review-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  background: var(--color-bg-2);
}

.review-main {
  min-width: 0;
}

.review-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.review-meta,
.review-reason {
  color: var(--color-text-2);
  font-size: 12px;
  line-height: 1.5;
}

.review-reason {
  margin-top: 4px;
}

.review-actions {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.locator-row {
  margin-top: 10px;
}

.locator-title {
  margin-bottom: 4px;
  color: var(--color-text-2);
  font-size: 12px;
}

.locator-json {
  max-height: 160px;
  overflow: auto;
  padding: 8px;
  border: 1px solid var(--color-border-2);
  border-radius: 6px;
  background: var(--color-fill-1);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.45;
}
</style>
