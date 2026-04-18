import request from './request'
import { downloadBlob } from './download'

export function fetchConversations(params) {
  return request.get('/admin/logs/conversations', { params })
}

export function exportConversations(params) {
  return downloadBlob('/admin/logs/conversations/export', params, 'conversations.csv')
}
