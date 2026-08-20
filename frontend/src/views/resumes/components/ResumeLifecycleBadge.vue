<template>
  <span class="resume-lifecycle" :data-state="state">
    {{ label }}<span v-if="conflictReason" class="resume-conflict"> · {{ conflictReason }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { resolveResumeLifecycle } from '@/utils/resumeLifecycle'

const props = defineProps({
  resume: { type: Object, required: true },
  now: { type: [Date, String, Number], default: null },
})
const state = computed(() => resolveResumeLifecycle(props.resume, props.now ?? new Date()))
const label = computed(() => ({ active: '在线', candidate: '候选', history: '历史', legacy: '兼容' })[state.value])
const conflictReason = computed(() => props.resume.replacement_conflict_reason || '')
</script>
