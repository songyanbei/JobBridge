import request from './request'
import { downloadBlob } from './download'

export function fetchDashboard() {
  return request.get('/admin/reports/dashboard')
}

export function fetchTrends(params) {
  return request.get('/admin/reports/trends', { params })
}

export function fetchTop(params) {
  return request.get('/admin/reports/top', { params })
}

export function fetchFunnel(params) {
  return request.get('/admin/reports/funnel', { params })
}

export function exportReports(params) {
  return downloadBlob('/admin/reports/export', params, 'reports.csv')
}
