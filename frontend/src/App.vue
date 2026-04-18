<template>
  <el-config-provider :locale="locale">
    <router-view />
  </el-config-provider>
</template>

<script setup>
import { onMounted } from 'vue'
import { ElConfigProvider } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'

const locale = zhCn
const appStore = useAppStore()
const authStore = useAuthStore()

onMounted(() => {
  appStore.applyTheme()
  authStore.restoreFromStorage()
})
</script>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
  padding: 0;
}
</style>
