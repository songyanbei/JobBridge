<template>
  <div class="json-editor">
    <el-input
      :model-value="modelValue"
      type="textarea"
      :rows="rows"
      :placeholder="placeholder"
      resize="vertical"
      @update:model-value="onInput"
    />
    <div v-if="error" class="jb-danger-text json-error">JSON 格式错误：{{ error }}</div>
    <div v-else-if="modelValue" class="jb-muted json-ok">JSON 有效</div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { parseJsonSafe } from '@/utils/validators'

const props = defineProps({
  modelValue: { type: String, default: '' },
  rows: { type: Number, default: 6 },
  placeholder: { type: String, default: '请输入 JSON' },
})

const emit = defineEmits(['update:modelValue', 'valid-change'])
const error = ref(null)

function validate(v) {
  if (!v || !v.trim()) {
    error.value = null
    emit('valid-change', true)
    return
  }
  const result = parseJsonSafe(v)
  error.value = result.ok ? null : result.error
  emit('valid-change', result.ok)
}

function onInput(v) {
  emit('update:modelValue', v)
  validate(v)
}

watch(
  () => props.modelValue,
  (v) => validate(v),
  { immediate: true },
)
</script>

<style scoped>
.json-error,
.json-ok {
  margin-top: 6px;
  font-size: 12px;
}
</style>
