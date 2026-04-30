<template>
  <div class="suggestion">
    <div class="suggestion-head">
      <span class="title">LLM 审核建议</span>
      <el-tag :type="riskTag" effect="light">
        {{ riskLabel }}
      </el-tag>
    </div>

    <div v-if="detail.suggestion" class="block">
      <div class="label">审核建议</div>
      <div class="content">{{ detail.suggestion }}</div>
    </div>

    <div v-if="rules.length" class="block">
      <div class="label">触发规则</div>
      <div class="content">
        <el-tag
          v-for="(r, idx) in rules"
          :key="idx"
          type="warning"
          style="margin-right: 6px; margin-bottom: 4px"
        >
          {{ r }}
        </el-tag>
      </div>
    </div>

    <div v-if="similar.length" class="block">
      <div class="label">相似内容提示</div>
      <div class="content">
        <div v-for="(item, idx) in similar" :key="idx" class="similar-row">
          · {{ item.title || item.summary || item.id }}
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { RISK_LABEL } from '@/utils/constants'

const props = defineProps({
  detail: { type: Object, default: () => ({}) },
})

const rules = computed(() => {
  const r = props.detail.trigger_rules || []
  return Array.isArray(r) ? r : []
})

const similar = computed(() => {
  const s = props.detail.similar_items || props.detail.similar || []
  return Array.isArray(s) ? s : []
})

const riskLabel = computed(() => RISK_LABEL[props.detail.risk_level] || '未知风险')
const riskTag = computed(() => {
  const map = { low: 'success', mid: 'warning', high: 'danger' }
  return map[props.detail.risk_level] || 'info'
})
</script>

<style scoped>
.suggestion {
  background: var(--el-bg-color);
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.suggestion-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.title {
  font-weight: 600;
}
.block {
  margin-bottom: 8px;
}
.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.content {
  font-size: 13px;
  line-height: 1.6;
}
.similar-row {
  color: var(--el-text-color-regular);
  margin-bottom: 3px;
}
</style>
