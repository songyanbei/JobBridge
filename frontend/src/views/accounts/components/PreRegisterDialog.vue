<template>
  <el-dialog
    :model-value="modelValue"
    :title="role === 'broker' ? '中介预注册' : '厂家预注册'"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <el-form-item label="显示名称" prop="display_name">
        <el-input v-model="form.display_name" />
      </el-form-item>
      <el-form-item label="公司 / 机构" prop="company">
        <el-input v-model="form.company" />
      </el-form-item>
      <el-form-item label="联系人" prop="contact_person">
        <el-input v-model="form.contact_person" />
      </el-form-item>
      <el-form-item label="手机号" prop="phone">
        <el-input v-model="form.phone" maxlength="11" />
      </el-form-item>
      <el-form-item label="external_userid（可选）">
        <el-input v-model="form.external_userid" />
      </el-form-item>
      <template v-if="role === 'broker'">
        <el-form-item label="检索能力">
          <el-checkbox v-model="form.can_search_jobs">可检索岗位</el-checkbox>
          <el-checkbox v-model="form.can_search_workers">可检索工人</el-checkbox>
        </el-form-item>
      </template>
    </el-form>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSubmit">提交</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, ref, watch } from 'vue'
import { isPhone } from '@/utils/validators'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  role: { type: String, default: 'factory' },
  submitting: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'submit'])
const formRef = ref(null)
const form = reactive({
  display_name: '',
  company: '',
  contact_person: '',
  phone: '',
  external_userid: '',
  can_search_jobs: true,
  can_search_workers: true,
})

const rules = {
  display_name: [{ required: true, message: '请输入显示名称', trigger: 'blur' }],
  company: [{ required: true, message: '请输入公司/机构', trigger: 'blur' }],
  contact_person: [{ required: true, message: '请输入联系人', trigger: 'blur' }],
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    {
      validator(_r, v, cb) {
        if (!isPhone(v)) cb(new Error('手机号格式不正确'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

watch(
  () => props.modelValue,
  (v) => {
    if (!v) {
      form.display_name = ''
      form.company = ''
      form.contact_person = ''
      form.phone = ''
      form.external_userid = ''
      form.can_search_jobs = true
      form.can_search_workers = true
    }
  },
)

async function onSubmit() {
  try {
    await formRef.value.validate()
  } catch (_e) {
    return
  }
  emit('submit', { ...form, role: props.role })
}
</script>
