<template>
  <div class="sidebar">
    <div class="sidebar-brand">
      <span v-if="!collapsed">JobBridge</span>
      <el-icon v-else :size="20"><Connection /></el-icon>
    </div>
    <el-menu
      :default-active="activeIndex"
      :default-openeds="defaultOpeneds"
      :collapse="collapsed"
      router
      :unique-opened="false"
      class="sidebar-menu"
      background-color="transparent"
    >
      <el-menu-item index="/admin/dashboard">
        <el-icon><Odometer /></el-icon>
        <template #title>Dashboard</template>
      </el-menu-item>
      <el-menu-item index="/admin/audit">
        <el-icon><Document /></el-icon>
        <template #title>
          <span>审核工作台</span>
          <el-badge
            v-if="pendingTotal > 0"
            :value="pendingTotal"
            :max="99"
            class="sidebar-badge"
          />
        </template>
      </el-menu-item>

      <el-sub-menu index="accounts">
        <template #title>
          <el-icon><User /></el-icon>
          <span>账号管理</span>
        </template>
        <el-menu-item index="/admin/accounts/factories">厂家</el-menu-item>
        <el-menu-item index="/admin/accounts/brokers">中介</el-menu-item>
        <el-menu-item index="/admin/accounts/workers">工人</el-menu-item>
        <el-menu-item index="/admin/accounts/blacklist">黑名单</el-menu-item>
      </el-sub-menu>

      <el-menu-item index="/admin/jobs">
        <el-icon><Briefcase /></el-icon>
        <template #title>岗位管理</template>
      </el-menu-item>
      <el-menu-item index="/admin/resumes">
        <el-icon><Tickets /></el-icon>
        <template #title>简历管理</template>
      </el-menu-item>

      <el-sub-menu index="dicts">
        <template #title>
          <el-icon><Collection /></el-icon>
          <span>字典管理</span>
        </template>
        <el-menu-item index="/admin/dicts/cities">城市</el-menu-item>
        <el-menu-item index="/admin/dicts/job-categories">工种</el-menu-item>
        <el-menu-item index="/admin/dicts/sensitive-words">敏感词</el-menu-item>
      </el-sub-menu>

      <el-menu-item index="/admin/config">
        <el-icon><Setting /></el-icon>
        <template #title>系统配置</template>
      </el-menu-item>
      <el-menu-item index="/admin/reports">
        <el-icon><TrendCharts /></el-icon>
        <template #title>数据看板</template>
      </el-menu-item>
      <el-menu-item index="/admin/logs/conversations">
        <el-icon><ChatLineRound /></el-icon>
        <template #title>对话日志</template>
      </el-menu-item>
    </el-menu>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()

const collapsed = computed(() => appStore.sidebarCollapsed)
const pendingTotal = computed(() => appStore.pendingCount.total || 0)

const activeIndex = computed(() => {
  // Keep active state aligned with the deepest matching path
  return route.path
})

const defaultOpeneds = computed(() => {
  const path = route.path
  const groups = []
  if (path.startsWith('/admin/accounts/')) groups.push('accounts')
  if (path.startsWith('/admin/dicts/')) groups.push('dicts')
  // still include both by default so badges & ink visible
  return Array.from(new Set([...groups, 'accounts', 'dicts']))
})
</script>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.sidebar-brand {
  height: var(--jb-header-height);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-primary);
}
.sidebar-menu {
  flex: 1;
  border: none;
  overflow-y: auto;
}
.sidebar-badge {
  margin-left: 8px;
}
</style>
