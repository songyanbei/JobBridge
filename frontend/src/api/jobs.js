import request from './request'
import { downloadBlob } from './download'

export function fetchJobs(params) {
  return request.get('/admin/jobs', { params })
}

export function fetchJobDetail(id) {
  return request.get(`/admin/jobs/${id}`)
}

export function updateJob(id, data) {
  return request.put(`/admin/jobs/${id}`, data)
}

export function delistJob(id, data) {
  return request.post(`/admin/jobs/${id}/delist`, data)
}

export function extendJob(id, data) {
  return request.post(`/admin/jobs/${id}/extend`, data)
}

export function restoreJob(id, data) {
  return request.post(`/admin/jobs/${id}/restore`, data)
}

export function retryReplacement(id, data) {
  return request.post(`/admin/jobs/replacements/${id}/retry`, data)
}

export function cancelReplacement(id, data) {
  return request.post(`/admin/jobs/replacements/${id}/cancel`, data)
}

export function exportJobs(params) {
  return downloadBlob('/admin/jobs/export', params, 'jobs.csv')
}
