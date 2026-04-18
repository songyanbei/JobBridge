<template>
  <el-dialog
    :model-value="modelValue"
    title="编辑字段"
    width="640px"
    :close-on-click-modal="false"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-form v-if="isJob" :model="form" label-position="top">
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="岗位标题">
            <el-input v-model="form.title" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="工种">
            <el-input v-model="form.job_category" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="城市">
            <el-input v-model="form.city" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="区县">
            <el-input v-model="form.district" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="薪资下限">
            <el-input-number v-model="form.salary_floor_monthly" :min="0" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="薪资上限">
            <el-input-number v-model="form.salary_ceiling_monthly" :min="0" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="支付方式">
            <el-select v-model="form.pay_type" style="width: 100%">
              <el-option label="日结" value="daily" />
              <el-option label="月结" value="monthly" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>

    <el-form v-else :model="form" label-position="top">
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="性别">
            <el-select v-model="form.gender" style="width: 100%">
              <el-option label="男" value="male" />
              <el-option label="女" value="female" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="年龄">
            <el-input-number v-model="form.age" :min="16" :max="80" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="24">
          <el-form-item label="期望工种">
            <el-input v-model="form.expected_job_categories" />
          </el-form-item>
        </el-col>
        <el-col :span="24">
          <el-form-item label="期望城市">
            <el-input v-model="form.expected_cities" />
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>

    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSave">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  targetType: { type: String, default: 'job' },
  detail: { type: Object, default: () => ({}) },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'submit'])

const isJob = computed(() => props.targetType === 'job')
const form = reactive({})

watch(
  () => props.modelValue,
  (v) => {
    if (!v) return
    const snap = props.detail.fields || props.detail || {}
    for (const k of Object.keys(form)) delete form[k]
    Object.assign(form, snap)
  },
)

function onSave() {
  emit('submit', { ...form })
}
</script>
