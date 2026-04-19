<template>
  <div class="topbar">
    <div class="topbar-left">
      <button class="icon-btn" aria-label="切换侧栏" @click="appStore.toggleSidebar()">
        <el-icon :size="18"><Expand /></el-icon>
      </button>
      <div class="crumbs">
        <router-link to="/admin/dashboard" class="crumb">首页</router-link>
        <template v-if="crumbs.length">
          <span class="sep">/</span>
          <span class="crumb current">{{ crumbs[crumbs.length - 1] }}</span>
        </template>
      </div>
    </div>

    <div class="topbar-right">
      <div class="search" :class="{ focused: searchFocused }">
        <el-icon class="leading" :size="14"><Search /></el-icon>
        <input
          v-model="search"
          placeholder="搜索 userid…"
          @focus="searchFocused = true"
          @blur="searchFocused = false"
          @keyup.enter="onSearch"
        />
        <span class="kbd mono">↵</span>
      </div>

      <el-tooltip :content="notifyTip" placement="bottom">
        <el-badge
          :value="pendingTotal"
          :hidden="pendingTotal === 0"
          :max="99"
          class="notify-badge"
        >
          <button class="icon-btn" @click="onNotify">
            <el-icon :size="18"><Bell /></el-icon>
          </button>
        </el-badge>
      </el-tooltip>

      <el-tooltip :content="themeTip" placement="bottom">
        <button class="icon-btn" @click="appStore.toggleTheme()">
          <el-icon :size="18">
            <component :is="themeIcon" />
          </el-icon>
        </button>
      </el-tooltip>

      <el-dropdown trigger="click" @command="onCommand">
        <div class="avatar-wrap">
          <div class="avatar mono">{{ adminInitial }}</div>
          <div class="avatar-meta">
            <div class="avatar-name">{{ adminName }}</div>
            <div class="avatar-role mono">ADMIN</div>
          </div>
        </div>
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
const searchFocused = ref(false)
const pwdVisible = ref(false)

const pendingTotal = computed(() => appStore.pendingCount.total || 0)
const notifyTip = computed(() =>
  pendingTotal.value > 0 ? `待审 ${pendingTotal.value} 条，点击进入审核工作台` : '暂无待办',
)

const crumbs = computed(() => {
  const titles = []
  for (const match of route.matched) {
    if (match.meta && match.meta.title) titles.push(match.meta.title)
  }
  return titles
})

const adminName = computed(
  () => authStore.admin?.display_name || authStore.admin?.username || 'admin',
)
const adminInitial = computed(() => (adminName.value || 'A').slice(0, 1).toUpperCase())

const themeIcon = computed(() => (appStore.theme === 'dark' ? Sunny : Moon))
const themeTip = computed(() => (appStore.theme === 'dark' ? '切换为浅色' : '切换为深色'))

function onSearch() {
  const q = search.value.trim()
  if (!q) return
  router.push({ path: '/admin/logs/conversations', query: { userid: q } })
}

function onNotify() {
  if (pendingTotal.value > 0) {
    router.push('/admin/audit')
  } else {
    ElMessage.info('暂无待办')
  }
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
  height: var(--topbar-h);
  padding: 0 var(--page-pad);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--line);
  background: var(--bg-elev);
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-base);
  color: var(--ink-muted);
  min-width: 0;
}
.crumb {
  color: var(--ink-muted);
  text-decoration: none;
}
.crumb:hover {
  color: var(--ink);
}
.crumb.current {
  color: var(--ink);
  font-weight: 500;
}
.sep {
  color: var(--ink-faint);
}

.search {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg-sunk);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  width: 280px;
  transition: border-color 0.12s ease, background 0.12s ease, box-shadow 0.12s ease;
}
.search.focused {
  border-color: var(--focus-ring);
  background: var(--bg-elev);
  box-shadow: 0 0 0 3px color-mix(in oklab, var(--focus-ring) 18%, transparent);
}
.search input {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  font-size: var(--text-base);
  min-width: 0;
  color: var(--ink);
}
.search input::placeholder {
  color: var(--ink-faint);
}
.search .leading {
  color: var(--ink-faint);
  display: grid;
  place-items: center;
}

.kbd {
  font-size: 10.5px;
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-bottom-width: 2px;
  border-radius: 4px;
  color: var(--ink-muted);
  background: var(--bg-elev);
  line-height: 1.3;
  min-width: 16px;
  text-align: center;
  display: inline-block;
}

.icon-btn {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  background: transparent;
  color: var(--ink-2);
  cursor: pointer;
  position: relative;
  transition: background 0.12s ease, color 0.12s ease;
}
.icon-btn:hover {
  background: var(--hover);
  color: var(--ink);
}
.dot-indicator {
  position: absolute;
  top: 7px;
  right: 7px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--danger);
}

.avatar-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 10px 4px 4px;
  border-radius: var(--r-md);
  cursor: pointer;
  color: var(--ink);
  transition: background 0.12s ease;
  border: 1px solid transparent;
}
.avatar-wrap:hover {
  background: var(--hover);
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  background: var(--accent);
  color: var(--accent-fg);
  display: grid;
  place-items: center;
  font-weight: 600;
  font-size: 12px;
  flex-shrink: 0;
}
.avatar-meta {
  line-height: 1.1;
}
.avatar-name {
  font-size: var(--text-base);
  font-weight: 500;
}
.avatar-role {
  font-size: 10px;
  color: var(--ink-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 2px;
}
</style>
