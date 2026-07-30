<template>
  <div class="strategy-simulation">
    <el-alert type="info" :closable="false" show-icon class="sim-alert">
      <template #title>模拟复用线上精排 LLM 与确定性排序流水线，但不产生线上服务副作用</template>
      <template #default>
        <div>
          同一次 LLM 语义排序结果会同时用于稳定版本和草稿版本，避免模型采样差异干扰参数对比；LLM 调用失败时本次模拟不会解锁发布。
        </div>
        <div>
          模拟不写搜索快照、不修改 shown_items、不写曝光、不改灰度分桶、不写对话日志、不发企微消息；唯一写入是发布前置校验需要的「最后模拟指纹」。
        </div>
      </template>
    </el-alert>

    <el-alert
      v-if="!draft"
      type="warning"
      :closable="false"
      show-icon
      class="sim-alert"
      title="当前方向还没有草稿版本，请先在「策略配置」页保存一份草稿再运行模拟。"
    />
    <el-alert
      v-else-if="needsSimulation"
      type="warning"
      :closable="false"
      show-icon
      class="sim-alert"
      title="草稿参数在上次模拟后被修改过，必须重新模拟一次，发布前置校验才会通过。"
    />

    <el-form :model="form" label-width="120px" class="sim-form">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="推荐方向">
            <el-tag>{{ directionLabel }}</el-tag>
            <span class="jb-muted draft-hint">
              草稿版本：{{ draft ? `v${draft.version_no}（id=${draft.id}）` : '—' }}
            </span>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="用户 ID">
            <el-input
              v-model="form.user_id"
              placeholder="可选，留空使用固定测试用户 simulation"
              clearable
            />
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="原始查询文本">
            <el-input v-model="form.raw_query" placeholder="可选，仅用于记录本次模拟输入" clearable />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="salaryLabel">
            <el-input-number v-model="form.salary" :min="0" :step="500" style="width: 200px" />
            <span class="jb-muted draft-hint">元 / 月，留空或 0 表示不限</span>
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="城市">
            <el-select
              v-model="form.city"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              placeholder="输入城市名后回车"
              style="width: 100%"
            />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="工种">
            <el-select
              v-model="form.job_category"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              placeholder="输入工种名后回车"
              style="width: 100%"
            />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item>
        <el-button type="primary" :loading="running" :disabled="!canRun" @click="onRun">
          运行模拟
        </el-button>
        <span v-if="!criteriaValid" class="jb-danger-text hint">
          城市和工种至少填写一个，否则后端硬过滤守卫会直接返回空候选。
        </span>
      </el-form-item>
    </el-form>

    <template v-if="result">
      <div class="result-meta">
        <el-tag size="small" type="info">候选池 {{ result.candidate_count ?? 0 }} 条</el-tag>
        <el-tag size="small" :type="result.current_basis === 'stable' ? 'success' : 'warning'">
          线上对照：{{ result.current_basis === 'stable' ? '稳定版本' : 'legacy（尚无稳定版本）' }}
        </el-tag>
        <el-tag size="small" :type="result.exposure_available ? 'success' : 'danger'">
          曝光数据：{{ result.exposure_available ? '已读取真实曝光' : '不可用，曝光分与重复系数按中性值处理' }}
        </el-tag>
        <el-tag size="small" type="info">轮换日 {{ result.rotation_date || '—' }}</el-tag>
        <el-tag size="small" type="info">写入副作用：{{ result.side_effects_written ? '有' : '无' }}</el-tag>
        <el-tag size="small" type="info">LLM 调用：{{ result.llm_invoked ? '有' : '无' }}</el-tag>
        <el-tag size="small" :type="result.simulation_mode === 'llm' ? 'success' : 'warning'">
          模拟模式：{{ result.simulation_mode || '—' }}
        </el-tag>
        <el-tag v-if="result.llm_invoked" size="small" type="info">
          Token：{{ result.llm_input_tokens ?? '—' }} / {{ result.llm_output_tokens ?? '—' }}
        </el-tag>
      </div>

      <el-row :gutter="16" class="compare-row">
        <el-col :span="12">
          <div class="compare-head">
            当前线上策略
            <span class="jb-muted">
              （{{ result.current_basis === 'stable' ? '稳定版本' : 'legacy 线上重排' }}）
            </span>
          </div>
          <el-empty v-if="!currentItems.length" description="无结果" :image-size="60" />
          <StrategyCandidateCard
            v-for="item in currentItems"
            :key="`cur-${item.target_id}`"
            :item="item"
            :summary="summaryOf(item)"
            :direction="direction"
          />
        </el-col>
        <el-col :span="12">
          <div class="compare-head">
            草稿策略
            <span class="jb-muted">（v{{ draft?.version_no }}）</span>
          </div>
          <el-empty v-if="!draftItems.length" description="无结果" :image-size="60" />
          <StrategyCandidateCard
            v-for="item in draftItems"
            :key="`draft-${item.target_id}`"
            :item="item"
            :summary="summaryOf(item)"
            :direction="direction"
          />
        </el-col>
      </el-row>

      <div class="section-title">排名变化</div>
      <el-table :data="result.rank_changes || []" border size="small">
        <el-table-column label="候选 ID" width="110">
          <template #default="{ row }"><span class="mono">{{ row.target_id }}</span></template>
        </el-table-column>
        <el-table-column label="线上位次" width="90">
          <template #default="{ row }">{{ row.current_position ?? '未进入' }}</template>
        </el-table-column>
        <el-table-column label="草稿位次" width="90">
          <template #default="{ row }">{{ row.draft_position ?? '未进入' }}</template>
        </el-table-column>
        <el-table-column label="变化" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="movementType(row.movement)">
              {{ MOVEMENT_LABEL[row.movement] || row.movement }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="原因码" min-width="320">
          <template #default="{ row }">
            <el-tag
              v-for="code in row.reason_codes || []"
              :key="code"
              size="small"
              class="reason-tag"
              :type="reasonCodeTagType(code)"
            >
              {{ reasonCodeLabel(code) }}
            </el-tag>
            <span v-if="!(row.reason_codes || []).length" class="jb-muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import StrategyCandidateCard from './StrategyCandidateCard.vue'
import {
  MOVEMENT_LABEL,
  STRATEGY_DIRECTIONS,
  reasonCodeLabel,
  reasonCodeTagType,
  simulateRecommendationDraft,
} from '@/api/recommendationStrategies'

const props = defineProps({
  direction: { type: String, required: true },
  draft: { type: Object, default: null },
})

const emit = defineEmits(['failed', 'simulated'])

const running = ref(false)
const result = ref(null)
const form = reactive({
  user_id: '',
  raw_query: '',
  city: [],
  job_category: [],
  salary: 0,
})

const directionLabel = computed(
  () => STRATEGY_DIRECTIONS.find((item) => item.value === props.direction)?.label || props.direction,
)
const salaryLabel = computed(() =>
  props.direction === 'search_job' ? '期望月薪下限' : '可接受月薪上限',
)
const criteriaValid = computed(() => form.city.length > 0 || form.job_category.length > 0)
const canRun = computed(() => !!props.draft && criteriaValid.value)
const needsSimulation = computed(() => {
  const draft = props.draft
  if (!draft) return false
  return !draft.last_simulated_digest || draft.last_simulated_digest !== draft.parameters_digest
})

const currentItems = computed(() => result.value?.current || [])
const draftItems = computed(() => result.value?.draft || [])

// 换方向或换草稿时旧结果不再可解读，直接丢弃避免误读。
watch(
  [
    () => props.direction,
    () => props.draft?.id,
    () => props.draft?.parameters_digest,
  ],
  () => {
    result.value = null
  },
)

function summaryOf(item) {
  return result.value?.candidate_summaries?.[String(item.target_id)] || null
}

function movementType(movement) {
  if (movement === 'up' || movement === 'entered') return 'success'
  if (movement === 'down' || movement === 'left') return 'danger'
  return 'info'
}

function buildCriteria() {
  const criteria = {}
  if (form.city.length) criteria.city = [...form.city]
  if (form.job_category.length) criteria.job_category = [...form.job_category]
  if (form.salary) {
    if (props.direction === 'search_job') criteria.salary_floor_monthly = Number(form.salary)
    else criteria.salary_ceiling_monthly = Number(form.salary)
  }
  return criteria
}

async function onRun() {
  if (!canRun.value) return
  running.value = true
  try {
    const payload = {
      direction: props.direction,
      draft_version_id: props.draft.id,
      raw_query: form.raw_query || '',
      criteria: buildCriteria(),
    }
    if (form.user_id.trim()) payload.user_id = form.user_id.trim()
    result.value = await simulateRecommendationDraft(props.draft.id, payload)
    ElMessage.success('模拟完成，最后模拟指纹已更新')
    // 模拟会写 last_simulated_digest，父组件需要重新拉版本才能解锁发布按钮。
    emit('simulated')
  } catch (err) {
    emit('failed', { err, fallback: '模拟失败' })
  } finally {
    running.value = false
  }
}
</script>

<style scoped>
.strategy-simulation {
  display: grid;
  gap: 14px;
}
.sim-alert :deep(.el-alert__content) {
  line-height: 1.7;
}
.sim-form {
  padding: 14px 16px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.draft-hint {
  margin-left: 10px;
  font-size: 12.5px;
}
.hint {
  margin-left: 12px;
  font-size: 12.5px;
}
.result-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.compare-row {
  margin: 0;
}
.compare-head {
  font-weight: 600;
  margin-bottom: 8px;
}
.section-title {
  font-weight: 600;
  margin-top: 4px;
}
.reason-tag {
  margin: 0 4px 4px 0;
}
</style>
