<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <h1>JobBridge 运营后台</h1>
        <p class="jb-muted">请使用管理员账号登录</p>
      </div>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            placeholder="admin"
            autocomplete="username"
            clearable
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button
          type="primary"
          style="width: 100%"
          :loading="submitting"
          :disabled="submitting"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>
    </div>

    <el-dialog
      v-model="changePwdVisible"
      title="首次登录，请修改密码"
      width="420px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <el-form
        ref="pwdFormRef"
        :model="pwdForm"
        :rules="pwdRules"
        label-position="top"
      >
        <el-form-item label="原密码" prop="old_password">
          <el-input v-model="pwdForm.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="new_password">
          <el-input v-model="pwdForm.new_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirm">
          <el-input v-model="pwdForm.confirm" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button type="primary" :loading="pwdSubmitting" @click="onChangePassword">
          确认修改
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref(null)
const form = reactive({ username: '', password: '' })
const submitting = ref(false)

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const changePwdVisible = ref(false)
const pwdFormRef = ref(null)
const pwdForm = reactive({ old_password: '', new_password: '', confirm: '' })
const pwdSubmitting = ref(false)
const pwdRules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '新密码长度至少 8 位', trigger: 'blur' },
    {
      validator(_rule, value, cb) {
        if (value && value === pwdForm.old_password) cb(new Error('新密码不能与原密码相同'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator(_rule, value, cb) {
        if (value !== pwdForm.new_password) cb(new Error('两次输入的新密码不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

async function onSubmit() {
  if (submitting.value) return
  try {
    await formRef.value.validate()
  } catch (_e) {
    return
  }
  submitting.value = true
  try {
    await auth.login({ username: form.username, password: form.password })
    try {
      await auth.loadMe()
    } catch (_e) {}
    if (!auth.passwordChanged) {
      changePwdVisible.value = true
    } else {
      router.replace('/admin/dashboard')
    }
  } catch (err) {
    // request interceptor does NOT toast 40001/40002/40003 on /admin/login, and
    // 40301 is a business code it does toast. Keep a single toast here so we
    // always surface a reason without doubling up with the interceptor.
    if (err && (err.code === 40001 || err.code === 40002 || err.code === 40003)) {
      ElMessage.error(err.message || '用户名或密码错误')
    }
  } finally {
    submitting.value = false
  }
}

async function onChangePassword() {
  try {
    await pwdFormRef.value.validate()
  } catch (_e) {
    return
  }
  pwdSubmitting.value = true
  try {
    await auth.changePassword({
      old_password: pwdForm.old_password,
      new_password: pwdForm.new_password,
    })
    ElMessage.success('密码修改成功')
    changePwdVisible.value = false
    router.replace('/admin/dashboard')
  } catch (err) {
    if (err && err.code === 40101 && err.data && err.data.fields) {
      const first = Object.values(err.data.fields)[0]
      ElMessage.error(first || err.message || '新密码不合法')
    }
  } finally {
    pwdSubmitting.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #e0e7ff, #f5f7fa);
}
.login-card {
  width: 380px;
  padding: 32px 28px;
  background: var(--el-bg-color);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.08);
}
.login-header {
  text-align: center;
  margin-bottom: 24px;
}
.login-header h1 {
  margin: 0 0 6px;
  font-size: 20px;
}
</style>
