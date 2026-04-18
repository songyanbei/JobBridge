<template>
  <el-dialog
    :model-value="modelValue"
    title="驳回"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-form :model="form" label-position="top">
      <el-form-item label="预设理由">
        <el-select v-model="form.preset" placeholder="选择一个预设理由" style="width: 100%">
          <el-option
            v-for="r in presets"
            :key="r"
            :label="r"
            :value="r"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="驳回理由（必填）">
        <el-input
          v-model="form.reason"
          type="textarea"
          :rows="3"
          placeholder="请补充详细理由，将回传给提交者"
        />
      </el-form-item>
      <el-form-item>
        <el-checkbox v-model="form.notify">通知提交者</el-checkbox>
        <el-checkbox v-model="form.block_user" style="margin-left: 12px">
          同步拉黑该用户
        </el-checkbox>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="danger"
        :loading="submitting"
        :disabled="!form.reason.trim()"
        @click="onSubmit"
      >
        提交驳回
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, watch } from 'vue'
import { PREDEFINED_REJECT_REASONS } from '@/utils/constants'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'submit'])
const presets = PREDEFINED_REJECT_REASONS

const form = reactive({
  preset: '',
  reason: '',
  notify: true,
  block_user: false,
})

watch(
  () => form.preset,
  (v) => {
    if (v && !form.reason) form.reason = v
  },
)

watch(
  () => props.modelValue,
  (v) => {
    if (!v) {
      form.preset = ''
      form.reason = ''
      form.notify = true
      form.block_user = false
    }
  },
)

function onSubmit() {
  if (!form.reason.trim()) return
  emit('submit', {
    reason: form.reason.trim(),
    notify: form.notify,
    block_user: form.block_user,
  })
}
</script>
