import request from './request'

export function fetchCities(params) {
  return request.get('/admin/dicts/cities', { params })
}

export function updateCity(id, data) {
  return request.put(`/admin/dicts/cities/${id}`, data)
}

export function fetchJobCategories(params) {
  return request.get('/admin/dicts/job-categories', { params })
}

export function createJobCategory(data) {
  return request.post('/admin/dicts/job-categories', data)
}

export function updateJobCategory(id, data) {
  return request.put(`/admin/dicts/job-categories/${id}`, data)
}

export function deleteJobCategory(id) {
  return request.delete(`/admin/dicts/job-categories/${id}`)
}

export function fetchSensitiveWords(params) {
  return request.get('/admin/dicts/sensitive-words', { params })
}

export function createSensitiveWord(data) {
  return request.post('/admin/dicts/sensitive-words', data)
}

export function deleteSensitiveWord(id) {
  return request.delete(`/admin/dicts/sensitive-words/${id}`)
}

export function batchCreateSensitiveWords(data) {
  return request.post('/admin/dicts/sensitive-words/batch', data)
}
