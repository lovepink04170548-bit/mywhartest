<template>
  <div class="actuator-onboarding">
    <div class="header">
      <div class="title">
        <h3>执行器接入</h3>
        <span class="count">平台下载页与安装指引</span>
      </div>
      <div class="actions">
        <a-button @click="loadGuide" :loading="loading">
          <template #icon><icon-refresh /></template>
          刷新
        </a-button>
        <a-button @click="copyConfigTemplate" :disabled="!guide?.config_template">
          <template #icon><icon-copy /></template>
          复制配置模板
        </a-button>
        <a-button @click="copyRegistrationToken" :disabled="!guide?.registration_token">
          <template #icon><icon-link /></template>
          复制接入令牌
        </a-button>
      </div>
    </div>

    <a-alert type="info" class="mb-4">
      <template #title>接入方式</template>
      平台在此页提供执行器下载包、配置模板和接入参数。用户下载安装到本机后，修改 config.toml 并启动执行器即可连接平台。
    </a-alert>

    <div class="section">
      <div class="section-head">
        <h4>下载安装包</h4>
        <span class="section-note">按操作系统选择对应版本</span>
      </div>
      <div class="package-list">
        <div v-for="item in packages" :key="item.platform" class="package-row">
          <div class="package-meta">
            <div class="package-name">{{ platformLabel(item.platform) }}</div>
            <div class="package-sub">文件：{{ item.file_name }}</div>
          </div>
          <div class="package-actions">
            <a-button
              type="primary"
              @click="downloadPackage(item)"
              :disabled="!item.available"
            >
              <template #icon><icon-download /></template>
              下载
            </a-button>
            <span class="sha-text">{{ item.sha256 ? `SHA256: ${item.sha256}` : '未配置校验值' }}</span>
          </div>
        </div>
      </div>
      <div class="section-note mt-2">
        Windows 和 macOS 均提供一键安装包；只有检测到真实发布包或配置了外部下载地址时，下载按钮才会启用。
      </div>
    </div>

    <div class="section">
      <div class="section-head">
        <h4>接入参数</h4>
        <span class="section-note">用户启动执行器前需要确认这些值</span>
      </div>
      <a-descriptions :column="1" bordered size="small">
        <a-descriptions-item label="API 地址">{{ guide?.api_url || '-' }}</a-descriptions-item>
        <a-descriptions-item label="WebSocket 地址">{{ guide?.ws_url || '-' }}</a-descriptions-item>
        <a-descriptions-item label="注册令牌">{{ guide?.registration_token || '-' }}</a-descriptions-item>
        <a-descriptions-item label="心跳间隔">{{ guide?.heartbeat_interval_seconds || 0 }} 秒</a-descriptions-item>
        <a-descriptions-item label="在线过期">{{ guide?.online_ttl_seconds || 0 }} 秒</a-descriptions-item>
        <a-descriptions-item label="令牌要求">
          {{ guide?.registration_token_required ? '必须配置' : '可选' }}
        </a-descriptions-item>
      </a-descriptions>
    </div>

    <div class="section">
      <div class="section-head">
        <h4>安装步骤</h4>
        <span class="section-note">用户按顺序完成即可接入</span>
      </div>
      <div class="step-list">
        <div v-for="(step, index) in guide?.install_steps || []" :key="step.title" class="step-row">
          <div class="step-index">{{ index + 1 }}</div>
          <div class="step-body">
            <div class="step-title">{{ step.title }}</div>
            <div class="step-content">{{ step.content }}</div>
          </div>
        </div>
      </div>
      <div v-if="guide?.notes?.length" class="note-list">
        <div v-for="note in guide.notes" :key="note" class="note-item">{{ note }}</div>
      </div>
    </div>

    <div class="section">
      <div class="section-head">
        <h4>配置模板</h4>
        <a-button size="mini" @click="copyConfigTemplate" :disabled="!guide?.config_template">
          <template #icon><icon-copy /></template>
          复制
        </a-button>
      </div>
      <pre class="config-block">{{ guide?.config_template || '暂无配置模板' }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { IconCopy, IconDownload, IconLink, IconRefresh } from '@arco-design/web-vue/es/icon'
import { actuatorApi, type ActuatorOnboardingInfo, type ActuatorPackageInfo } from '../api'
import { extractResponseData } from '../types'

void [IconCopy, IconDownload, IconLink, IconRefresh]

const loading = ref(false)
const guide = ref<ActuatorOnboardingInfo | null>(null)
const packages = ref<ActuatorPackageInfo[]>([])

const loadGuide = async () => {
  loading.value = true
  try {
    const res = await actuatorApi.onboarding()
    const data = extractResponseData<ActuatorOnboardingInfo>(res)
    guide.value = data || null
    packages.value = data?.packages || []
  } catch (error) {
    console.error('Load actuator onboarding error:', error)
    guide.value = null
    packages.value = []
  } finally {
    loading.value = false
  }
}

const platformLabel = (platform: string) => {
  const labels: Record<string, string> = {
    windows: 'Windows 安装包',
    linux: 'Linux 安装包',
    macos: 'macOS 安装包',
  }
  return labels[platform] || platform
}

const downloadPackage = (item: ActuatorPackageInfo) => {
  if (!item.available || !item.url) {
    Message.warning('当前环境未配置该平台的下载地址')
    return
  }
  window.open(item.url, '_blank', 'noopener,noreferrer')
}

const copyConfigTemplate = async () => {
  if (!guide.value?.config_template) return
  try {
    await navigator.clipboard.writeText(guide.value.config_template)
    Message.success('配置模板已复制')
  } catch (error) {
    console.error('Copy config template error:', error)
    Message.error('复制失败，请手动选择文本')
  }
}

const copyRegistrationToken = async () => {
  if (!guide.value?.registration_token) return
  try {
    await navigator.clipboard.writeText(guide.value.registration_token)
    Message.success('接入令牌已复制')
  } catch (error) {
    console.error('Copy registration token error:', error)
    Message.error('复制失败，请手动选择文本')
  }
}

defineExpose({ refresh: loadGuide })

onMounted(() => {
  loadGuide()
})
</script>

<style scoped lang="scss">
.actuator-onboarding {
  padding: 16px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}

.title {
  display: flex;
  align-items: center;
  gap: 12px;

  h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
  }

  .count {
    color: var(--color-text-3);
    font-size: 14px;
  }
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.mb-4 {
  margin-bottom: 16px;
}

.section {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border-2);
}

.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;

  h4 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
}

.section-note {
  font-size: 12px;
  color: var(--color-text-3);
}

.package-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.package-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border-1);
}

.package-meta {
  min-width: 0;
}

.package-name {
  font-weight: 600;
}

.package-sub,
.sha-text {
  font-size: 12px;
  color: var(--color-text-3);
  word-break: break-all;
}

.package-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.step-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.step-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.step-index {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--color-fill-2);
  color: var(--color-text-1);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  flex: 0 0 auto;
}

.step-body {
  flex: 1;
}

.step-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
}

.step-content {
  font-size: 12px;
  color: var(--color-text-3);
  line-height: 1.6;
}

.note-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.note-item {
  font-size: 12px;
  color: var(--color-text-3);
}

.config-block {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--color-border-2);
  border-radius: 4px;
  background: var(--color-fill-1);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.6;
}
</style>
