import request from './request'

export function fetchConfig() {
  return request.get('/admin/config')
}

export function updateConfig(key, data) {
  return request.put(`/admin/config/${key}`, data)
}

export function fetchVisibilityPolicy() {
  return request.get('/admin/config/visibility-policy')
}

export function validateVisibilityPolicy(policy) {
  return request.post('/admin/config/visibility-policy/validate', { policy })
}

export function saveVisibilityPolicy(data) {
  return request.put('/admin/config/visibility-policy', data)
}

export function fetchVisibilityPolicyHistory() {
  return request.get('/admin/config/visibility-policy/history')
}

export function fetchVisibilityPolicyHistoryDetail(revision) {
  return request.get(`/admin/config/visibility-policy/history/${revision}`)
}

export function restoreVisibilityPolicy(revision, data) {
  return request.post(`/admin/config/visibility-policy/history/${revision}/restore`, data)
}
