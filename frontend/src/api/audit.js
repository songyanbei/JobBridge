import request from './request'

export function fetchAuditQueue(params) {
  return request.get('/admin/audit/queue', { params })
}

export function fetchPendingCount() {
  return request.get('/admin/audit/pending-count')
}

export function fetchAuditDetail(targetType, id) {
  return request.get(`/admin/audit/${targetType}/${id}`)
}

export function lockAuditItem(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/lock`)
}

export function unlockAuditItem(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/unlock`)
}

export function passAuditItem(targetType, id, version) {
  return request.post(`/admin/audit/${targetType}/${id}/pass`, { version })
}

export function rejectAuditItem(targetType, id, data) {
  return request.post(`/admin/audit/${targetType}/${id}/reject`, data)
}

export function editAuditItem(targetType, id, data) {
  return request.put(`/admin/audit/${targetType}/${id}/edit`, data)
}

export function undoAuditAction(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/undo`)
}
