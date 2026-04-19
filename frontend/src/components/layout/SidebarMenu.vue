<template>
  <div class="sidebar" :class="{ collapsed }">
    <div class="sidebar-brand">
      <div class="logo-mark mono">JB</div>
      <div v-if="!collapsed" class="brand-text">
        <div class="brand-title">JobBridge</div>
        <div class="brand-sub mono">OPERATOR CONSOLE</div>
      </div>
    </div>

    <nav class="sidebar-nav">
      <div v-if="!collapsed" class="sidebar-section">概览</div>
      <router-link
        v-for="item in topItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: isActive(item) }"
      >
        <el-icon class="nav-icon" :size="17"><component :is="item.icon" /></el-icon>
        <span class="nav-label">{{ item.label }}</span>
        <span
          v-if="item.badge && pendingTotal > 0"
          class="nav-badge mono"
        >{{ pendingTotal > 99 ? '99+' : pendingTotal }}</span>
      </router-link>

      <div v-if="!collapsed" class="sidebar-section">账号</div>
      <div
        v-for="item in accountItems"
        :key="item.path"
      >
        <router-link
          :to="item.path"
          class="nav-item"
          :class="{ active: isActive(item) }"
        >
          <el-icon class="nav-icon" :size="17"><component :is="item.icon" /></el-icon>
          <span class="nav-label">{{ item.label }}</span>
        </router-link>
      </div>

      <div v-if="!collapsed" class="sidebar-section">数据</div>
      <router-link
        v-for="item in dataItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: isActive(item) }"
      >
        <el-icon class="nav-icon" :size="17"><component :is="item.icon" /></el-icon>
        <span class="nav-label">{{ item.label }}</span>
      </router-link>

      <div v-if="!collapsed" class="sidebar-section">字典</div>
      <router-link
        v-for="item in dictItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: isActive(item) }"
      >
        <el-icon class="nav-icon" :size="17"><component :is="item.icon" /></el-icon>
        <span class="nav-label">{{ item.label }}</span>
      </router-link>

      <div v-if="!collapsed" class="sidebar-section">系统</div>
      <router-link
        v-for="item in systemItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        :class="{ active: isActive(item) }"
      >
        <el-icon class="nav-icon" :size="17"><component :is="item.icon" /></el-icon>
        <span class="nav-label">{{ item.label }}</span>
      </router-link>
    </nav>

    <div class="sidebar-foot">
      <span class="live-dot" />
      <span v-if="!collapsed">{{ statusText }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  Odometer,
  Document,
  Briefcase,
  Tickets,
  Setting,
  TrendCharts,
  ChatLineRound,
  OfficeBuilding,
  Avatar,
  User,
  CircleClose,
  Location,
  Management,
  Warning,
} from '@element-plus/icons-vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()

const collapsed = computed(() => appStore.sidebarCollapsed)
const pendingTotal = computed(() => appStore.pendingCount.total || 0)
const statusText = computed(() =>
  appStore.networkOnline ? '在线 · 实时' : '离线',
)

const topItems = [
  { path: '/admin/dashboard', label: 'Dashboard', icon: Odometer },
  { path: '/admin/audit', label: '审核工作台', icon: Document, badge: true },
]
const accountItems = [
  { path: '/admin/accounts/factories', label: '厂家', icon: OfficeBuilding },
  { path: '/admin/accounts/brokers', label: '中介', icon: Avatar },
  { path: '/admin/accounts/workers', label: '工人', icon: User },
  { path: '/admin/accounts/blacklist', label: '黑名单', icon: CircleClose },
]
const dataItems = [
  { path: '/admin/jobs', label: '岗位管理', icon: Briefcase },
  { path: '/admin/resumes', label: '简历管理', icon: Tickets },
  { path: '/admin/reports', label: '数据看板', icon: TrendCharts },
  { path: '/admin/logs/conversations', label: '对话日志', icon: ChatLineRound },
]
const dictItems = [
  { path: '/admin/dicts/cities', label: '城市', icon: Location },
  { path: '/admin/dicts/job-categories', label: '工种', icon: Management },
  { path: '/admin/dicts/sensitive-words', label: '敏感词', icon: Warning },
]
const systemItems = [
  { path: '/admin/config', label: '系统配置', icon: Setting },
]

function isActive(item) {
  const p = route.path
  if (p === item.path) return true
  if (item.path === '/admin/logs/conversations' && p.startsWith('/admin/logs')) return true
  return false
}
</script>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--sidebar-bg);
  color: var(--sidebar-fg);
  overflow: hidden;
}

.sidebar-brand {
  height: var(--topbar-h);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  border-bottom: 1px solid color-mix(in oklab, var(--sidebar-fg) 8%, transparent);
}
.logo-mark {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  background: var(--accent);
  color: var(--accent-fg);
  font-weight: 600;
  font-size: 13px;
  flex-shrink: 0;
  letter-spacing: -0.02em;
}
.brand-text {
  line-height: 1.1;
}
.brand-title {
  font-weight: 600;
  font-size: var(--text-md);
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.brand-sub {
  font-size: 9.5px;
  color: var(--sidebar-fg-muted);
  letter-spacing: 0.18em;
  text-transform: uppercase;
  margin-top: 2px;
}

.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 10px 10px 16px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.sidebar-section {
  font-size: 11px;
  font-weight: 500;
  color: var(--sidebar-fg-muted);
  padding: 14px 10px 6px;
  white-space: nowrap;
  overflow: hidden;
  opacity: 0.75;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 10px;
  border-radius: 7px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--sidebar-fg);
  position: relative;
  text-decoration: none;
  transition: background 0.12s ease, color 0.12s ease;
  user-select: none;
  white-space: nowrap;
  overflow: hidden;
}
.nav-item:hover {
  background: var(--sidebar-hover);
  color: var(--sidebar-fg);
}
.nav-item.active {
  background: var(--sidebar-active);
  font-weight: 500;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 6px;
  bottom: 6px;
  width: 3px;
  background: var(--accent);
  border-radius: 0 3px 3px 0;
}
.nav-icon {
  flex-shrink: 0;
  opacity: 0.85;
}
.nav-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nav-badge {
  font-size: 10.5px;
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--accent-fg);
  font-weight: 500;
  line-height: 1;
}

.sidebar-foot {
  padding: 12px 16px;
  border-top: 1px solid color-mix(in oklab, var(--sidebar-fg) 8%, transparent);
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--sidebar-fg-muted);
  font-family: var(--font-mono);
  letter-spacing: 0.04em;
}
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 0 3px color-mix(in oklab, var(--success) 35%, transparent);
  flex-shrink: 0;
}

/* Collapsed */
.sidebar.collapsed .brand-text,
.sidebar.collapsed .nav-label,
.sidebar.collapsed .nav-badge,
.sidebar.collapsed .sidebar-section,
.sidebar.collapsed .sidebar-foot span {
  display: none;
}
</style>
