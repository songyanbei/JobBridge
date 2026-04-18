import request from './request'
import { downloadBlob } from './download'

export function fetchResumes(params) {
  return request.get('/admin/resumes', { params })
}

export function fetchResumeDetail(id) {
  return request.get(`/admin/resumes/${id}`)
}

export function updateResume(id, data) {
  return request.put(`/admin/resumes/${id}`, data)
}

export function delistResume(id, data) {
  return request.post(`/admin/resumes/${id}/delist`, data)
}

export function extendResume(id, data) {
  return request.post(`/admin/resumes/${id}/extend`, data)
}

export function exportResumes(params) {
  return downloadBlob('/admin/resumes/export', params, 'resumes.csv')
}
