import request from './request'

export function login(data) {
  return request.post('/admin/login', data)
}

export function getMe() {
  return request.get('/admin/me')
}

export function changePassword(data) {
  return request.put('/admin/me/password', data)
}
