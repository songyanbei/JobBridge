<template>
  <button type="button" :disabled="disabled"><slot /></button>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  role: { type: String, default: '' },
  status: { type: String, required: true },
  requiredStatus: { type: String, required: true },
  approvedBy: { type: String, default: '' },
  username: { type: String, default: '' },
  fourEyes: { type: Boolean, default: false },
})
const disabled = computed(() => props.role !== 'super_admin'
  || props.status !== props.requiredStatus
  || (props.fourEyes && !!props.approvedBy && props.approvedBy === props.username))
</script>
