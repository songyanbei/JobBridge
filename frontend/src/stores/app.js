import { defineStore } from 'pinia'
import { fetchPendingCount } from '@/api/audit'

const SIDEBAR_KEY = 'jobbridge_sidebar_collapsed'
const THEME_KEY = 'jobbridge_theme'

export const useAppStore = defineStore('app', {
  state: () => ({
    sidebarCollapsed: localStorage.getItem(SIDEBAR_KEY) === '1',
    theme: localStorage.getItem(THEME_KEY) || 'light',
    pendingCount: { job: 0, resume: 0, total: 0 },
    pendingCountFetchedAt: 0,
    networkOnline: typeof navigator !== 'undefined' ? navigator.onLine : true,
  }),
  actions: {
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed
      localStorage.setItem(SIDEBAR_KEY, this.sidebarCollapsed ? '1' : '0')
    },
    setTheme(theme) {
      this.theme = theme
      localStorage.setItem(THEME_KEY, theme)
      this.applyTheme()
    },
    toggleTheme() {
      this.setTheme(this.theme === 'dark' ? 'light' : 'dark')
    },
    applyTheme() {
      if (typeof document === 'undefined') return
      const el = document.documentElement
      if (this.theme === 'dark') el.classList.add('dark')
      else el.classList.remove('dark')
    },
    async refreshPendingCount() {
      try {
        const data = await fetchPendingCount()
        this.pendingCount = {
          job: data.job ?? 0,
          resume: data.resume ?? 0,
          total: data.total ?? (data.job ?? 0) + (data.resume ?? 0),
        }
        this.pendingCountFetchedAt = Date.now()
      } catch (_e) {
        // swallow; badge stays as-is
      }
    },
    setNetworkStatus(online) {
      this.networkOnline = !!online
    },
  },
})
