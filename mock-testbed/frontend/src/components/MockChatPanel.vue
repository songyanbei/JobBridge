<template>
  <div class="chat-panel">
    <div class="panel-header">
      <MockIdentityPicker
        v-model="externalUserid"
        :role-filter="roleFilter"
        :placeholder="placeholder"
        @change="onIdentityChange"
      />
      <span class="status" :class="statusClass">{{ statusText }}</span>
    </div>

    <div class="messages" ref="messagesEl">
      <div v-if="!externalUserid" class="empty-hint">
        请从上方切换器选择一个模拟身份
      </div>
      <div v-else v-for="(m, i) in messages" :key="i" :class="['bubble', m.direction]">
        <div class="content">{{ m.content }}</div>
        <div class="meta">{{ m.direction === 'in' ? '我（' + externalUserid + '）' : 'bot 回复' }} · {{ formatTs(m.ts) }}</div>
      </div>
    </div>

    <div class="composer">
      <el-input
        v-model="draft"
        placeholder="输入消息后回车或点发送"
        :disabled="!externalUserid || sending"
        @keyup.enter="send"
        clearable
      />
      <el-button
        type="primary"
        :disabled="!externalUserid || !draft.trim() || sending"
        :loading="sending"
        @click="send"
      >
        发送
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
import { mockInbound, openMockSse } from '@/api.js'
import MockIdentityPicker from './MockIdentityPicker.vue'

const props = defineProps({
  roleFilter: { type: Array, default: null },
  initialUserid: { type: String, default: '' },
  placeholder: { type: String, default: '选择模拟身份' },
})

const externalUserid = ref(props.initialUserid || '')
const messages = ref([])
const draft = ref('')
const sending = ref(false)
const sseStatus = ref('disconnected')  // disconnected | connecting | ready | error
const messagesEl = ref(null)
let es = null

const statusText = computed(() => ({
  disconnected: 'SSE 未连接',
  connecting: 'SSE 连接中…',
  ready: 'SSE 已连接',
  error: 'SSE 错误',
})[sseStatus.value])

const statusClass = computed(() => ({
  ready: 'status-ok',
  error: 'status-err',
}[sseStatus.value] || 'status-idle'))

function formatTs(ts) {
  const d = new Date(ts)
  return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`
}

function scrollToBottom() {
  nextTick(() => {
    if (messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    }
  })
}

function closeSse() {
  if (es) {
    try { es.close() } catch { /* ignore */ }
    es = null
  }
}

function onIdentityChange(newId) {
  closeSse()
  messages.value = []
  sseStatus.value = 'disconnected'
  if (!newId) return

  sseStatus.value = 'connecting'
  es = openMockSse(newId, {
    onReady: () => { sseStatus.value = 'ready' },
    onMessage: (payload) => {
      const content = payload?.text?.content ?? JSON.stringify(payload)
      messages.value.push({ direction: 'out', content, ts: Date.now() })
      scrollToBottom()
    },
    onError: () => { sseStatus.value = 'error' },
  })
}

async function send() {
  if (!externalUserid.value || !draft.value.trim()) return
  sending.value = true
  const content = draft.value.trim()
  try {
    const resp = await mockInbound({
      externalUserid: externalUserid.value,
      content,
    })
    if (resp?.errcode === 0) {
      messages.value.push({ direction: 'in', content, ts: Date.now() })
      draft.value = ''
      scrollToBottom()
    } else {
      console.warn('[MockChatPanel] inbound rejected:', resp)
      messages.value.push({
        direction: 'in',
        content: content + `\n[⚠️ 入站被拒：${resp?.errmsg || 'unknown'}]`,
        ts: Date.now(),
      })
    }
  } catch (err) {
    console.error('[MockChatPanel] send failed', err)
    messages.value.push({
      direction: 'in',
      content: content + '\n[⚠️ 发送异常，请看控制台]',
      ts: Date.now(),
    })
  } finally {
    sending.value = false
  }
}

onBeforeUnmount(closeSse)
</script>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: #fff;
  border-radius: 6px;
  border: 1px solid #e4e7ed;
  overflow: hidden;
}
.panel-header {
  padding: 12px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  gap: 12px;
  align-items: center;
  background: #fafbfc;
}
.status {
  font-size: 12px;
  white-space: nowrap;
  padding: 2px 8px;
  border-radius: 10px;
}
.status-idle { color: #909399; background: #f0f2f5; }
.status-ok { color: #fff; background: #67c23a; }
.status-err { color: #fff; background: #f56c6c; }

.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: #f5f7fa;
}
.empty-hint {
  text-align: center;
  color: #a8abb2;
  padding: 40px 0;
  font-size: 13px;
}

.bubble {
  max-width: 70%;
  padding: 10px 12px;
  border-radius: 8px;
  line-height: 1.4;
  word-break: break-word;
}
.bubble .content { white-space: pre-wrap; }
.bubble .meta {
  font-size: 11px;
  color: #909399;
  margin-top: 4px;
}
.bubble.in {
  align-self: flex-end;
  background: #409eff;
  color: #fff;
}
.bubble.in .meta { color: #e6f1ff; }
.bubble.out {
  align-self: flex-start;
  background: #fff;
  border: 1px solid #e4e7ed;
  color: #303133;
}

.composer {
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  display: flex;
  gap: 8px;
  background: #fafbfc;
}
.composer .el-input { flex: 1; }
</style>
