<template>
  <el-drawer
    :model-value="modelValue"
    title="提交者审核历史"
    size="420px"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <div v-if="!items || !items.length" class="jb-muted">暂无历史记录</div>
    <el-timeline v-else>
      <el-timeline-item
        v-for="(it, idx) in items"
        :key="idx"
        :color="colorOf(it.action || it.status)"
        :timestamp="formatDateTime(it.created_at || it.time)"
      >
        <div class="row-title">
          {{ it.action || it.status || '--' }} · {{ it.target_type }} #{{ it.target_id }}
        </div>
        <div v-if="it.reason" class="row-sub jb-muted">{{ it.reason }}</div>
      </el-timeline-item>
    </el-timeline>
  </el-drawer>
</template>

<script setup>
import { formatDateTime } from '@/utils/format'

defineProps({
  modelValue: { type: Boolean, default: false },
  items: { type: Array, default: () => [] },
})
defineEmits(['update:modelValue'])

function colorOf(action) {
  if (!action) return '#909399'
  if (action === 'passed' || action === 'pass') return '#67c23a'
  if (action === 'rejected' || action === 'reject') return '#f56c6c'
  return '#e6a23c'
}
</script>

<style scoped>
.row-title {
  font-weight: 500;
}
.row-sub {
  margin-top: 4px;
  font-size: 12px;
}
</style>
