<template>
  <div class="element-map-version-list">
    <div class="page-header">
      <div class="search-box">
        <a-select
          v-model="filters.status"
          placeholder="状态"
          allow-clear
          style="width: 120px"
          @change="onSearch"
        >
          <a-option value="draft">草稿</a-option>
          <a-option value="confirmed">已确认</a-option>
          <a-option value="archived">已归档</a-option>
        </a-select>
        <a-select
          v-model="filters.stale_status"
          placeholder="版本状态"
          allow-clear
          style="width: 130px"
          @change="onSearch"
        >
          <a-option value="current">当前</a-option>
          <a-option value="superseded">已过期</a-option>
        </a-select>
        <a-input-search
          v-model="filters.search"
          placeholder="搜索地图/URL/版本组"
          allow-clear
          style="width: 240px"
          @search="onSearch"
          @clear="onSearch"
        />
        <a-button type="outline" @click="refresh">
          <template #icon><icon-refresh /></template>
          刷新
        </a-button>
        <a-button type="primary" :disabled="!projectId" @click="openManualCapture">
          <template #icon><icon-record /></template>
          人工采集
        </a-button>
        <a-popconfirm
          content="确定要删除选中的元素地图吗？此操作不可恢复。"
          @ok="batchDeleteMaps"
        >
          <a-button
            type="primary"
            status="danger"
            :disabled="selectedRowKeys.length === 0"
          >
            <template #icon><icon-delete /></template>
            批量删除
          </a-button>
        </a-popconfirm>
      </div>
    </div>

    <a-table
      :columns="columns"
      :data="maps"
      :pagination="pagination"
      :loading="loading"
      :scroll="{ x: 1500 }"
      :row-selection="{ type: 'checkbox', showCheckedAll: true }"
      v-model:selectedKeys="selectedRowKeys"
      row-key="id"
      @page-change="onPageChange"
      @page-size-change="onPageSizeChange"
    >
      <template #name="{ record }">
        <div class="name-cell">
          <strong>{{ record.name }}</strong>
          <span class="muted mono">{{ record.version_group || '-' }}</span>
        </div>
      </template>
      <template #version="{ record }">
        <a-tag color="blue">v{{ record.version }}</a-tag>
      </template>
      <template #status="{ record }">
        <a-space :size="4">
          <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
          <a-tag :color="staleColor(record.stale_status)">{{ staleLabel(record.stale_status) }}</a-tag>
        </a-space>
      </template>
      <template #scope="{ record }">
        <div class="scope-cell">
          <span>{{ record.environment_name || '-' }}</span>
          <span class="muted">{{ record.role_key || 'default' }} / {{ record.permission_key || 'default' }}</span>
        </div>
      </template>
      <template #coverage="{ record }">
        <div class="coverage-cell">
          <span>页面 {{ record.page_count ?? coverage(record).page_count }}</span>
          <span>元素 {{ record.element_count ?? coverage(record).element_count }}</span>
          <span>低置信 {{ coverage(record).low_confidence_count }}</span>
        </div>
      </template>
      <template #diff="{ record }">
        <div class="diff-cell">
          <span>新增 {{ diff(record).added_element_count }}</span>
          <span>移除 {{ diff(record).removed_element_count }}</span>
          <span>定位变更 {{ diff(record).locator_changed_count }}</span>
        </div>
      </template>
      <template #hash="{ record }">
        <div class="hash-cell">
          <span class="mono">map {{ shortHash(record.map_hash) }}</span>
          <span class="mono">baseline {{ shortHash(record.baseline_hash) }}</span>
        </div>
      </template>
      <template #updated_at="{ record }">
        {{ formatTime(record.updated_at) }}
      </template>
      <template #operations="{ record }">
        <a-space :size="4">
          <a-button type="text" size="mini" @click="openDetail(record)">
            <template #icon><icon-eye /></template>
            详情
          </a-button>
          <a-button
            v-if="record.status !== 'confirmed'"
            type="text"
            size="mini"
            status="success"
            :loading="confirmingId === record.id"
            @click="confirmMap(record)"
          >
            <template #icon><icon-check /></template>
            确认为当前
          </a-button>
          <a-popconfirm
            v-if="record.status !== 'archived'"
            content="确认归档该元素地图版本？"
            @ok="archiveMap(record)"
          >
            <a-button type="text" size="mini" status="warning" :loading="archivingId === record.id">
              归档
            </a-button>
          </a-popconfirm>
        </a-space>
      </template>
    </a-table>

    <a-drawer
      v-model:visible="detailVisible"
      width="920px"
      :title="currentMap ? `${currentMap.name} v${currentMap.version}` : '元素地图详情'"
      unmount-on-close
    >
      <a-spin :loading="detailLoading">
        <template v-if="currentMap">
          <a-descriptions :column="2" bordered size="small">
            <a-descriptions-item label="状态">
              <a-space :size="4">
                <a-tag :color="statusColor(currentMap.status)">{{ statusLabel(currentMap.status) }}</a-tag>
                <a-tag :color="staleColor(currentMap.stale_status)">{{ staleLabel(currentMap.stale_status) }}</a-tag>
              </a-space>
            </a-descriptions-item>
            <a-descriptions-item label="环境">{{ currentMap.environment_name || '-' }}</a-descriptions-item>
            <a-descriptions-item label="角色/权限">
              {{ currentMap.role_key || 'default' }} / {{ currentMap.permission_key || 'default' }}
            </a-descriptions-item>
            <a-descriptions-item label="版本组">
              <span class="mono">{{ currentMap.version_group || '-' }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="基础 URL" :span="2">
              <span class="mono">{{ currentMap.base_url || '-' }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="地图 Hash" :span="2">
              <span class="mono">{{ currentMap.map_hash || '-' }}</span>
            </a-descriptions-item>
            <a-descriptions-item label="基线 Hash" :span="2">
              <span class="mono">{{ currentMap.baseline_hash || '-' }}</span>
            </a-descriptions-item>
            <a-descriptions-item v-if="currentMap.stale_reason" label="过期原因" :span="2">
              {{ currentMap.stale_reason }}
            </a-descriptions-item>
          </a-descriptions>

          <a-tabs class="detail-tabs" v-model:active-key="detailTab">
            <a-tab-pane key="summary" title="摘要">
              <a-descriptions :column="3" bordered size="small">
                <a-descriptions-item label="页面">{{ coverage(currentMap).page_count }}</a-descriptions-item>
                <a-descriptions-item label="元素">{{ coverage(currentMap).element_count }}</a-descriptions-item>
                <a-descriptions-item label="低置信">{{ coverage(currentMap).low_confidence_count }}</a-descriptions-item>
                <a-descriptions-item label="风险入口">{{ coverage(currentMap).risk_count }}</a-descriptions-item>
                <a-descriptions-item label="SoM元素">{{ coverage(currentMap).som_element_count }}</a-descriptions-item>
                <a-descriptions-item label="虚拟列表">{{ coverage(currentMap).virtual_list_container_count }}</a-descriptions-item>
              </a-descriptions>
              <div class="section-title">差异摘要</div>
              <pre class="json-block">{{ formatJson(currentMap.diff_summary) }}</pre>
            </a-tab-pane>
            <a-tab-pane key="versions" :title="`版本历史 (${groupVersions.length})`">
              <a-table
                :columns="versionColumns"
                :data="groupVersions"
                :pagination="false"
                :loading="versionsLoading"
                size="small"
                row-key="id"
              >
                <template #version="{ record }">
                  <a-tag color="blue">v{{ record.version }}</a-tag>
                </template>
                <template #status="{ record }">
                  <a-space :size="4">
                    <a-tag :color="statusColor(record.status)">{{ statusLabel(record.status) }}</a-tag>
                    <a-tag :color="staleColor(record.stale_status)">{{ staleLabel(record.stale_status) }}</a-tag>
                  </a-space>
                </template>
                <template #diff="{ record }">
                  新增 {{ diff(record).added_element_count }} /
                  移除 {{ diff(record).removed_element_count }} /
                  定位 {{ diff(record).locator_changed_count }}
                </template>
                <template #updated_at="{ record }">
                  {{ formatTime(record.updated_at) }}
                </template>
                <template #operations="{ record }">
                  <a-button type="text" size="mini" @click="openDetail(record)">查看</a-button>
                </template>
              </a-table>
            </a-tab-pane>
            <a-tab-pane key="baseline" title="定位基线">
              <pre class="json-block">{{ formatJson(mapJson(currentMap).locator_baseline || []) }}</pre>
            </a-tab-pane>
            <a-tab-pane key="map" title="地图 JSON">
              <pre class="json-block">{{ formatJson(currentMap.map_json) }}</pre>
            </a-tab-pane>
          </a-tabs>
        </template>
      </a-spin>
    </a-drawer>

    <a-modal
      v-model:visible="manualCaptureVisible"
      title="人工采集元素地图"
      :footer="false"
      :mask-closable="!manualCaptureRunning"
    >
      <a-alert
        v-if="manualCaptureRunning"
        type="info"
        class="manual-capture-alert"
      >
        浏览器采集中。请在弹出的浏览器中完成操作，关闭浏览器后系统会保存元素地图。
      </a-alert>
      <a-form :model="manualCaptureForm" layout="vertical">
        <a-form-item field="name" label="地图名称">
          <a-input v-model="manualCaptureForm.name" placeholder="默认使用当前时间生成" :disabled="manualCaptureRunning" />
        </a-form-item>
        <a-form-item field="environment_config" label="环境">
          <a-select
            v-model="manualCaptureForm.environment_config"
            placeholder="可选：选择环境后自动带出 URL"
            allow-clear
            :disabled="manualCaptureRunning"
            @focus="loadEnvConfigs"
            @change="syncManualCaptureBaseUrl"
          >
            <a-option v-for="env in envConfigs" :key="env.id" :value="env.id">
              {{ env.name }}
            </a-option>
          </a-select>
        </a-form-item>
        <a-form-item field="base_url" label="采集入口 URL" required>
          <a-input v-model="manualCaptureForm.base_url" placeholder="https://example.com/" :disabled="manualCaptureRunning" />
        </a-form-item>
        <a-row :gutter="12">
          <a-col :span="12">
            <a-form-item field="interval_ms" label="采集间隔(ms)">
              <a-input-number
                v-model="manualCaptureForm.interval_ms"
                :min="500"
                :max="5000"
                :step="100"
                :disabled="manualCaptureRunning"
              />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item field="max_duration_seconds" label="最长采集(s)">
              <a-input-number
                v-model="manualCaptureForm.max_duration_seconds"
                :min="30"
                :max="1800"
                :step="30"
                :disabled="manualCaptureRunning"
              />
            </a-form-item>
          </a-col>
        </a-row>
      </a-form>
      <div class="modal-actions">
        <a-button :disabled="manualCaptureRunning" @click="manualCaptureVisible = false">取消</a-button>
        <a-button
          type="primary"
          :loading="manualCaptureSubmitting || manualCaptureRunning"
          @click="startManualCapture"
        >
          {{ manualCaptureRunning ? '采集中' : '开始采集' }}
        </a-button>
      </div>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { IconCheck, IconDelete, IconEye, IconRecord, IconRefresh } from '@arco-design/web-vue/es/icon'
import { useProjectStore } from '@/store/projectStore'
import { elementMapApi, envConfigApi } from '../api'
import type { ElementMapStatus, UiElementMap, UiEnvironmentConfig } from '../types'
import { extractPaginationData, extractResponseData } from '../types'

const projectStore = useProjectStore()
const projectId = computed(() => projectStore.currentProject?.id)

const loading = ref(false)
const detailLoading = ref(false)
const versionsLoading = ref(false)
const confirmingId = ref<number | null>(null)
const archivingId = ref<number | null>(null)
const detailVisible = ref(false)
const detailTab = ref('summary')
const maps = ref<UiElementMap[]>([])
const selectedRowKeys = ref<number[]>([])
const currentMap = ref<UiElementMap | null>(null)
const groupVersions = ref<UiElementMap[]>([])
const envConfigs = ref<UiEnvironmentConfig[]>([])
const manualCaptureVisible = ref(false)
const manualCaptureSubmitting = ref(false)
const manualCaptureRunning = ref(false)
const manualCaptureElementMapId = ref<number | null>(null)
let manualCapturePollTimer: ReturnType<typeof setTimeout> | null = null

const filters = reactive({
  status: undefined as ElementMapStatus | undefined,
  stale_status: undefined as string | undefined,
  search: '',
})

const manualCaptureForm = reactive({
  name: '',
  environment_config: undefined as number | undefined,
  base_url: '',
  interval_ms: 1000,
  max_duration_seconds: 600,
})

const pagination = reactive({
  current: 1,
  pageSize: 10,
  total: 0,
  showTotal: true,
  showPageSize: true,
})

const columns = [
  { title: '地图', slotName: 'name', width: 260 },
  { title: '版本', slotName: 'version', width: 90 },
  { title: '状态', slotName: 'status', width: 170 },
  { title: '环境/范围', slotName: 'scope', width: 210 },
  { title: '覆盖', slotName: 'coverage', width: 180 },
  { title: '差异', slotName: 'diff', width: 210 },
  { title: 'Hash', slotName: 'hash', width: 220 },
  { title: '更新时间', slotName: 'updated_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 230, fixed: 'right' },
]

const versionColumns = [
  { title: 'ID', dataIndex: 'id', width: 80 },
  { title: '版本', slotName: 'version', width: 90 },
  { title: '状态', slotName: 'status', width: 170 },
  { title: '差异', slotName: 'diff' },
  { title: '更新时间', slotName: 'updated_at', width: 180 },
  { title: '操作', slotName: 'operations', width: 90 },
]

const asRecord = (value: unknown): Record<string, any> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {}

const mapJson = (map: UiElementMap) => asRecord(map.map_json)
const coverage = (map: UiElementMap) => {
  const summary = asRecord(map.coverage_summary)
  return {
    page_count: Number(summary.page_count ?? map.page_count ?? 0),
    element_count: Number(summary.element_count ?? map.element_count ?? 0),
    low_confidence_count: Number(summary.low_confidence_count ?? map.low_confidence_items?.length ?? 0),
    risk_count: Number(summary.risk_count ?? map.risk_items?.length ?? 0),
    som_element_count: Number(summary.som_element_count ?? 0),
    virtual_list_container_count: Number(summary.virtual_list_container_count ?? 0),
  }
}
const diff = (map: UiElementMap) => {
  const summary = asRecord(map.diff_summary)
  return {
    added_element_count: Number(summary.added_element_count ?? 0),
    removed_element_count: Number(summary.removed_element_count ?? 0),
    locator_changed_count: Number(summary.locator_changed_count ?? 0),
  }
}

const statusLabel = (status?: string) => ({
  draft: '草稿',
  confirmed: '已确认',
  archived: '已归档',
}[status || ''] || status || '-')

const statusColor = (status?: string) => ({
  draft: 'gray',
  confirmed: 'green',
  archived: 'orange',
}[status || ''] || 'gray')

const staleLabel = (status?: string) => ({
  current: '当前',
  superseded: '已过期',
}[status || 'current'] || status || '当前')

const staleColor = (status?: string) => ({
  current: 'arcoblue',
  superseded: 'orangered',
}[status || 'current'] || 'gray')

const shortHash = (hash?: string) => hash ? hash.slice(0, 10) : '-'
const formatTime = (time?: string) => time ? new Date(time).toLocaleString() : '-'
const formatJson = (data: unknown) => JSON.stringify(data || {}, null, 2)

const manualCaptureStatus = (map: UiElementMap) => {
  const capture = asRecord(mapJson(map).manual_capture)
  return String(capture.status || '')
}

const isManualCaptureInProgress = (map: UiElementMap) =>
  mapJson(map).capture_mode === 'manual_capture' && manualCaptureStatus(map) === 'capturing'

const stopManualCapturePolling = () => {
  if (manualCapturePollTimer) {
    clearTimeout(manualCapturePollTimer)
    manualCapturePollTimer = null
  }
}

const pollManualCaptureResult = async (mapId: number) => {
  stopManualCapturePolling()
  try {
    const res = await elementMapApi.get(mapId)
    const map = extractResponseData<UiElementMap>(res)
    const captureStatus = map ? manualCaptureStatus(map) : ''
    if (map && captureStatus === 'completed') {
      manualCaptureRunning.value = false
      manualCaptureElementMapId.value = null
      manualCaptureVisible.value = false
      Message.success('人工采集完成，元素地图已保存')
      await refresh()
      return
    }
    if (map && captureStatus === 'failed') {
      manualCaptureRunning.value = false
      manualCaptureElementMapId.value = null
      Message.error(map.stale_reason || '人工采集失败')
      await refresh()
      return
    }
  } catch (err: any) {
    Message.error(err?.message || '查询人工采集状态失败')
    manualCaptureRunning.value = false
    manualCaptureElementMapId.value = null
    return
  }
  manualCapturePollTimer = setTimeout(() => pollManualCaptureResult(mapId), 2000)
}

const loadEnvConfigs = async () => {
  if (!projectId.value) return
  const res = await envConfigApi.list({ project: projectId.value })
  envConfigs.value = extractPaginationData(res).items as UiEnvironmentConfig[]
}

const syncManualCaptureBaseUrl = () => {
  const env = envConfigs.value.find(item => item.id === manualCaptureForm.environment_config)
  if (env?.base_url) {
    manualCaptureForm.base_url = env.base_url
  }
}

const openManualCapture = async () => {
  stopManualCapturePolling()
  manualCaptureRunning.value = false
  manualCaptureElementMapId.value = null
  manualCaptureForm.name = ''
  manualCaptureForm.environment_config = undefined
  manualCaptureForm.base_url = ''
  manualCaptureForm.interval_ms = 1000
  manualCaptureForm.max_duration_seconds = 600
  await loadEnvConfigs()
  const defaultEnv = envConfigs.value.find(item => item.is_default) || envConfigs.value[0]
  if (defaultEnv) {
    manualCaptureForm.environment_config = defaultEnv.id
    manualCaptureForm.base_url = defaultEnv.base_url || ''
  }
  manualCaptureVisible.value = true
}

const startManualCapture = async () => {
  if (!projectId.value) return
  const baseUrl = manualCaptureForm.base_url.trim()
  if (!baseUrl) {
    Message.warning('请填写采集入口 URL')
    return
  }
  manualCaptureSubmitting.value = true
  try {
    const res = await elementMapApi.startManualCapture({
      project: projectId.value,
      environment_config: manualCaptureForm.environment_config || null,
      base_url: baseUrl,
      name: manualCaptureForm.name.trim() || undefined,
      interval_ms: manualCaptureForm.interval_ms,
      max_duration_seconds: manualCaptureForm.max_duration_seconds,
    })
    const payload = extractResponseData<{ message?: string; element_map?: UiElementMap }>(res)
    const mapId = payload?.element_map?.id
    if (!mapId) {
      throw new Error('人工采集任务缺少元素地图 ID')
    }
    Message.success(payload?.message || '人工采集任务已下发')
    manualCaptureElementMapId.value = mapId
    manualCaptureRunning.value = true
    pollManualCaptureResult(mapId)
  } catch (err: any) {
    Message.error(err?.message || '人工采集任务下发失败')
    manualCaptureRunning.value = false
    manualCaptureElementMapId.value = null
  } finally {
    manualCaptureSubmitting.value = false
  }
}

const refresh = async () => {
  if (!projectId.value) return
  loading.value = true
  try {
    const res = await elementMapApi.list({
      project: projectId.value,
      status: filters.status,
      stale_status: filters.stale_status,
      search: filters.search || undefined,
      page: pagination.current,
      page_size: pagination.pageSize,
    })
    const { items, count } = extractPaginationData(res)
    const visibleItems = (items as UiElementMap[]).filter(map => !isManualCaptureInProgress(map))
    maps.value = visibleItems
    pagination.total = Math.max(0, count - ((items as UiElementMap[]).length - visibleItems.length))
  } catch (err: any) {
    Message.error(err?.message || '加载元素地图失败')
  } finally {
    loading.value = false
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

const loadGroupVersions = async (map: UiElementMap) => {
  if (!projectId.value || !map.version_group) {
    groupVersions.value = []
    return
  }
  versionsLoading.value = true
  try {
    const res = await elementMapApi.list({
      project: projectId.value,
      version_group: map.version_group,
    })
    groupVersions.value = (extractPaginationData(res).items as UiElementMap[])
      .sort((left, right) => Number(right.version || 0) - Number(left.version || 0))
  } catch (err: any) {
    Message.error(err?.message || '加载版本历史失败')
  } finally {
    versionsLoading.value = false
  }
}

const openDetail = async (map: UiElementMap) => {
  detailVisible.value = true
  detailLoading.value = true
  detailTab.value = 'summary'
  try {
    const res = await elementMapApi.get(map.id)
    currentMap.value = extractResponseData<UiElementMap>(res) || map
    await loadGroupVersions(currentMap.value)
  } catch (err: any) {
    Message.error(err?.message || '加载元素地图详情失败')
    currentMap.value = map
  } finally {
    detailLoading.value = false
  }
}

const confirmMap = async (map: UiElementMap) => {
  confirmingId.value = map.id
  try {
    const res = await elementMapApi.confirm(map.id)
    const updated = extractResponseData<UiElementMap>(res)
    Message.success('元素地图已确认为当前版本')
    if (currentMap.value?.id === map.id && updated) {
      currentMap.value = updated
      await loadGroupVersions(updated)
    }
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '确认元素地图失败')
  } finally {
    confirmingId.value = null
  }
}

const archiveMap = async (map: UiElementMap) => {
  archivingId.value = map.id
  try {
    const res = await elementMapApi.update(map.id, { status: 'archived' })
    const updated = extractResponseData<UiElementMap>(res)
    Message.success('元素地图版本已归档')
    if (currentMap.value?.id === map.id && updated) {
      currentMap.value = updated
      await loadGroupVersions(updated)
    }
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '归档元素地图失败')
  } finally {
    archivingId.value = null
  }
}

const batchDeleteMaps = async () => {
  if (selectedRowKeys.value.length === 0) {
    Message.warning('请选择要删除的元素地图')
    return
  }

  const deletingIds = [...selectedRowKeys.value]
  try {
    const res = await elementMapApi.batchDelete(deletingIds)
    const result = extractResponseData<{ message?: string }>(res)
    Message.success(result?.message || `成功删除 ${deletingIds.length} 个元素地图`)
    selectedRowKeys.value = []
    if (currentMap.value && deletingIds.includes(currentMap.value.id)) {
      detailVisible.value = false
      currentMap.value = null
      groupVersions.value = []
    }
    await refresh()
  } catch (err: any) {
    Message.error(err?.message || '批量删除元素地图失败')
  }
}

onMounted(refresh)
onBeforeUnmount(stopManualCapturePolling)

defineExpose({ refresh })
</script>

<style scoped>
.element-map-version-list {
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

.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.name-cell,
.scope-cell,
.hash-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  line-height: 1.35;
}

.coverage-cell,
.diff-cell {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--color-text-2);
  font-size: 12px;
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

.section-title {
  margin: 14px 0 8px;
  font-weight: 600;
  color: var(--color-text-1);
}

.json-block {
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

.manual-capture-alert {
  margin-bottom: 12px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}
</style>
