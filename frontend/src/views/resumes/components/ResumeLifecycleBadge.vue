<template>
  <span class="resume-lifecycle" :data-state="state">
    {{ label }}<span v-if="conflictReason" class="resume-conflict"> · {{ conflictReason }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ resume: { type: Object, required: true } })
const expired = computed(() => props.resume.expires_at && new Date(props.resume.expires_at) <= new Date())
const state = computed(() => {
  if (props.resume.deleted_at || props.resume.delist_reason || expired.value) return 'history'
  if (!props.resume.activated_at && props.resume.candidate_expires_at) return 'candidate'
  if (props.resume.activated_at && props.resume.expires_at) return 'active'
  return 'legacy'
})
const label = computed(() => ({ active: '在线', candidate: '候选', history: '历史', legacy: '兼容' })[state.value])
const conflictReason = computed(() => props.resume.replacement_conflict_reason || '')
</script>
