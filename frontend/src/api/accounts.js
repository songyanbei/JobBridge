import request from './request'
import { uploadFile } from './download'

export function fetchFactories(params) {
  return request.get('/admin/accounts/factories', { params })
}

export function fetchFactoryDetail(userid) {
  return request.get(`/admin/accounts/factories/${userid}`)
}

export function createFactory(data) {
  return request.post('/admin/accounts/factories', data)
}

export function updateFactory(userid, data) {
  return request.put(`/admin/accounts/factories/${userid}`, data)
}

export function importFactories(file) {
  return uploadFile('/admin/accounts/factories/import', file)
}

export function fetchBrokers(params) {
  return request.get('/admin/accounts/brokers', { params })
}

export function fetchBrokerDetail(userid) {
  return request.get(`/admin/accounts/brokers/${userid}`)
}

export function createBroker(data) {
  return request.post('/admin/accounts/brokers', data)
}

export function updateBroker(userid, data) {
  return request.put(`/admin/accounts/brokers/${userid}`, data)
}

export function importBrokers(file) {
  return uploadFile('/admin/accounts/brokers/import', file)
}

export function fetchWorkers(params) {
  return request.get('/admin/accounts/workers', { params })
}

export function fetchWorkerDetail(userid) {
  return request.get(`/admin/accounts/workers/${userid}`)
}

export function fetchBlacklist(params) {
  return request.get('/admin/accounts/blacklist', { params })
}

export function blockUser(userid, data) {
  return request.post(`/admin/accounts/${userid}/block`, data)
}

export function unblockUser(userid, data) {
  return request.post(`/admin/accounts/${userid}/unblock`, data)
}
