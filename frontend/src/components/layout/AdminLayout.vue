<template>
  <el-container class="admin-layout">
    <el-aside :width="sidebarWidth" class="admin-aside">
      <SidebarMenu />
    </el-aside>
    <el-container>
      <el-header class="admin-header" :height="'56px'">
        <TopBar />
      </el-header>
      <el-main class="admin-main">
        <div v-if="!appStore.networkOnline" class="jb-risk-banner-high">
          网络已断开，部分操作可能失败。请检查网络连接后重试。
        </div>
        <router-view v-slot="{ Component }">
          <keep-alive :include="cacheList">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>

    <el-dialog
      v-model="forcePwdVisible"
      title="请先修改默认密码"
      width="420px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <ChangePasswordForm @success="onPwdChanged" />
    </el-dialog>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import SidebarMenu from './SidebarMenu.vue'
import TopBar from './TopBar.vue'
import ChangePasswordForm from './ChangePasswordForm.vue'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'

const appStore = useAppStore()
const authStore = useAuthStore()

const sidebarWidth = computed(() =>
  appStore.sidebarCollapsed ? 'var(--jb-sidebar-collapsed-width)' : 'var(--jb-sidebar-width)',
)

const cacheList = []

const forcePwdVisible = ref(false)

let pendingTimer = null

function onOnline() {
  appStore.setNetworkStatus(true)
}
function onOffline() {
  appStore.setNetworkStatus(false)
}

onMounted(async () => {
  window.addEventListener('online', onOnline)
  window.addEventListener('offline', onOffline)
  appStore.refreshPendingCount()
  pendingTimer = window.setInterval(() => {
    appStore.refreshPendingCount()
  }, 60_000)

  try {
    if (!authStore.admin) await authStore.loadMe()
  } catch (_e) {}
  if (!authStore.passwordChanged) {
    forcePwdVisible.value = true
  }
})

onUnmounted(() => {
  window.removeEventListener('online', onOnline)
  window.removeEventListener('offline', onOffline)
  if (pendingTimer) clearInterval(pendingTimer)
})

function onPwdChanged() {
  forcePwdVisible.value = false
}
</script>

<style scoped>
.admin-layout {
  height: 100vh;
}
.admin-aside {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-lighter);
  transition: width 0.2s ease;
  overflow: hidden;
}
.admin-header {
  padding: 0;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.admin-main {
  padding: 0;
  background: var(--el-bg-color-page);
  overflow: auto;
}
</style>
