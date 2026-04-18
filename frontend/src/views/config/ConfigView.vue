<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">系统配置</div>
      <div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
    </div>

    <div v-loading="loading" class="config-container">
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
                <el-input-number
                  v-else-if="row.value_type === 'int'"
                  v-model="row._draft"
                  :min="0"
                  style="width: 220px"
                />
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
import { fetchConfig, updateConfig } from '@/api/config'
import { DANGEROUS_CONFIG_KEYS } from '@/utils/constants'

const loading = ref(false)
const groups = ref({})
const activeGroups = ref([])

const grouped = computed(() => groups.value)

function editorFor(_row) {
  return ElSwitch
}

function isDangerous(row) {
  if (typeof row.danger === 'boolean') return row.danger
  return DANGEROUS_CONFIG_KEYS.includes(row.config_key)
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
  } finally {
    loading.value = false
  }
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
</style>
