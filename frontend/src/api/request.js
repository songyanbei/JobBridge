import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

const BASE = import.meta.env.VITE_API_BASE || ''

const request = axios.create({
  baseURL: BASE,
  timeout: 20000,
})

request.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token && !config.headers.Authorization) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

request.interceptors.response.use(
  (response) => {
    const body = response.data
    if (response.config.responseType === 'blob') {
      return response
    }
    if (!body || typeof body.code === 'undefined') {
      return body
    }
    if (body.code === 0) {
      return body.data
    }
    if ([40901, 40902, 40903].includes(body.code)) {
      return Promise.reject(body)
    }
    // 40002 / 40003 are session errors (token expired / invalid) — auto-logout.
    // 40001 is *credential* invalid (wrong password, wrong old password on
    // /admin/me/password, wrong API key): it can reach this code path for
    // legitimate reasons that shouldn't evict an otherwise-signed-in admin.
    // Let the calling view handle it (login form, change-password form).
    if ([40002, 40003].includes(body.code)) {
      const current = router.currentRoute.value
      if (current && current.path === '/admin/login') {
        return Promise.reject(body)
      }
      const auth = useAuthStore()
      auth.logout()
      ElMessage.error(body.message || '登录已失效，请重新登录')
      router.push('/admin/login')
      return Promise.reject(body)
    }
    if (body.code === 40001) {
      return Promise.reject(body)
    }
    if (body.code >= 40100 && body.code <= 40199) {
      return Promise.reject(body)
    }
    ElMessage.error(body.message || '系统繁忙')
    return Promise.reject(body)
  },
  (error) => {
    if (error.response && error.response.status >= 500) {
      ElMessage.error('服务异常，请稍后重试')
    } else if (error.code === 'ECONNABORTED') {
      ElMessage.error('请求超时，请稍后重试')
    } else {
      ElMessage.error(error.message || '网络异常')
    }
    return Promise.reject(error)
  },
)

export default request
