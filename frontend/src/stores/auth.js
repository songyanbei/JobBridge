import { defineStore } from 'pinia'
import { login as apiLogin, getMe, changePassword as apiChangePassword } from '@/api/auth'

const TOKEN_KEY = 'jobbridge_admin_token'
const EXPIRES_KEY = 'jobbridge_admin_expires_at'
const PWD_KEY = 'jobbridge_admin_password_changed'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: null,
    expiresAt: null,
    admin: null,
    passwordChanged: false,
    initialized: false,
  }),
  getters: {
    isExpired(state) {
      if (!state.expiresAt) return true
      return new Date(state.expiresAt).getTime() <= Date.now()
    },
    isAuthenticated(state) {
      if (!state.token) return false
      if (!state.expiresAt) return false
      return new Date(state.expiresAt).getTime() > Date.now()
    },
  },
  actions: {
    restoreFromStorage() {
      if (this.initialized) return
      const token = localStorage.getItem(TOKEN_KEY)
      const expiresAt = localStorage.getItem(EXPIRES_KEY)
      const pwd = localStorage.getItem(PWD_KEY)
      if (token && expiresAt && new Date(expiresAt).getTime() > Date.now()) {
        this.token = token
        this.expiresAt = expiresAt
        this.passwordChanged = pwd === '1'
      } else {
        this.clear()
      }
      this.initialized = true
    },
    async login(payload) {
      const data = await apiLogin(payload)
      this.token = data.access_token
      this.expiresAt = data.expires_at
      this.passwordChanged = !!data.password_changed
      localStorage.setItem(TOKEN_KEY, this.token)
      localStorage.setItem(EXPIRES_KEY, this.expiresAt)
      localStorage.setItem(PWD_KEY, this.passwordChanged ? '1' : '0')
      return data
    },
    async loadMe() {
      const data = await getMe()
      this.admin = data
      if (typeof data.password_changed !== 'undefined') {
        this.passwordChanged = !!data.password_changed
        localStorage.setItem(PWD_KEY, this.passwordChanged ? '1' : '0')
      }
      return data
    },
    async changePassword(payload) {
      const data = await apiChangePassword(payload)
      this.passwordChanged = true
      localStorage.setItem(PWD_KEY, '1')
      return data
    },
    clear() {
      this.token = null
      this.expiresAt = null
      this.admin = null
      this.passwordChanged = false
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(EXPIRES_KEY)
      localStorage.removeItem(PWD_KEY)
    },
    logout() {
      this.clear()
    },
  },
})
