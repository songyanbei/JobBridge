<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="420px"
    :close-on-click-modal="false"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <div v-if="message" class="confirm-message">{{ message }}</div>
    <el-form v-if="requireReason" :model="form" label-position="top">
      <el-form-item label="理由（必填）">
        <el-input v-model="form.reason" type="textarea" :rows="3" />
      </el-form-item>
    </el-form>
    <slot />
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button
        :type="dangerous ? 'danger' : 'primary'"
        :loading="submitting"
        :disabled="requireReason && !form.reason.trim()"
        @click="onConfirm"
      >
        {{ confirmText }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '确认操作' },
  message: { type: String, default: '' },
  dangerous: { type: Boolean, default: true },
  requireReason: { type: Boolean, default: false },
  submitting: { type: Boolean, default: false },
  confirmText: { type: String, default: '确认' },
})

const emit = defineEmits(['update:modelValue', 'confirm'])
const form = reactive({ reason: '' })

watch(
  () => props.modelValue,
  (v) => {
    if (v) form.reason = ''
  },
)

function onConfirm() {
  if (props.requireReason && !form.reason.trim()) return
  emit('confirm', { reason: form.reason.trim() })
}
</script>

<style scoped>
.confirm-message {
  margin-bottom: 12px;
  line-height: 1.6;
}
</style>
