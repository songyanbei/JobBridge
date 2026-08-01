<template>
  <div class="candidate-card" :class="{ exploration: item.is_exploration }">
    <div class="card-head">
      <span class="position mono">{{ item.position }}</span>
      <span class="target mono">{{ targetTypeLabel }} #{{ item.target_id }}</span>
      <el-tag v-if="item.is_exploration" type="warning" size="small">探索位</el-tag>
      <span class="final-score mono">综合分 {{ toScore(item.final_score) }}</span>
    </div>

    <div class="card-summary">
      <span v-for="line in summaryLines" :key="line.label" class="summary-item">
        <span class="jb-muted">{{ line.label }}</span>
        <span>{{ line.value }}</span>
      </span>
      <span v-if="!summaryLines.length" class="jb-muted">无可展示摘要</span>
    </div>

    <div v-if="item.score_detail" class="card-scores">
      <span v-for="metric in SCORE_METRICS" :key="metric.key" class="score-item">
        <span class="jb-muted">{{ metric.label }}</span>
        <span class="mono">{{ toScore(item.score_detail[metric.key]) }}</span>
      </span>
    </div>
    <div v-else class="jb-muted no-score">legacy 对照没有 v1 打分明细</div>

    <div v-if="allReasonCodes.length" class="card-reasons">
      <el-tag
        v-for="code in allReasonCodes"
        :key="code"
        size="small"
        class="reason-tag"
        :type="reasonCodeTagType(code)"
      >
        {{ reasonCodeLabel(code) }}
      </el-tag>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { reasonCodeLabel, reasonCodeTagType } from '@/api/recommendationStrategies'
import { formatDate } from '@/utils/format'

const props = defineProps({
  item: { type: Object, required: true },
  summary: { type: Object, default: null },
  direction: { type: String, required: true },
})

const SCORE_METRICS = [
  { key: 'match_score', label: '匹配' },
  { key: 'quality_score', label: '信息质量' },
  { key: 'freshness_score', label: '新鲜度' },
  { key: 'exposure_opportunity', label: '曝光机会' },
  { key: 'base_score', label: '基础分' },
  { key: 'repeat_factor', label: '重复系数' },
  { key: 'repeat_adjusted_score', label: '去重后' },
]

const JOB_SUMMARY_FIELDS = [
  { key: 'company', label: '企业' },
  { key: 'city', label: '城市' },
  { key: 'district', label: '区县' },
  { key: 'job_category', label: '工种' },
  { key: 'salary_floor_monthly', label: '月薪下限' },
  { key: 'salary_ceiling_monthly', label: '月薪上限' },
  { key: 'pay_type', label: '结算' },
  { key: 'employment_type', label: '用工' },
  { key: 'created_at', label: '发布', date: true },
  { key: 'owner_userid', label: '主体' },
]

const RESUME_SUMMARY_FIELDS = [
  { key: 'expected_cities', label: '期望城市' },
  { key: 'expected_districts', label: '期望区县' },
  { key: 'expected_job_categories', label: '期望工种' },
  { key: 'salary_expect_floor_monthly', label: '期望月薪' },
  { key: 'education', label: '学历' },
  { key: 'work_experience', label: '经验' },
  { key: 'age', label: '年龄' },
  { key: 'gender', label: '性别' },
  { key: 'created_at', label: '发布', date: true },
  { key: 'owner_userid', label: '主体' },
]

const targetTypeLabel = computed(() => (props.item.target_type === 'job' ? '岗位' : '简历'))

const summaryLines = computed(() => {
  const data = props.summary
  if (!data) return []
  const fields = props.direction === 'search_job' ? JOB_SUMMARY_FIELDS : RESUME_SUMMARY_FIELDS
  const lines = []
  for (const field of fields) {
    const raw = data[field.key]
    if (raw === null || raw === undefined || raw === '') continue
    if (Array.isArray(raw) && !raw.length) continue
    const value = field.date
      ? formatDate(raw)
      : Array.isArray(raw)
        ? raw.join(' / ')
        : String(raw)
    lines.push({ label: field.label, value })
  }
  return lines
})

const allReasonCodes = computed(() => {
  const codes = [...(props.item.reason_codes || [])]
  for (const code of props.item.score_detail?.reason_codes || []) {
    if (!codes.includes(code)) codes.push(code)
  }
  return codes
})

function toScore(value) {
  if (value === null || value === undefined) return '—'
  const num = Number(value)
  if (Number.isNaN(num)) return '—'
  return num.toFixed(3)
}
</script>

<style scoped>
.candidate-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
  background: var(--el-bg-color);
}
.candidate-card.exploration {
  border-color: var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.position {
  font-weight: 600;
  font-size: 16px;
  min-width: 18px;
}
.target {
  font-weight: 500;
}
.final-score {
  margin-left: auto;
  font-size: 12.5px;
}
.card-summary,
.card-scores {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 12.5px;
  line-height: 1.7;
}
.card-scores {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.summary-item,
.score-item {
  display: inline-flex;
  gap: 4px;
}
.no-score {
  margin-top: 4px;
  font-size: 12.5px;
}
.card-reasons {
  margin-top: 6px;
}
.reason-tag {
  margin: 0 4px 4px 0;
}
</style>
