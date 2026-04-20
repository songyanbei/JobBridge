// Mock 企业微信测试台 · 前端 API 封装
//
// 所有请求通过 Vite proxy 转发到沙箱后端（http://localhost:8001）。
// 生产环境不应该部署本沙箱；若真要部署，通过独立 nginx 反向代理接入。

import axios from 'axios'

const request = axios.create({
  baseURL: '',
  timeout: 20000,
})

request.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    console.error('[mock-api] request failed:', err)
    return Promise.reject(err)
  }
)

// ============================================================================
// 1) 列出可选身份
// ============================================================================
export async function fetchMockUsers() {
  return request.get('/mock/wework/users')
}

// ============================================================================
// 2) 入站消息（模拟用户在企业微信里发消息给应用）
// ============================================================================
export async function mockInbound({ externalUserid, content, msgType = 'text', corpid = 'wwmock_corpid', agentid = '1000002' }) {
  const payload = {
    ToUserName:   corpid,
    FromUserName: externalUserid,
    CreateTime:   Math.floor(Date.now() / 1000),
    MsgType:      msgType,
    Content:      content,
    MsgId:        `mock_msgid_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`,
    AgentID:      agentid,
  }
  return request.post('/mock/wework/inbound', payload)
}

// ============================================================================
// 3) 订阅 SSE 出站推送
// ============================================================================
// 返回的 EventSource 需要由调用方 close()
export function openMockSse(externalUserid, { onMessage, onReady, onPing, onError } = {}) {
  const url = `/mock/wework/sse?external_userid=${encodeURIComponent(externalUserid)}`
  const es = new EventSource(url)

  es.addEventListener('ready', (e) => {
    try { onReady && onReady(JSON.parse(e.data)) } catch { /* ignore */ }
  })
  es.addEventListener('message', (e) => {
    try { onMessage && onMessage(JSON.parse(e.data)) } catch (err) { console.warn('SSE parse error', err) }
  })
  es.addEventListener('ping', (e) => {
    try { onPing && onPing(JSON.parse(e.data)) } catch { /* ignore */ }
  })
  es.addEventListener('error', (e) => {
    if (onError) onError(e)
    else console.warn('[mock-sse] error', e)
  })

  return es
}
