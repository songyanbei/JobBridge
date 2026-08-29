import request from './request'

export const fetchCleanupTasks = (params) => request.get('/admin/cleanup/tasks', { params })
export const fetchMediaIssues = (params) => request.get('/admin/cleanup/media-isolation', { params })
export const fetchMediaDeadLetters = (params) => request.get('/admin/cleanup/media-dead-letters', { params })
export const retryDeadLetters = (data) => request.post('/admin/cleanup/dead-letters/retry', data)
export const approveMediaIssue = (id, data) => request.post(`/admin/cleanup/media-isolation/${id}/approve`, data)
export const executeMediaIssue = (id) => request.post(`/admin/cleanup/media-isolation/${id}/execute`)
