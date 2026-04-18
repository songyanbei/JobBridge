<template>
  <el-form
    ref="formRef"
    :model="form"
    :rules="rules"
    label-position="top"
  >
    <el-form-item label="原密码" prop="old_password">
      <el-input v-model="form.old_password" type="password" show-password />
    </el-form-item>
    <el-form-item label="新密码" prop="new_password">
      <el-input v-model="form.new_password" type="password" show-password />
    </el-form-item>
    <el-form-item label="确认新密码" prop="confirm">
      <el-input v-model="form.confirm" type="password" show-password />
    </el-form-item>
    <el-button type="primary" :loading="submitting" @click="onSubmit" style="width: 100%">
      确认修改
    </el-button>
  </el-form>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const emit = defineEmits(['success'])
const auth = useAuthStore()
const formRef = ref(null)
const submitting = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm: '' })

const rules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '新密码长度至少 8 位', trigger: 'blur' },
    {
      validator(_r, v, cb) {
        if (v && v === form.old_password) cb(new Error('新密码不能与原密码相同'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator(_r, v, cb) {
        if (v !== form.new_password) cb(new Error('两次输入的新密码不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

async function onSubmit() {
  try {
    await formRef.value.validate()
  } catch (_e) {
    return
  }
  submitting.value = true
  try {
    await auth.changePassword({
      old_password: form.old_password,
      new_password: form.new_password,
    })
    ElMessage.success('密码修改成功')
    form.old_password = ''
    form.new_password = ''
    form.confirm = ''
    emit('success')
  } catch (err) {
    if (err && err.code === 40101 && err.data && err.data.fields) {
      const first = Object.values(err.data.fields)[0]
      ElMessage.error(first || '新密码不合法')
    } else if (err && err.code === 40001) {
      ElMessage.error('原密码错误')
    }
  } finally {
    submitting.value = false
  }
}
</script>
