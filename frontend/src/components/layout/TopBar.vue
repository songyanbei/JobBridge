<template>
  <div class="topbar">
    <div class="topbar-left">
      <el-button link :icon="Expand" @click="appStore.toggleSidebar()" />
      <el-breadcrumb separator="/">
        <el-breadcrumb-item :to="{ path: '/admin/dashboard' }">首页</el-breadcrumb-item>
        <el-breadcrumb-item v-if="crumbs.length">
          {{ crumbs[crumbs.length - 1] }}
        </el-breadcrumb-item>
      </el-breadcrumb>
    </div>

    <div class="topbar-right">
      <el-input
        v-model="search"
        placeholder="搜索 userid"
        clearable
        class="topbar-search"
        @keyup.enter="onSearch"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>

      <el-tooltip content="通知" placement="bottom">
        <el-badge :is-dot="hasNotify" class="notify-badge">
          <el-button link :icon="Bell" @click="onNotify" />
        </el-badge>
      </el-tooltip>

      <el-tooltip :content="themeTip" placement="bottom">
        <el-button link :icon="themeIcon" @click="appStore.toggleTheme()" />
      </el-tooltip>

      <el-dropdown trigger="click" @command="onCommand">
        <span class="avatar-wrap">
          <el-avatar :size="32">{{ adminInitial }}</el-avatar>
          <span class="avatar-name">{{ adminName }}</span>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="password">修改密码</el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <el-dialog v-model="pwdVisible" title="修改密码" width="420px">
      <ChangePasswordForm @success="pwdVisible = false" />
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Bell, Expand, Moon, Search, Sunny } from '@element-plus/icons-vue'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import ChangePasswordForm from './ChangePasswordForm.vue'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const authStore = useAuthStore()

const search = ref('')
const hasNotify = ref(false)
const pwdVisible = ref(false)

const crumbs = computed(() => {
  const titles = []
  for (const match of route.matched) {
    if (match.meta && match.meta.title) titles.push(match.meta.title)
  }
  return titles
})

const adminName = computed(() => authStore.admin?.display_name || authStore.admin?.username || 'admin')
const adminInitial = computed(() => (adminName.value || 'A').slice(0, 1).toUpperCase())

const themeIcon = computed(() => (appStore.theme === 'dark' ? Sunny : Moon))
const themeTip = computed(() => (appStore.theme === 'dark' ? '切换为浅色' : '切换为深色'))

function onSearch() {
  const q = search.value.trim()
  if (!q) return
  // Global userid search → jump to conversation logs with userid prefilled
  router.push({ path: '/admin/logs/conversations', query: { userid: q } })
}

function onNotify() {
  ElMessage.info('暂无新通知')
}

async function onCommand(cmd) {
  if (cmd === 'password') {
    pwdVisible.value = true
    return
  }
  if (cmd === 'logout') {
    try {
      await ElMessageBox.confirm('确认退出登录？', '提示', {
        confirmButtonText: '退出',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch (_e) {
      return
    }
    authStore.logout()
    router.push('/admin/login')
  }
}
</script>

<style scoped>
.topbar {
  height: 56px;
  padding: 0 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.topbar-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.topbar-search {
  width: 220px;
}
.avatar-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.avatar-name {
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.notify-badge :deep(.el-badge__content) {
  top: 6px;
  right: 6px;
}
</style>
