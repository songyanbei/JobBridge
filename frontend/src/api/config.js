import request from './request'

export function fetchConfig() {
  return request.get('/admin/config')
}

export function updateConfig(key, data) {
  return request.put(`/admin/config/${key}`, data)
}
