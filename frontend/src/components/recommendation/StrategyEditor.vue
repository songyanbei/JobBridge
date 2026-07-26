<template>
  <div class="strategy-editor">
    <div class="editor-head">
      <div class="editor-head-main">
        <!-- 用 :model-value 而不是 v-model：模板切换要先确认再落到表单，
             取消时 el-select 会自动回滚到未改动的值。 -->
        <el-select
          :model-value="form.template_key"
          :disabled="!canEdit"
          style="width: 200px"
          @change="onTemplateChange"
        >
          <el-option
            v-for="tpl in STRATEGY_TEMPLATES"
            :key="tpl.key"
            :label="tpl.label"
            :value="tpl.key"
          />
        </el-select>
        <el-tag :type="isCustom ? 'warning' : 'success'" size="small">{{ strategyLabel }}</el-tag>
        <span class="jb-muted template-desc">{{ templateDescription }}</span>
      </div>
      <div class="editor-head-actions">
        <el-button :disabled="!canEdit" @click="onRestoreTemplateDefaults">恢复模板默认值</el-button>
        <el-button v-if="serverDraft" :disabled="!canEdit || !dirty" @click="onDiscardLocal">
          放弃未保存修改
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="!canEdit"
      type="info"
      :closable="false"
      show-icon
      title="当前账号为只读角色，仅可查看参数与运行模拟，不能保存草稿。"
      class="editor-alert"
    />

    <el-alert
      v-else-if="serverDraft"
      :type="draftNeedsSimulation ? 'warning' : 'success'"
      :closable="false"
      show-icon
      class="editor-alert"
      :title="draftStatusTitle"
    >
      <template #default>
        <div>
          草稿版本 v{{ serverDraft.version_no }}（id={{ serverDraft.id }}）· 参数指纹
          <span class="mono">{{ shortDigest(serverDraft.parameters_digest) }}</span>
          · 最后模拟指纹
          <span class="mono">{{ shortDigest(serverDraft.last_simulated_digest) || '无' }}</span>
        </div>
        <div>
          发布前置校验要求「最后模拟指纹 = 当前参数指纹」。参数每改动一次并保存，指纹都会变化，必须重新模拟一次才能发布。
        </div>
      </template>
    </el-alert>

    <el-alert
      v-else
      type="info"
      :closable="false"
      show-icon
      class="editor-alert"
      title="当前方向还没有草稿版本，保存后会创建一个新草稿；保存只生成草稿，不影响线上流量。"
    />

    <div class="weight-bar" :class="{ invalid: !weightValid }">
      <span>四项权重合计</span>
      <span class="weight-sum mono">{{ currentWeightSum }}</span>
      <span>/ 100</span>
      <el-tag :type="weightValid ? 'success' : 'danger'" size="small">
        {{ weightValid ? '校验通过' : `与 100 相差 ${currentWeightSum - 100 > 0 ? '+' : ''}${currentWeightSum - 100}` }}
      </el-tag>
      <span v-if="!weightValid" class="jb-danger-text">
        匹配质量 + 信息质量 + 新鲜度 + 曝光机会必须等于 100，否则不能保存草稿，也不能发布。
      </span>
    </div>

    <el-table :data="STRATEGY_PARAM_META" border size="small" class="param-table">
      <el-table-column label="参数" width="150">
        <template #default="{ row }">
          <div>{{ row.label }}</div>
          <div class="mono param-key">{{ row.key }}</div>
        </template>
      </el-table-column>
      <el-table-column label="取值" width="220">
        <template #default="{ row }">
          <el-select
            v-if="row.type === 'enum'"
            v-model="form.parameters[row.key]"
            :disabled="!canEdit"
            style="width: 160px"
          >
            <el-option
              v-for="opt in row.options"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div v-else class="number-cell">
            <el-input-number
              v-model="form.parameters[row.key]"
              :min="row.min"
              :max="row.max"
              :step="1"
              :disabled="!canEdit"
              controls-position="right"
              style="width: 150px"
            />
            <span v-if="row.unit" class="jb-muted unit">{{ row.unit }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="允许范围" width="130">
        <template #default="{ row }">
          <span class="mono">{{ rangeText(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="含义" min-width="200">
        <template #default="{ row }">{{ row.desc }}</template>
      </el-table-column>
      <el-table-column label="风险提示" min-width="240">
        <template #default="{ row }">
          <span class="jb-warning-text">{{ row.risk }}</span>
        </template>
      </el-table-column>
      <el-table-column label="模板默认" width="100">
        <template #default="{ row }">
          <span class="mono">{{ displayValue(row, templateDefaults[row.key]) }}</span>
        </template>
      </el-table-column>
    </el-table>

    <el-form label-position="top" class="reason-form">
      <el-form-item label="修改原因（必填，会写入审计日志与版本记录）">
        <el-input
          v-model="form.change_reason"
          type="textarea"
          :rows="2"
          :maxlength="255"
          show-word-limit
          :disabled="!canEdit"
          placeholder="例如：提高曝光机会权重，缓解头部企业集中"
        />
      </el-form-item>
    </el-form>

    <div class="editor-foot">
      <el-button
        type="primary"
        :loading="saving"
        :disabled="!canSave"
        @click="onSave"
      >
        保存草稿
      </el-button>
      <span v-if="canEdit && !weightValid" class="jb-danger-text">权重合计不等于 100，无法保存。</span>
      <span v-else-if="canEdit && !form.change_reason.trim()" class="jb-muted">请先填写修改原因。</span>
      <span v-else-if="canEdit && serverDraft && !dirty" class="jb-muted">参数与已保存草稿一致，无需保存。</span>
      <span v-else-if="canEdit" class="jb-muted">保存后需要重新运行一次模拟，才能发布为候选版本。</span>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  STRATEGY_PARAM_META,
  STRATEGY_TEMPLATES,
  createRecommendationDraft,
  normalizeParameters,
  sameParameters,
  templateByKey,
  templateLabel,
  templateParameters,
  updateRecommendationDraft,
  weightSum,
} from '@/api/recommendationStrategies'

const props = defineProps({
  direction: { type: String, required: true },
  versions: { type: Array, default: () => [] },
  release: { type: Object, default: null },
  canEdit: { type: Boolean, default: false },
})

const emit = defineEmits(['saved', 'failed', 'state-change'])

const saving = ref(false)
const form = reactive({
  template_key: 'balanced',
  parameters: templateParameters('balanced'),
  change_reason: '',
})

/** 服务端当前草稿；后端每个方向最多保留一个 draft 状态版本。 */
const serverDraft = computed(
  () => props.versions.find((item) => item.status === 'draft') || null,
)
const stableVersion = computed(() => {
  const id = props.release?.stable_version_id
  if (!id) return null
  return props.versions.find((item) => item.id === id) || null
})

/** 本地表单的"基线"：有草稿就是草稿，否则是稳定版本/默认模板。 */
const baseline = computed(() => {
  const source = serverDraft.value || stableVersion.value
  if (source) {
    return {
      template_key: source.template_key || 'balanced',
      parameters: normalizeParameters(source.parameters),
    }
  }
  return { template_key: 'balanced', parameters: templateParameters('balanced') }
})

const templateDefaults = computed(() => templateParameters(form.template_key))
const templateDescription = computed(() => templateByKey(form.template_key)?.description || '')
const isCustom = computed(() => !sameParameters(form.parameters, templateDefaults.value))
const strategyLabel = computed(() =>
  isCustom.value
    ? `基于「${templateLabel(form.template_key)}」的自定义策略`
    : `官方模板「${templateLabel(form.template_key)}」`,
)

const currentWeightSum = computed(() => weightSum(form.parameters))
const weightValid = computed(() => currentWeightSum.value === 100)
const dirty = computed(
  () =>
    form.template_key !== baseline.value.template_key ||
    !sameParameters(form.parameters, baseline.value.parameters),
)
const draftNeedsSimulation = computed(() => {
  const draft = serverDraft.value
  if (!draft) return false
  return !draft.last_simulated_digest || draft.last_simulated_digest !== draft.parameters_digest
})
const draftStatusTitle = computed(() =>
  draftNeedsSimulation.value
    ? '当前草稿尚未针对最新参数完成模拟，暂时不能发布为候选版本'
    : '当前草稿已完成模拟，可以发布为候选版本',
)
// 还没有草稿时，第一次保存本身就是有意义的动作（哪怕参数就是模板默认值），
// 所以不要求 dirty；已有草稿时才要求确实改过东西。
const canSave = computed(
  () =>
    props.canEdit &&
    weightValid.value &&
    !!form.change_reason.trim() &&
    (dirty.value || !serverDraft.value),
)

// 父组件用它来禁用发布：草稿有未保存修改，或权重合计不等于 100 时都不能发布。
watch(
  [dirty, weightValid, currentWeightSum],
  () =>
    emit('state-change', {
      dirty: dirty.value,
      weightValid: weightValid.value,
      weightSum: currentWeightSum.value,
    }),
  { immediate: true },
)

/**
 * 只在服务端草稿"换了一版"时回填表单，避免父组件每次轮询刷新都冲掉正在编辑的值。
 */
const syncKey = computed(() => {
  const draft = serverDraft.value
  if (draft) return `draft:${draft.id}:${draft.lock_version}:${draft.parameters_digest}`
  return `stable:${props.direction}:${props.release?.stable_version_id || 0}`
})

watch(
  syncKey,
  () => {
    form.template_key = baseline.value.template_key
    form.parameters = { ...baseline.value.parameters }
    form.change_reason = ''
  },
  { immediate: true },
)

function rangeText(meta) {
  if (meta.type === 'enum') return meta.options.map((o) => o.value).join(' / ')
  return `${meta.min} ~ ${meta.max}${meta.unit || ''}`
}

function displayValue(meta, value) {
  if (value === null || value === undefined) return '—'
  if (meta.type === 'enum') {
    return meta.options.find((o) => o.value === value)?.label || value
  }
  return `${value}${meta.unit || ''}`
}

function shortDigest(digest) {
  if (!digest) return ''
  return String(digest).slice(0, 12)
}

async function confirmOverwrite(message) {
  // 有草稿时即便本地还没改动，套模板也会覆盖草稿里已保存的参数，同样要确认。
  if (!dirty.value && !serverDraft.value) return true
  try {
    await ElMessageBox.confirm(message, '确认覆盖当前草稿参数', {
      confirmButtonText: '确认覆盖',
      cancelButtonText: '取消',
      type: 'warning',
    })
    return true
  } catch (_e) {
    return false
  }
}

async function onTemplateChange(nextKey) {
  if (nextKey === form.template_key) return
  const ok = await confirmOverwrite(
    `切换到「${templateLabel(nextKey)}」会用该模板的默认值覆盖当前未保存的参数，确认切换？`,
  )
  if (!ok) return
  form.template_key = nextKey
  form.parameters = templateParameters(nextKey)
}

async function onRestoreTemplateDefaults() {
  const ok = await confirmOverwrite(
    `将用「${templateLabel(form.template_key)}」的官方默认值覆盖当前未保存的参数，确认恢复？`,
  )
  if (!ok) return
  form.parameters = templateParameters(form.template_key)
}

function onDiscardLocal() {
  form.template_key = baseline.value.template_key
  form.parameters = { ...baseline.value.parameters }
}

async function onSave() {
  if (!canSave.value) return
  const payload = {
    template_key: form.template_key,
    parameters: { ...form.parameters },
    change_reason: form.change_reason.trim(),
  }
  saving.value = true
  try {
    if (serverDraft.value) {
      await updateRecommendationDraft(serverDraft.value.id, {
        ...payload,
        lock_version: serverDraft.value.lock_version,
      })
    } else {
      // 新建草稿时后端不校验 lock_version，但 schema 要求 >= 1。
      await createRecommendationDraft(props.direction, { ...payload, lock_version: 1 })
    }
    ElMessage.success('草稿已保存，请重新运行模拟后再发布')
    emit('saved')
  } catch (err) {
    emit('failed', { err, fallback: '保存草稿失败' })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.strategy-editor {
  display: grid;
  gap: 14px;
}
.editor-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.editor-head-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.editor-head-actions {
  display: flex;
  gap: 8px;
}
.template-desc {
  font-size: 12.5px;
}
.editor-alert :deep(.el-alert__content) {
  line-height: 1.7;
}
.weight-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-light);
}
.weight-bar.invalid {
  border-color: var(--el-color-danger-light-5);
  background: var(--el-color-danger-light-9);
}
.weight-sum {
  font-size: 18px;
  font-weight: 600;
}
.param-key {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.number-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}
.unit {
  font-size: 12px;
}
.reason-form {
  max-width: 720px;
}
.reason-form :deep(.el-form-item) {
  margin-bottom: 0;
}
.editor-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
