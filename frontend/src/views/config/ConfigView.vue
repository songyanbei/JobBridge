<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">系统配置</div>
      <div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <div v-loading="loading" class="config-container">
      <el-card v-if="policy" class="visibility-card" shadow="never">
        <template #header>
          <div class="visibility-header">
            <span>推荐可见字段（revision {{ policy.revision }}）</span>
            <div>
              <el-button size="small" @click="loadPolicy">刷新策略</el-button>
              <el-button size="small" type="primary" :loading="policySaving" @click="savePolicy">保存策略</el-button>
            </div>
          </div>
        </template>
        <el-alert title="字段权限由后端注册表和安全上限校验；电话、联系人、地址属于高敏字段。" type="info" :closable="false" />
        <el-table :data="policyRows" border size="small" style="margin-top: 12px">
          <el-table-column prop="scene" label="场景" width="150" />
          <el-table-column prop="role" label="角色" width="100" />
          <el-table-column label="字段">
            <template #default="{ row }">
              <el-checkbox-group v-model="row.fields">
                <el-checkbox
                  v-for="field in row.options"
                  :key="field.key"
                  :label="field.key"
                  :disabled="row.fixed"
                >
                  {{ field.label }}<el-tag v-if="field.sensitive" type="warning" size="small">高敏</el-tag>
                </el-checkbox>
              </el-checkbox-group>
            </template>
          </el-table-column>
        </el-table>
        <el-checkbox v-model="confirmSensitive" style="margin-top: 12px">我确认本次新增高敏字段会立即影响推荐展示</el-checkbox>
        <div class="history-row">
          <span>历史版本（审计保留 {{ policy.audit_retention_days }} 天）</span>
          <el-select v-model="restoreRevision" placeholder="选择版本" size="small" style="width: 180px">
            <el-option v-for="item in history" :key="item.revision" :label="`revision ${item.revision}（剩余 ${item.remaining_recovery_days} 天）`" :value="item.revision" :disabled="!item.recoverable" />
          </el-select>
          <el-button size="small" :disabled="!restoreRevision" @click="viewPolicyHistory">查看详情</el-button>
          <el-button size="small" :disabled="!restoreRevision" @click="restorePolicy">以此版本恢复</el-button>
        </div>
      </el-card>
      <el-collapse v-model="activeGroups">
        <el-collapse-item
          v-for="(items, ns) in grouped"
          :key="ns"
          :name="ns"
          :title="ns"
        >
          <el-table :data="items" border size="small">
            <el-table-column prop="config_key" label="Key" width="280">
              <template #default="{ row }">
                <span>{{ row.config_key }}</span>
                <el-tag
                  v-if="isDangerous(row)"
                  type="danger"
                  size="small"
                  style="margin-left: 6px"
                >
                  危险
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" show-overflow-tooltip />
            <el-table-column label="值">
              <template #default="{ row }">
                <component
                  :is="editorFor(row)"
                  v-if="row.value_type === 'bool'"
                  v-model="row._draft"
                />
                <div v-else-if="row.value_type === 'int'" class="number-editor">
                  <el-input-number
                    v-model="row._draft"
                    :min="numberRange(row).min"
                    :max="numberRange(row).max"
                    style="width: 220px"
                  />
                  <span v-if="isJobTtl(row)" class="unit">天</span>
                  <div v-if="isJobTtl(row)" class="ttl-hint">
                    {{ ttlHint(row) }}
                  </div>
                </div>
                <JsonEditor
                  v-else-if="row.value_type === 'json'"
                  v-model="row._draft"
                  :rows="3"
                  @valid-change="(ok) => (row._valid = ok)"
                />
                <el-input
                  v-else
                  v-model="row._draft"
                />
              </template>
            </el-table-column>
            <el-table-column prop="value_type" label="类型" width="80" />
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="!isDirty(row) || (row.value_type === 'json' && row._valid === false)"
                  :loading="row._saving"
                  @click="onSave(row)"
                >
                  保存
                </el-button>
                <el-button
                  v-if="isDirty(row)"
                  link
                  size="small"
                  @click="onReset(row)"
                >
                  取消
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox, ElSwitch } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import JsonEditor from '@/components/JsonEditor.vue'
import {
  fetchConfig, updateConfig, fetchVisibilityPolicy, saveVisibilityPolicy,
  fetchVisibilityPolicyHistory, fetchVisibilityPolicyHistoryDetail, restoreVisibilityPolicy,
} from '@/api/config'
import { DANGEROUS_CONFIG_KEYS } from '@/utils/constants'

const loading = ref(false)
const groups = ref({})
const activeGroups = ref([])
const policy = ref(null)
const policyRows = ref([])
const history = ref([])
const restoreRevision = ref(null)
const confirmSensitive = ref(false)
const policySaving = ref(false)

const grouped = computed(() => groups.value)

function editorFor(_row) {
  return ElSwitch
}

function isDangerous(row) {
  if (typeof row.danger === 'boolean') return row.danger
  return DANGEROUS_CONFIG_KEYS.includes(row.config_key)
}

function numberRange(row) {
  if (row.config_key === 'ttl.job.days') return { min: 1, max: 3650 }
  if (row.config_key === 'ttl.job.candidate.days') return { min: 1, max: 365 }
  return { min: 0, max: undefined }
}

function isJobTtl(row) {
  return row.config_key === 'ttl.job.days' || row.config_key === 'ttl.job.candidate.days'
}

function ttlHint(row) {
  if (row.config_key === 'ttl.job.candidate.days') {
    return '适用于首次发布和全量更新候选，仅影响后续创建的候选。'
  }
  return '仅影响后续激活的岗位，不追溯修改已有岗位。'
}

function normalize(row) {
  const v = row.config_value
  if (row.value_type === 'bool') {
    row._draft = v === true || v === 'true' || v === 1 || v === '1'
  } else if (row.value_type === 'int') {
    row._draft = Number(v)
  } else {
    row._draft = v === null || v === undefined ? '' : String(v)
  }
  row._valid = true
  row._saving = false
  row._original = row._draft
}

function isDirty(row) {
  return row._draft !== row._original
}

async function load() {
  loading.value = true
  try {
    const data = await fetchConfig()
    const next = {}
    for (const [ns, items] of Object.entries(data || {})) {
      next[ns] = (items || []).map((it) => {
        const copy = { ...it }
        normalize(copy)
        return copy
      })
    }
    groups.value = next
    if (!activeGroups.value.length) activeGroups.value = Object.keys(next)
    await loadPolicy()
  } finally {
    loading.value = false
  }
}

async function loadPolicy() {
  const [current, revisions] = await Promise.all([
    fetchVisibilityPolicy(), fetchVisibilityPolicyHistory(),
  ])
  policy.value = current
  history.value = revisions || []
  policyRows.value = []
  for (const [scene, roles] of Object.entries(current.matrix || {})) {
    if (scene === 'schema_version' || scene === 'revision') continue
    for (const [role, fields] of Object.entries(roles || {})) {
      policyRows.value.push({
        scene, role, fields: [...fields], options: current.fields?.[scene]?.[role] || [],
        fixed: scene === 'job_search' && role === 'worker',
      })
    }
  }
}

function policyPayload() {
  const next = {}
  for (const row of policyRows.value) {
    next[row.scene] ||= {}
    next[row.scene][row.role] = [...row.fields]
  }
  return next
}

async function viewPolicyHistory() {
  if (!restoreRevision.value) return
  const detail = await fetchVisibilityPolicyHistoryDetail(restoreRevision.value)
  await ElMessageBox.alert(
    JSON.stringify(detail.config_value, null, 2),
    `revision ${detail.revision} · 剩余 ${detail.remaining_recovery_days} 天`,
    { confirmButtonText: '关闭' },
  )
}

async function savePolicy() {
  policySaving.value = true
  try {
    const result = await saveVisibilityPolicy({
      policy: policyPayload(), expected_revision: policy.value.revision,
      confirm_sensitive_expansion: confirmSensitive.value,
    })
    ElMessage.success('推荐字段策略保存成功')
    confirmSensitive.value = false
    policy.value = { ...policy.value, ...result, matrix: result.matrix }
    await loadPolicy()
  } finally {
    policySaving.value = false
  }
}

async function restorePolicy() {
  if (!restoreRevision.value) return
  await ElMessageBox.confirm(`确认恢复 revision ${restoreRevision.value}？`, '恢复策略', { type: 'warning' })
  await restoreVisibilityPolicy(restoreRevision.value, {
    expected_revision: policy.value.revision,
    confirm_sensitive_expansion: confirmSensitive.value,
  })
  ElMessage.success('策略已恢复')
  restoreRevision.value = null
  confirmSensitive.value = false
  await loadPolicy()
}

function onReset(row) {
  row._draft = row._original
}

async function onSave(row) {
  if (!isDirty(row)) return
  if (row.value_type === 'json' && row._valid === false) {
    ElMessage.error('JSON 格式错误，无法保存')
    return
  }
  if (isDangerous(row)) {
    try {
      await ElMessageBox.confirm(
        `该配置项「${row.config_key}」属于危险项，保存后将立即影响线上业务。确认修改？`,
        '危险操作确认',
        {
          confirmButtonText: '确认修改',
          cancelButtonText: '取消',
          type: 'warning',
        },
      )
    } catch (_e) {
      return
    }
  }
  row._saving = true
  try {
    let value = row._draft
    if (row.value_type === 'bool') value = value ? 'true' : 'false'
    else if (row.value_type === 'int') value = String(value)
    const resp = await updateConfig(row.config_key, { config_value: value })
    ElMessage.success(resp?.notice || '保存成功')
    row._original = row._draft
    if (typeof resp?.danger === 'boolean') row.danger = resp.danger
  } finally {
    row._saving = false
  }
}

load()
</script>

<style scoped>
.config-container {
  padding-bottom: 20px;
}
.number-editor .unit {
  margin-left: 8px;
  color: var(--el-text-color-regular);
}
.ttl-hint {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 18px;
}
.visibility-card {
  margin-bottom: 18px;
}
.visibility-header,
.history-row {
  display: flex;
  align-items: center;
  gap: 12px;
  justify-content: space-between;
}
.history-row {
  justify-content: flex-start;
  margin-top: 16px;
  color: var(--el-text-color-secondary);
}
.el-checkbox {
  margin-right: 12px;
}
</style>
