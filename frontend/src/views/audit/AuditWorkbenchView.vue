<template>
  <div class="jb-page audit-page">
    <div class="jb-page-header">
      <div class="jb-page-title">
        审核工作台
        <el-tag v-if="pendingTotal > 0" type="warning" size="small" style="margin-left: 8px">
          待审 {{ pendingTotal }}
        </el-tag>
      </div>
      <div class="header-right">
        <el-radio-group v-model="mode" size="small">
          <el-radio-button label="card">单卡精读</el-radio-button>
          <el-radio-button label="list">列表速览</el-radio-button>
        </el-radio-group>
        <el-select v-model="targetType" size="small" style="width: 100px" @change="onTargetTypeChange">
          <el-option label="岗位" value="job" />
          <el-option label="简历" value="resume" />
        </el-select>
        <el-button size="small" :icon="QuestionFilled" @click="helpVisible = true">
          快捷键
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane label="待审" name="pending" />
      <el-tab-pane label="已通过" name="passed" />
      <el-tab-pane label="已驳回" name="rejected" />
    </el-tabs>

    <div v-if="mode === 'card'" class="card-mode">
      <div class="queue-col">
        <div class="queue-head">
          <span>队列（{{ queue.length }}）</span>
          <el-button size="small" :icon="Refresh" @click="reloadQueue" />
        </div>
        <div v-if="queueLoading" v-loading="true" class="queue-loading" />
        <div v-else-if="!queue.length" class="queue-empty">
          <el-empty description="队列已清空，休息一下 🎉" :image-size="80" />
        </div>
        <div v-else class="queue-list">
          <div
            v-for="item in queue"
            :key="item.target_type + item.id"
            class="queue-item"
            :class="{ active: currentItem && currentItem.id === item.id, locked: item.locked_by && !isSelfLocked(item) }"
            @click="selectItem(item)"
          >
            <div class="queue-item-title">
              <el-tag size="small" type="info">{{ item.target_type === 'job' ? '岗位' : '简历' }}</el-tag>
              <span>#{{ item.id }}</span>
            </div>
            <div class="queue-item-sub jb-muted">
              {{ item.extracted_brief || item.owner_userid || '--' }}
            </div>
            <div v-if="item.locked_by" class="jb-warning-text queue-lock-hint">
              🔒 {{ item.locked_by }}
            </div>
          </div>
        </div>
      </div>

      <div class="detail-col">
        <div v-if="!currentItem" class="jb-card empty-detail">
          <el-empty description="请选择一个队列项开始审核" :image-size="80" />
        </div>
        <div v-else v-loading="detailLoading" class="detail-wrap">
          <div
            v-if="detail.risk_level === 'high'"
            class="jb-risk-banner-high"
          >
            高风险内容：请仔细核对后再决策
          </div>
          <div
            v-else-if="detail.risk_level === 'mid'"
            class="jb-risk-banner-mid"
          >
            中风险内容：请谨慎判断
          </div>

          <div
            class="detail-card"
            :class="riskClass"
          >
            <div class="detail-head">
              <div>
                <el-tag size="small">{{ currentItem.target_type === 'job' ? '岗位' : '简历' }}</el-tag>
                <span class="detail-id">#{{ currentItem.id }}</span>
                <el-tag size="small" type="info" style="margin-left: 8px">v{{ detail.version }}</el-tag>
              </div>
              <div class="submitter-bar">
                <span class="jb-muted">提交者：</span>
                <el-link @click="historyVisible = true">
                  {{ detail.owner_userid || '--' }}
                </el-link>
                <span v-if="submitter7d.length" class="jb-muted">
                  (7天内 {{ submitter7d.length }} 条)
                </span>
              </div>
            </div>

            <el-row :gutter="16">
              <el-col :span="14">
                <div class="section-title">A 原始内容</div>
                <div class="raw-content">
                  <div v-for="(v, k) in rawFields" :key="k" class="raw-row">
                    <span class="jb-muted">{{ k }}：</span>
                    <span>{{ v }}</span>
                  </div>
                </div>
                <ImagePreview v-if="images.length" :images="images" style="margin-top: 8px" />
              </el-col>
              <el-col :span="10">
                <div class="section-title">B 风险与建议</div>
                <AuditSuggestionPanel :detail="detail" />
              </el-col>
            </el-row>
          </div>

          <div class="action-bar">
            <el-button type="success" :disabled="!canPass" @click="onPass">
              通过 (P)
            </el-button>
            <el-button type="danger" :disabled="!canReject" @click="rejectVisible = true">
              驳回 (R)
            </el-button>
            <el-button @click="onNext">稍后 (S)</el-button>
            <el-button :icon="EditPen" @click="editVisible = true">编辑 (E)</el-button>
            <el-button
              :type="undoSecondsLeft > 0 ? 'warning' : 'default'"
              :disabled="undoSecondsLeft <= 0 || !undoTarget"
              @click="onUndo"
            >
              撤销 (U)
              <template v-if="undoSecondsLeft > 0 && undoTarget">
                {{ undoTarget.target_type }}#{{ undoTarget.id }} · {{ undoSecondsLeft }}s
              </template>
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <div v-else class="list-mode">
      <PageTable
        :rows="queue"
        :loading="queueLoading"
        :total="queueTotal"
        :page="queuePage"
        :size="queueSize"
        :exportable="false"
        selectable
        @update:page="(v) => { queuePage = v; loadQueue() }"
        @update:size="(v) => { queueSize = v; queuePage = 1; loadQueue() }"
        @selection-change="(v) => (selectedRows = v)"
        @refresh="reloadQueue"
      >
        <template #toolbar-left>
          <el-button
            type="success"
            :disabled="!canBatch"
            @click="onBatch('pass')"
          >
            批量通过（{{ selectedRows.length }}）
          </el-button>
          <el-button
            type="danger"
            :disabled="!canBatch"
            @click="onBatch('reject')"
          >
            批量驳回（{{ selectedRows.length }}）
          </el-button>
          <span class="jb-muted" style="margin-left: 8px; font-size: 12px">
            <template v-if="activeTab !== 'pending'">批量操作仅在“待审”tab 可用</template>
            <template v-else>批量上限 {{ BATCH_AUDIT_LIMIT }} 条</template>
          </span>
        </template>

        <el-table-column prop="target_type" label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.target_type === 'job' ? '岗位' : '简历' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="id" label="ID" width="90" />
        <el-table-column prop="extracted_brief" label="摘要" show-overflow-tooltip />
        <el-table-column prop="risk_level" label="风险" width="90">
          <template #default="{ row }">
            <el-tag
              v-if="row.risk_level"
              :type="{ low: 'success', mid: 'warning', high: 'danger' }[row.risk_level] || 'info'"
              size="small"
            >
              {{ { low: '低', mid: '中', high: '高' }[row.risk_level] || '--' }}
            </el-tag>
            <span v-else class="jb-muted">--</span>
          </template>
        </el-table-column>
        <el-table-column prop="owner_userid" label="提交者" width="120" />
        <el-table-column prop="created_at" label="提交时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="locked_by" label="锁" width="100">
          <template #default="{ row }">
            <span v-if="row.locked_by" class="jb-warning-text">🔒 {{ row.locked_by }}</span>
            <span v-else class="jb-muted">--</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              @click="() => { mode = 'card'; selectItem(row) }"
            >
              打开
            </el-button>
          </template>
        </el-table-column>
      </PageTable>
    </div>

    <RejectPanel
      v-model="rejectVisible"
      :submitting="actionSubmitting"
      @submit="onReject"
    />

    <AuditEditForm
      v-model="editVisible"
      :target-type="currentItem?.target_type"
      :detail="detail"
      :submitting="actionSubmitting"
      @submit="onEdit"
    />

    <SubmitterHistoryDrawer v-model="historyVisible" :items="submitterHistoryFull" />

    <KeyboardHelpModal v-model="helpVisible" />

    <el-dialog v-model="batchVisible" :title="batchTitle" width="520px">
      <div v-if="!batchRunning && !batchResult">
        <p>
          即将对 {{ selectedRows.length }} 条进行{{ batchAction === 'pass' ? '通过' : '驳回' }}操作。
          前端将按条目串行执行单条接口，任一失败将立即中断，已成功条目不回滚。
        </p>
        <el-form v-if="batchAction === 'reject'" :model="batchForm" label-position="top">
          <el-form-item label="驳回理由（必填）">
            <el-input v-model="batchForm.reason" type="textarea" :rows="3" />
          </el-form-item>
        </el-form>
      </div>
      <div v-else-if="batchRunning">
        <el-progress
          :percentage="Math.round(((batchSuccess + batchFailed.length) / selectedRows.length) * 100)"
        />
        <div style="margin-top: 10px" class="jb-muted">
          进度 {{ batchSuccess + batchFailed.length }} / {{ selectedRows.length }}
        </div>
      </div>
      <div v-else>
        <el-alert
          :title="`已成功 ${batchSuccess} 条，失败 ${batchFailed.length} 条`"
          :type="batchFailed.length ? 'warning' : 'success'"
          :closable="false"
          show-icon
        />
        <el-table
          v-if="batchFailed.length"
          :data="batchFailed"
          size="small"
          border
          style="margin-top: 10px"
        >
          <el-table-column prop="id" label="ID" width="90" />
          <el-table-column prop="code" label="code" width="90" />
          <el-table-column prop="message" label="原因" />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false" :disabled="batchRunning">关闭</el-button>
        <el-button
          v-if="!batchRunning && !batchResult"
          type="primary"
          :disabled="batchAction === 'reject' && !batchForm.reason.trim()"
          @click="runBatch"
        >
          开始执行
        </el-button>
        <el-button v-if="batchResult" type="primary" @click="() => { batchVisible = false; reloadQueue() }">
          刷新队列
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { EditPen, QuestionFilled, Refresh } from '@element-plus/icons-vue'
import PageTable from '@/components/PageTable.vue'
import ImagePreview from '@/components/ImagePreview.vue'
import KeyboardHelpModal from '@/components/KeyboardHelpModal.vue'
import RejectPanel from './components/RejectPanel.vue'
import AuditEditForm from './components/AuditEditForm.vue'
import AuditSuggestionPanel from './components/AuditSuggestionPanel.vue'
import SubmitterHistoryDrawer from './components/SubmitterHistoryDrawer.vue'
import { useKeyboard } from '@/composables/useKeyboard'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import {
  fetchAuditQueue,
  fetchAuditDetail,
  lockAuditItem,
  unlockAuditItem,
  passAuditItem,
  rejectAuditItem,
  editAuditItem,
  undoAuditAction,
} from '@/api/audit'
import {
  BATCH_AUDIT_LIMIT,
  LOCK_RENEW_INTERVAL_MS,
  UNDO_WINDOW_MS,
  BREAK_PROMPT_EVERY,
  ERROR_CODES,
} from '@/utils/constants'
import { formatDateTime } from '@/utils/format'

const appStore = useAppStore()
const authStore = useAuthStore()

const activeTab = ref('pending')
const targetType = ref('job')
const mode = ref('card')

const queue = ref([])
const queueLoading = ref(false)
const queueTotal = ref(0)
const queuePage = ref(1)
const queueSize = ref(20)
const selectedRows = ref([])

const currentItem = ref(null)
const detail = ref({})
const detailLoading = ref(false)

const lockRenewTimer = ref(null)
const undoSecondsLeft = ref(0)
const undoTimer = ref(null)
// Captured at action time so Undo still targets the condemned item after
// moveToNext() shifts currentItem. Cleared when the countdown expires or when
// the user successfully undoes.
const undoTarget = ref(null)
const processedCount = ref(0)

const rejectVisible = ref(false)
const editVisible = ref(false)
const helpVisible = ref(false)
const historyVisible = ref(false)

const actionSubmitting = ref(false)

const pendingTotal = computed(() => appStore.pendingCount.total || 0)

// --- batch state ---
const batchVisible = ref(false)
const batchAction = ref('pass')
const batchRunning = ref(false)
const batchResult = ref(null)
const batchSuccess = ref(0)
const batchFailed = ref([])
const batchForm = ref({ reason: '' })
const batchTitle = computed(() =>
  batchAction.value === 'pass' ? '批量通过' : '批量驳回',
)

// --- derived detail displays ---
const rawFields = computed(() => {
  const raw = detail.value.raw_text
  return raw ? { 内容: raw } : {}
})

const images = computed(() => {
  const imgs = detail.value.images || detail.value.photos || []
  return Array.isArray(imgs) ? imgs : []
})

const riskClass = computed(() => {
  const lv = detail.value.risk_level
  if (lv === 'high') return 'jb-risk-high'
  if (lv === 'mid') return 'jb-risk-mid'
  if (lv === 'low') return 'jb-risk-low'
  return ''
})

const submitter7d = computed(() => {
  const h = detail.value.submitter_history_7d || detail.value.submitter_history || []
  return Array.isArray(h) ? h : []
})

const submitterHistoryFull = computed(() => {
  return detail.value.submitter_history_full || submitter7d.value
})

const canPass = computed(() => !!currentItem.value && !!detail.value.version && activeTab.value === 'pending')
const canReject = computed(() => canPass.value)
const canBatch = computed(() => activeTab.value === 'pending' && selectedRows.value.length > 0)

function isSelfLocked(item) {
  const me = authStore.admin?.username
  return !!me && item.locked_by === me
}

async function loadQueue() {
  queueLoading.value = true
  try {
    const data = await fetchAuditQueue({
      status: activeTab.value,
      target_type: targetType.value,
      page: queuePage.value,
      size: queueSize.value,
    })
    queue.value = data.items || []
    queueTotal.value = data.total || 0
  } finally {
    queueLoading.value = false
  }
  appStore.refreshPendingCount()
}

async function reloadQueue() {
  queuePage.value = 1
  await loadQueue()
}

function onTabChange() {
  if (currentItem.value) releaseLock()
  currentItem.value = null
  detail.value = {}
  queuePage.value = 1
  loadQueue()
}

function onTargetTypeChange() {
  if (currentItem.value) releaseLock()
  currentItem.value = null
  detail.value = {}
  queuePage.value = 1
  loadQueue()
}

async function selectItem(item) {
  if (currentItem.value) await releaseLock()
  // Close any dialog tied to the previous item so stale drafts don't leak.
  // NOTE: do NOT touch undoSecondsLeft / undoTarget here — Undo is bound to
  // the last acted-upon item, not the currently-viewed one, and must stay
  // reachable while the 30s window is open, including after moveToNext().
  editVisible.value = false
  rejectVisible.value = false
  historyVisible.value = false
  currentItem.value = item
  detail.value = {}
  await acquireLockAndDetail()
}

async function acquireLockAndDetail() {
  const it = currentItem.value
  if (!it) return
  detailLoading.value = true
  try {
    try {
      await lockAuditItem(it.target_type, it.id)
    } catch (err) {
      if (err && err.code === ERROR_CODES.LOCKED) {
        await ElMessageBox.alert(
          `此条目正在被 ${err.data?.locked_by || '其他管理员'} 处理，请稍后再试`,
          '锁冲突',
          { type: 'warning' },
        )
        currentItem.value = null
        await loadQueue()
        return
      }
      // Network/5xx/other. The interceptor has toasted. Clear currentItem so
      // the detail pane doesn't stick on a phantom selection with no lock.
      currentItem.value = null
      throw err
    }
    startLockRenew()
    const d = await fetchAuditDetail(it.target_type, it.id)
    detail.value = d || {}
  } finally {
    detailLoading.value = false
  }
}

function startLockRenew() {
  stopLockRenew()
  // Snapshot which item this timer is renewing. If the user has since switched
  // to a different item, `clearInterval` can't cancel an already-started async
  // callback — so a renew that was in-flight during releaseLock() would
  // silently re-extend a lock we just freed. Detect that post-hoc and unlock
  // the stale target ourselves.
  lockRenewTimer.value = window.setInterval(async () => {
    const target = currentItem.value
    if (!target) return
    try {
      await lockAuditItem(target.target_type, target.id)
      const now = currentItem.value
      if (!now || now.target_type !== target.target_type || now.id !== target.id) {
        // We accidentally re-locked a stale item. Release it.
        try {
          await unlockAuditItem(target.target_type, target.id)
        } catch (_e) {
          // best-effort — the 300s TTL is the fallback
        }
      }
    } catch (err) {
      if (err && err.code === ERROR_CODES.LOCKED) {
        ElMessage.warning('锁已失效，请刷新队列')
        stopLockRenew()
        currentItem.value = null
        detail.value = {}
        await loadQueue()
      }
    }
  }, LOCK_RENEW_INTERVAL_MS)
}

function stopLockRenew() {
  if (lockRenewTimer.value) {
    clearInterval(lockRenewTimer.value)
    lockRenewTimer.value = null
  }
}

async function releaseLock() {
  stopLockRenew()
  const it = currentItem.value
  if (!it) return
  try {
    await unlockAuditItem(it.target_type, it.id)
  } catch (err) {
    console.warn('unlock failed', err)
  }
}

function startUndoCountdown(target) {
  undoTarget.value = target
  undoSecondsLeft.value = Math.floor(UNDO_WINDOW_MS / 1000)
  if (undoTimer.value) clearInterval(undoTimer.value)
  undoTimer.value = window.setInterval(() => {
    undoSecondsLeft.value -= 1
    if (undoSecondsLeft.value <= 0) {
      clearInterval(undoTimer.value)
      undoTimer.value = null
      undoTarget.value = null
    }
  }, 1000)
}

async function moveToNext() {
  const idx = queue.value.findIndex(
    (x) => currentItem.value && x.id === currentItem.value.id && x.target_type === currentItem.value.target_type,
  )
  const next = queue.value[idx + 1]
  if (next) {
    await selectItem(next)
  } else {
    await releaseLock()
    currentItem.value = null
    detail.value = {}
    ElMessage.success('本页队列已处理完毕')
    loadQueue()
  }
  processedCount.value += 1
  if (processedCount.value % BREAK_PROMPT_EVERY === 0) {
    ElMessage({
      message: `已处理 ${processedCount.value} 条，起身活动一下吧 ☕`,
      type: 'info',
      duration: 4000,
    })
  }
}

async function onPass() {
  if (!canPass.value || actionSubmitting.value) return
  const target = { target_type: currentItem.value.target_type, id: currentItem.value.id }
  actionSubmitting.value = true
  try {
    await passAuditItem(target.target_type, target.id, detail.value.version)
    ElMessage.success('已通过')
    startUndoCountdown(target)
    stopLockRenew()
    await moveToNext()
  } catch (err) {
    handleAuditError(err)
  } finally {
    actionSubmitting.value = false
  }
}

async function onReject(payload) {
  if (!canReject.value || actionSubmitting.value) return
  const target = { target_type: currentItem.value.target_type, id: currentItem.value.id }
  actionSubmitting.value = true
  try {
    await rejectAuditItem(target.target_type, target.id, {
      version: detail.value.version,
      reason: payload.reason,
      notify: payload.notify,
      block_user: payload.block_user,
    })
    ElMessage.success('已驳回')
    rejectVisible.value = false
    startUndoCountdown(target)
    stopLockRenew()
    await moveToNext()
  } catch (err) {
    handleAuditError(err)
  } finally {
    actionSubmitting.value = false
  }
}

async function onEdit(fields) {
  if (!currentItem.value || actionSubmitting.value) return
  actionSubmitting.value = true
  try {
    await editAuditItem(currentItem.value.target_type, currentItem.value.id, {
      version: detail.value.version,
      fields,
    })
    ElMessage.success('已保存')
    editVisible.value = false
    // refresh detail to pick up new version
    const d = await fetchAuditDetail(currentItem.value.target_type, currentItem.value.id)
    detail.value = d || {}
  } catch (err) {
    handleAuditError(err)
  } finally {
    actionSubmitting.value = false
  }
}

async function onUndo() {
  if (!undoTarget.value || undoSecondsLeft.value <= 0) return
  const target = undoTarget.value
  try {
    await undoAuditAction(target.target_type, target.id)
    ElMessage.success(`已撤销 ${target.target_type}#${target.id}`)
    undoSecondsLeft.value = 0
    undoTarget.value = null
    await reloadQueue()
  } catch (err) {
    if (err && err.code === ERROR_CODES.UNDO_EXPIRED) {
      ElMessage.warning('撤销窗口已过期')
      undoSecondsLeft.value = 0
      undoTarget.value = null
    } else {
      handleAuditError(err)
    }
  }
}

function onNext() {
  moveToNext()
}

async function handleAuditError(err) {
  if (!err) return
  if (err.code === ERROR_CODES.VERSION_CONFLICT) {
    await ElMessageBox.alert('此条目已被其他管理员修改，将重新加载最新数据', '版本冲突', {
      type: 'warning',
    })
    const d = await fetchAuditDetail(currentItem.value.target_type, currentItem.value.id)
    detail.value = d || {}
  } else if (err.code === ERROR_CODES.LOCKED) {
    ElMessage.warning(`此条目正在被 ${err.data?.locked_by || '其他人'} 处理`)
    currentItem.value = null
    detail.value = {}
    await reloadQueue()
  } else if (err.code === ERROR_CODES.UNDO_EXPIRED) {
    ElMessage.warning('撤销窗口已过期')
  }
}

// --- batch mode ---
async function onBatch(action) {
  if (activeTab.value !== 'pending') {
    ElMessage.warning('批量操作仅在“待审” tab 可用')
    return
  }
  if (selectedRows.value.length === 0) return
  if (selectedRows.value.length > BATCH_AUDIT_LIMIT) {
    ElMessage.error(`批量操作一次最多 ${BATCH_AUDIT_LIMIT} 条`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认对 ${selectedRows.value.length} 条执行${action === 'pass' ? '通过' : '驳回'}？`,
      '二次确认',
      { type: 'warning' },
    )
  } catch (_e) {
    return
  }
  batchAction.value = action
  batchForm.value.reason = ''
  batchResult.value = null
  batchSuccess.value = 0
  batchFailed.value = []
  batchVisible.value = true
}

async function runBatch() {
  if (batchRunning.value) return
  batchRunning.value = true
  batchSuccess.value = 0
  batchFailed.value = []
  try {
    for (const row of selectedRows.value) {
      let locked = false
      let shouldBreak = false
      try {
        await lockAuditItem(row.target_type, row.id)
        locked = true
        const d = await fetchAuditDetail(row.target_type, row.id)
        const ver = d.version
        if (batchAction.value === 'pass') {
          await passAuditItem(row.target_type, row.id, ver)
        } else {
          await rejectAuditItem(row.target_type, row.id, {
            version: ver,
            reason: batchForm.value.reason,
            notify: true,
            block_user: false,
          })
        }
        batchSuccess.value += 1
      } catch (err) {
        batchFailed.value.push({
          id: `${row.target_type}#${row.id}`,
          code: err?.code || 'ERR',
          message: err?.message || '执行失败',
        })
        shouldBreak = true
      } finally {
        // Always release the lock we acquired — even when the current row
        // failed — otherwise it sits under our admin for the full 300s TTL
        // and blocks everyone else from picking it up.
        if (locked) {
          try {
            await unlockAuditItem(row.target_type, row.id)
          } catch (_e) {
            // unlock failure is non-fatal for batch reporting
          }
        }
      }
      if (shouldBreak) break
    }
  } finally {
    batchRunning.value = false
    batchResult.value = { success: batchSuccess.value, failed: batchFailed.value.length }
  }
}

useKeyboard([
  { key: 'p', handler: onPass, disabled: () => !canPass.value || rejectVisible.value || editVisible.value },
  {
    key: 'r',
    handler: () => {
      if (canReject.value && !rejectVisible.value) rejectVisible.value = true
    },
  },
  { key: 's', handler: onNext, disabled: () => !currentItem.value },
  {
    key: 'e',
    handler: () => {
      if (currentItem.value && !editVisible.value) editVisible.value = true
    },
  },
  {
    key: 'u',
    handler: onUndo,
    disabled: () => undoSecondsLeft.value <= 0 || !undoTarget.value,
  },
  {
    key: '?',
    handler: () => (helpVisible.value = true),
  },
])

onMounted(() => {
  loadQueue()
})

onUnmounted(() => {
  releaseLock()
  if (undoTimer.value) clearInterval(undoTimer.value)
})
</script>

<style scoped>
.audit-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--jb-header-height));
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-mode {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.queue-col {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.queue-head {
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-weight: 600;
}
.queue-list {
  flex: 1;
  overflow: auto;
}
.queue-item {
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: pointer;
}
.queue-item.active {
  background: var(--el-color-primary-light-9);
}
.queue-item.locked {
  opacity: 0.75;
}
.queue-item-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.queue-item-sub {
  font-size: 12px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-lock-hint {
  font-size: 12px;
  margin-top: 2px;
}
.queue-empty {
  padding: 24px 0;
}
.queue-loading {
  height: 200px;
}

.detail-col {
  overflow: auto;
  display: flex;
  flex-direction: column;
}
.empty-detail {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.detail-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.detail-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 14px 16px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.detail-id {
  font-weight: 600;
  margin-left: 6px;
}
.submitter-bar {
  font-size: 13px;
}
.section-title {
  font-weight: 600;
  margin-bottom: 6px;
  font-size: 14px;
}
.raw-content {
  max-height: 280px;
  overflow: auto;
  background: var(--el-fill-color-lighter);
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 13px;
}
.raw-row {
  line-height: 1.7;
}
.action-bar {
  display: flex;
  gap: 10px;
  padding: 10px 14px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

</style>
