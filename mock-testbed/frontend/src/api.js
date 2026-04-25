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
// 1.5) 拉 corpid / agentid 等常量（单点真源；首次请求后缓存在内存）
// ============================================================================
let _configCache = null
let _configInflight = null

export async function fetchMockConfig() {
  if (_configCache) return _configCache
  if (_configInflight) return _configInflight
  _configInflight = request.get('/mock/wework/config').then((data) => {
    _configCache = data
    _configInflight = null
    return data
  }).catch((err) => {
    _configInflight = null
    throw err
  })
  return _configInflight
}

// ============================================================================
// 2) 入站消息（模拟用户在企业微信里发消息给应用）
// ============================================================================
// 前端不再硬编码 corpid / agentid 默认值 —— 从 /mock/wework/config 拉取，
// 避免和后端 MockSettings.corpid/agentid 两处各写一份导致漂移。
export async function mockInbound({ externalUserid, content, msgType = 'text' }) {
  const config = await fetchMockConfig()
  const payload = {
    ToUserName:   config.corpid,
    FromUserName: externalUserid,
    CreateTime:   Math.floor(Date.now() / 1000),
    MsgType:      msgType,
    Content:      content,
    MsgId:        `mock_msgid_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`,
    AgentID:      config.agentid,
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
