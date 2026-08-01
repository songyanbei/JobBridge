<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">
        推荐策略
        <el-tag v-if="roleLoaded" size="small" :type="roleTagType">{{ roleLabel }}</el-tag>
        <el-tag v-else size="small" type="danger">权限未知（按最小权限展示）</el-tag>
      </div>
      <div>
        <el-button :icon="Refresh" :loading="loading" @click="refreshAll">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="conflictMessage"
      type="warning"
      show-icon
      :closable="false"
      class="page-alert"
      title="版本已被其他管理员修改"
    >
      <template #default>
        <div>{{ conflictMessage }}</div>
        <div>
          后台对策略版本和 release 都使用乐观锁，你手上的 lock_version 已经过期。请先刷新拿到最新版本，确认对方的改动后再重新提交，避免覆盖他人操作。
        </div>
        <el-button type="primary" size="small" class="alert-action" @click="refreshAll">
          刷新并重新加载
        </el-button>
      </template>
    </el-alert>

    <el-alert
      v-if="errorMessage"
      type="error"
      show-icon
      class="page-alert"
      :title="errorMessage"
      @close="errorMessage = ''"
    />

    <!-- ---------------------------------------------------------------- -->
    <!-- 总开关（§7.5）                                                     -->
    <!-- ---------------------------------------------------------------- -->
    <div v-loading="runtimeLoading" class="jb-card kill-card">
      <div class="card-head">
        <span class="card-title">推荐总开关（kill switch）</span>
        <el-tag size="small" :type="killOn ? 'danger' : 'success'">
          {{ killOn ? '已开启：全部推荐强制走 legacy' : '未开启：按各方向的执行模式运行' }}
        </el-tag>
      </div>
      <div class="kill-row">
        <el-switch
          :model-value="killOn"
          :disabled="!canKillSwitch || !runtimeControl"
          active-text="开启 kill"
          inactive-text="关闭 kill"
          @change="onKillToggle"
        />
        <span v-if="!canKillSwitch" class="jb-muted">仅 super_admin 可以切换总开关。</span>
        <span v-else-if="!runtimeControl" class="jb-muted">控制面尚未初始化，请先刷新。</span>
      </div>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="DB revision">
          <span class="mono">{{ runtimeControl?.revision ?? '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="最后更新人">{{ runtimeControl?.updated_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="最后更新时间">
          {{ runtimeControl?.updated_at ? formatDateTime(runtimeControl.updated_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="最后修改原因" :span="3">
          {{ runtimeControl?.change_reason || '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="本进程当前值">
          {{ localControl ? (localControl.kill_switch ? '开启' : '关闭') : '未知' }}
          <span v-if="localControl" class="jb-muted">
            （来源 {{ localControl.source }}，{{ localControl.age_seconds }}s 前校验，{{ localControl.fresh ? '新鲜' : '已过期' }}）
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="环境变量 override">
          {{ runtime?.env_override === null || runtime?.env_override === undefined ? '未设置' : (runtime.env_override ? 'true' : 'false') }}
        </el-descriptions-item>
        <el-descriptions-item label="最大生效时间">
          <span class="mono">{{ maxPropagationSeconds }} 秒</span>
        </el-descriptions-item>
      </el-descriptions>
      <div class="jb-muted kill-note">
        切换后最大生效时间为 {{ maxPropagationSeconds }} 秒：先提交 DB revision，再广播到各进程，Pub/Sub 丢失时由 DB 轮询收敛。本页不承诺「立即生效」。
      </div>
      <div class="jb-muted kill-note">
        环境变量 <span class="mono">RECOMMENDATION_STRATEGY_KILL_SWITCH</span> 只是进程启动时的更强 override，修改 .env 需要滚动重启全部 App/Worker，不能当作在线开关；环境变量 false 也不能覆盖 DB 的 true。
      </div>
    </div>

    <!-- ---------------------------------------------------------------- -->
    <!-- 方向                                                              -->
    <!-- ---------------------------------------------------------------- -->
    <el-tabs v-model="direction" class="direction-tabs" @tab-change="onDirectionChange">
      <el-tab-pane
        v-for="item in STRATEGY_DIRECTIONS"
        :key="item.value"
        :label="item.label"
        :name="item.value"
      />
    </el-tabs>

    <div v-loading="loading" class="jb-card release-card">
      <div class="card-head">
        <span class="card-title">当前发布状态</span>
        <el-tag size="small" :type="modeTagType">{{ executionModeLabel }}</el-tag>
        <el-tag size="small" type="info">灰度 {{ release?.rollout_percentage ?? 0 }}%</el-tag>
        <el-tag v-if="!release?.initialized" size="small" type="warning">尚未初始化基线</el-tag>
      </div>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="稳定版本">
          <span v-if="stableVersion" class="mono">
            v{{ stableVersion.version_no }}（id={{ stableVersion.id }}）
          </span>
          <span v-else class="jb-muted">legacy（尚无稳定版本）</span>
        </el-descriptions-item>
        <el-descriptions-item label="候选版本">
          <span v-if="candidateVersion" class="mono">
            v{{ candidateVersion.version_no }}（id={{ candidateVersion.id }}）
          </span>
          <span v-else class="jb-muted">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="当前模板">
          {{ templateLabel(stableVersion?.template_key || candidateVersion?.template_key) }}
        </el-descriptions-item>
        <el-descriptions-item label="release revision">
          <span class="mono">{{ release?.revision ?? 0 }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="最近修改人">{{ release?.updated_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="最近修改时间">
          {{ release?.updated_at ? formatDateTime(release.updated_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="稳定版本发布人">{{ stableVersion?.published_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="稳定版本发布时间">
          {{ stableVersion?.published_at ? formatDateTime(stableVersion.published_at) : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="候选版本发布时间">
          {{ candidateVersion?.published_at ? formatDateTime(candidateVersion.published_at) : '—' }}
        </el-descriptions-item>
      </el-descriptions>

      <div class="action-bar">
        <el-button
          type="primary"
          :disabled="!canPublishCandidate"
          @click="openPublish"
        >
          发布为候选版本
        </el-button>
        <el-button :disabled="!canRollout" @click="openRollout">灰度发布</el-button>
        <el-button :disabled="!canPromote" @click="promoteVisible = true">全量发布</el-button>
        <el-button type="danger" plain :disabled="!canRollback" @click="openRollback">回滚</el-button>
      </div>
      <div class="action-hints">
        <div v-if="!canEditDraft" class="jb-muted">
          当前账号不能编辑草稿（需要 operator 及以上），「策略配置」页为只读，但仍可运行模拟。
        </div>
        <div v-if="!canPublish" class="jb-muted">
          当前账号不能发布、调整灰度、全量发布或回滚（需要 super_admin），相关按钮已禁用。
        </div>
        <div v-if="publishBlockedReason" class="jb-warning-text">发布为候选版本：{{ publishBlockedReason }}</div>
        <div v-if="promoteBlockedReason" class="jb-warning-text">全量发布：{{ promoteBlockedReason }}</div>
        <div class="jb-muted">
          §7.5 固定步骤：off → shadow 5% → shadow 25% → shadow 100% → on 5% → on 25% → on 50% → on 100% → 全量发布。「灰度 0%」不是影子验证，只表示候选版本不执行。
        </div>
      </div>
    </div>

    <el-tabs v-model="subTab" class="sub-tabs">
      <el-tab-pane label="策略配置" name="editor">
        <StrategyEditor
          :key="`editor-${direction}`"
          :direction="direction"
          :versions="versions"
          :release="release"
          :can-edit="canEditDraft"
          @saved="onDraftSaved"
          @failed="onChildFailed"
          @state-change="onEditorStateChange"
        />
      </el-tab-pane>
      <el-tab-pane label="模拟对比" name="simulation">
        <StrategySimulation
          :key="`sim-${direction}`"
          :direction="direction"
          :draft="draftVersion"
          @simulated="loadDirection"
          @failed="onChildFailed"
        />
      </el-tab-pane>
      <el-tab-pane label="版本历史" name="history">
        <StrategyVersionHistory
          :history="history"
          :versions="versions"
          :release="release"
          :current-revision="release?.revision || 0"
          :can-rollback="canRollback"
          :loading="loading || historyLoading"
          @rollback="onRollbackFromHistory"
        />
      </el-tab-pane>
      <el-tab-pane label="效果指标" name="metrics">
        <StrategyMetrics :key="`metrics-${direction}`" :direction="direction" />
      </el-tab-pane>
    </el-tabs>

    <!-- ---------------------------------------------------------------- -->
    <!-- 二次确认弹窗                                                       -->
    <!-- ---------------------------------------------------------------- -->
    <ConfirmAction
      v-model="publishVisible"
      title="发布为候选版本"
      require-reason
      :submitting="submitting"
      confirm-text="确认发布"
      :message="publishMessage"
      @confirm="onPublishConfirm"
    />

    <ConfirmAction
      v-model="rolloutVisible"
      title="灰度发布"
      require-reason
      :submitting="submitting"
      confirm-text="确认调整"
      message="调整执行模式或灰度比例会改变线上分流。已生成的搜索快照不受影响，新搜索按新配置执行。"
      @confirm="onRolloutConfirm"
    >
      <el-form :model="rolloutForm" label-position="top">
        <el-form-item label="执行模式">
          <el-select v-model="rolloutForm.execution_mode" style="width: 100%">
            <el-option
              v-for="opt in EXECUTION_MODE_OPTIONS"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="候选版本（off 以外必选，且必须是已发布版本）">
          <el-select
            v-model="rolloutForm.candidate_version_id"
            clearable
            style="width: 100%"
            :disabled="rolloutForm.execution_mode === 'off'"
          >
            <el-option
              v-for="item in publishedVersions"
              :key="item.id"
              :label="`v${item.version_no}（id=${item.id}）· ${templateLabel(item.template_key)}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="灰度比例（%）">
          <el-radio-group v-model="rolloutForm.rollout_percentage" size="small">
            <el-radio-button v-for="step in ROLLOUT_STEPS" :key="step" :label="step">
              {{ step }}%
            </el-radio-button>
          </el-radio-group>
          <el-input-number
            v-model="rolloutForm.rollout_percentage"
            :min="0"
            :max="100"
            :step="1"
            style="width: 130px; margin-top: 8px"
          />
        </el-form-item>
      </el-form>
    </ConfirmAction>

    <ConfirmAction
      v-model="promoteVisible"
      title="全量发布"
      require-reason
      :submitting="submitting"
      confirm-text="确认全量发布"
      :message="promoteMessage"
      @confirm="onPromoteConfirm"
    />

    <ConfirmAction
      v-model="rollbackVisible"
      title="回滚"
      require-reason
      :submitting="submitting"
      confirm-text="确认回滚"
      message="回滚必须显式选择一个历史 revision，系统会把该快照原样复制成一个更高的新 revision，不是「退回上一步」。"
      @confirm="onRollbackConfirm"
    >
      <el-form :model="rollbackForm" label-position="top">
        <el-form-item label="目标 revision（必选）">
          <el-select v-model="rollbackForm.target_revision" filterable style="width: 100%">
            <el-option
              v-for="row in history"
              :key="row.revision"
              :label="revisionLabel(row)"
              :value="row.revision"
              :disabled="row.revision === release?.revision"
            />
          </el-select>
        </el-form-item>
      </el-form>
    </ConfirmAction>

    <ConfirmAction
      v-model="killVisible"
      :title="killTarget ? '开启推荐总开关' : '关闭推荐总开关'"
      require-reason
      :submitting="submitting"
      :confirm-text="killTarget ? '确认开启 kill' : '确认关闭 kill'"
      :message="killMessage"
      @confirm="onKillConfirm"
    />
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import ConfirmAction from '@/components/ConfirmAction.vue'
import StrategyEditor from '@/components/recommendation/StrategyEditor.vue'
import StrategySimulation from '@/components/recommendation/StrategySimulation.vue'
import StrategyVersionHistory from '@/components/recommendation/StrategyVersionHistory.vue'
import StrategyMetrics from '@/components/recommendation/StrategyMetrics.vue'
import {
  ADMIN_ROLE_LABEL,
  EXECUTION_MODE_LABEL,
  EXECUTION_MODE_OPTIONS,
  ROLLOUT_STEPS,
  STRATEGY_DIRECTIONS,
  apiErrorMessage,
  getRecommendationReleaseHistory,
  getRecommendationRoleMe,
  getRecommendationRuntimeControl,
  getRecommendationStrategy,
  isConflictError,
  promoteRecommendationRelease,
  publishRecommendationCandidate,
  rollbackRecommendationRelease,
  templateLabel,
  updateRecommendationKillSwitch,
  updateRecommendationRelease,
} from '@/api/recommendationStrategies'
import { formatDateTime } from '@/utils/format'

const direction = ref('search_job')
const subTab = ref('editor')

const loading = ref(false)
const historyLoading = ref(false)
const runtimeLoading = ref(false)
const submitting = ref(false)

const release = ref(null)
const versions = ref([])
const history = ref([])
const runtime = ref(null)

const errorMessage = ref('')
const conflictMessage = ref('')
const editorState = ref({ dirty: false, weightValid: true, weightSum: 100 })

// §9.10 RBAC：/admin/me 不返回 role，必须走 /admin/recommendation-roles/me。
// 后端已经是 fail-closed，前端同样按最小权限兜底：拉取失败就当没有任何写权限。
const role = ref('')
const permissions = ref([])
const roleLoaded = ref(false)

const publishVisible = ref(false)
const rolloutVisible = ref(false)
const promoteVisible = ref(false)
const rollbackVisible = ref(false)
const killVisible = ref(false)
const killTarget = ref(false)

const rolloutForm = reactive({
  execution_mode: 'shadow',
  candidate_version_id: null,
  rollout_percentage: 5,
})
const rollbackForm = reactive({ target_revision: null })

// ---------------------------------------------------------------------------
// 派生状态
// ---------------------------------------------------------------------------

const roleLabel = computed(() => ADMIN_ROLE_LABEL[role.value] || role.value || '未知角色')
const roleTagType = computed(() => {
  if (role.value === 'super_admin') return 'danger'
  if (role.value === 'operator') return 'warning'
  return 'info'
})

function can(permission) {
  return permissions.value.includes(permission)
}

const canEditDraft = computed(() => can('strategy_draft_edit'))
const canPublish = computed(() => can('strategy_publish'))
const canRollout = computed(() => can('strategy_rollout'))
const canKillSwitch = computed(() => can('strategy_kill_switch'))

const draftVersion = computed(() => versions.value.find((item) => item.status === 'draft') || null)
const publishedVersions = computed(() => versions.value.filter((item) => item.status === 'published'))
const stableVersion = computed(() => {
  const id = release.value?.stable_version_id
  return id ? versions.value.find((item) => item.id === id) || null : null
})
const candidateVersion = computed(() => {
  const id = release.value?.candidate_version_id
  return id ? versions.value.find((item) => item.id === id) || null : null
})

const executionModeLabel = computed(
  () => EXECUTION_MODE_LABEL[release.value?.execution_mode] || 'off · 只走 legacy',
)
const modeTagType = computed(() => {
  const mode = release.value?.execution_mode
  if (mode === 'on') return 'success'
  if (mode === 'shadow') return 'warning'
  return 'info'
})

const draftSimulated = computed(() => {
  const draft = draftVersion.value
  return !!draft && !!draft.last_simulated_digest && draft.last_simulated_digest === draft.parameters_digest
})

const publishBlockedReason = computed(() => {
  if (!canPublish.value) return ''
  if (!draftVersion.value) return '当前方向没有草稿版本，请先在「策略配置」页保存草稿。'
  if (!editorState.value.weightValid) {
    return `四项权重合计为 ${editorState.value.weightSum}，必须等于 100。`
  }
  if (editorState.value.dirty) return '「策略配置」页存在未保存的修改，请先保存草稿。'
  if (!draftSimulated.value) return '草稿在最后一次修改后还没有完成模拟，请到「模拟对比」页重新运行一次模拟。'
  if (!release.value?.initialized) return 'release 基线尚未初始化，请先刷新页面。'
  return ''
})

const canPublishCandidate = computed(() => canPublish.value && !publishBlockedReason.value)

const promoteBlockedReason = computed(() => {
  if (!can('strategy_promote')) return ''
  if (!release.value?.candidate_version_id) return '当前没有候选版本。'
  if (release.value.execution_mode !== 'on' || Number(release.value.rollout_percentage) !== 100) {
    return '全量发布要求候选版本先处于 on 且灰度比例 100%。'
  }
  return ''
})

const canPromote = computed(() => can('strategy_promote') && !promoteBlockedReason.value)
const canRollback = computed(() => can('strategy_rollback') && history.value.length > 0)

const runtimeControl = computed(() => runtime.value?.control || null)
const localControl = computed(() => runtime.value?.local || null)
const killOn = computed(() => !!runtimeControl.value?.kill_switch)
const maxPropagationSeconds = computed(() => runtime.value?.max_propagation_seconds ?? 5)

const publishMessage = computed(() => {
  const draft = draftVersion.value
  if (!draft) return ''
  return `将草稿 v${draft.version_no}（id=${draft.id}）发布为不可变的已发布版本。发布本身不会改变线上分流，还需要单独执行「灰度发布」把它设为候选版本。`
})

const promoteMessage = computed(() => {
  const candidate = candidateVersion.value
  const stableText = stableVersion.value ? `v${stableVersion.value.version_no}` : 'legacy'
  return `将候选版本${candidate ? ` v${candidate.version_no}（id=${candidate.id}）` : ''}升级为稳定版本，原稳定版本（${stableText}）会被归档，候选指针会被清空。`
})

const killMessage = computed(() =>
  killTarget.value
    ? `开启后所有方向都不再执行 shadow/on 分配，全部推荐回到 legacy；已发出的 v1 快照在 show_more 时也会失效并按 legacy 重建。最大生效时间 ${maxPropagationSeconds.value} 秒。`
    : `关闭后各方向恢复按自己的执行模式和灰度比例运行。最大生效时间 ${maxPropagationSeconds.value} 秒。`,
)

// ---------------------------------------------------------------------------
// 错误处理（P2-25：拦截器 reject 的是 {code, message}，不是 axios error）
// ---------------------------------------------------------------------------

function handleError(err, fallback) {
  if (isConflictError(err)) {
    conflictMessage.value = apiErrorMessage(err, '当前数据已被其他管理员修改')
    ElMessage.warning('数据已被其他管理员修改，请刷新后重试')
    return
  }
  errorMessage.value = apiErrorMessage(err, fallback)
}

function onChildFailed(payload) {
  handleError(payload?.err, payload?.fallback || '操作失败')
}

// ---------------------------------------------------------------------------
// 加载
// ---------------------------------------------------------------------------

async function loadRole() {
  try {
    const data = await getRecommendationRoleMe()
    role.value = data?.role || ''
    permissions.value = Array.isArray(data?.permissions) ? data.permissions : []
    roleLoaded.value = true
  } catch (err) {
    role.value = ''
    permissions.value = []
    roleLoaded.value = false
    errorMessage.value = apiErrorMessage(err, '无法获取当前账号的推荐策略角色，已按最小权限展示')
  }
}

async function loadDirection() {
  loading.value = true
  try {
    const data = await getRecommendationStrategy(direction.value)
    release.value = data?.release || null
    versions.value = data?.versions || []
  } catch (err) {
    release.value = null
    versions.value = []
    handleError(err, '策略加载失败')
  } finally {
    loading.value = false
  }
  await loadHistory()
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const data = await getRecommendationReleaseHistory(direction.value)
    history.value = data?.history || []
  } catch (err) {
    history.value = []
    handleError(err, '发布历史加载失败')
  } finally {
    historyLoading.value = false
  }
}

async function loadRuntime() {
  runtimeLoading.value = true
  try {
    runtime.value = await getRecommendationRuntimeControl()
  } catch (err) {
    runtime.value = null
    handleError(err, '总开关状态加载失败')
  } finally {
    runtimeLoading.value = false
  }
}

async function refreshAll() {
  conflictMessage.value = ''
  errorMessage.value = ''
  await loadRole()
  await Promise.all([loadDirection(), loadRuntime()])
}

function onDirectionChange() {
  conflictMessage.value = ''
  errorMessage.value = ''
  editorState.value = { dirty: false, weightValid: true, weightSum: 100 }
  loadDirection()
}

function onEditorStateChange(state) {
  editorState.value = { ...editorState.value, ...(state || {}) }
}

function onDraftSaved() {
  loadDirection()
}

// ---------------------------------------------------------------------------
// 写操作
// ---------------------------------------------------------------------------

function openPublish() {
  if (!canPublishCandidate.value) return
  publishVisible.value = true
}

async function onPublishConfirm({ reason }) {
  const draft = draftVersion.value
  if (!draft || !release.value) return
  submitting.value = true
  try {
    await publishRecommendationCandidate(draft.id, {
      lock_version: draft.lock_version,
      release_lock_version: release.value.lock_version,
      change_reason: reason,
    })
    ElMessage.success('已发布为不可变版本')
    publishVisible.value = false
    await loadDirection()
  } catch (err) {
    handleError(err, '发布候选版本失败')
  } finally {
    submitting.value = false
  }
}

function openRollout() {
  if (!canRollout.value) return
  rolloutForm.execution_mode = release.value?.execution_mode || 'off'
  rolloutForm.candidate_version_id =
    release.value?.candidate_version_id || publishedVersions.value[0]?.id || null
  rolloutForm.rollout_percentage = Number(release.value?.rollout_percentage ?? 0)
  rolloutVisible.value = true
}

async function onRolloutConfirm({ reason }) {
  if (!release.value) return
  if (rolloutForm.execution_mode !== 'off' && !rolloutForm.candidate_version_id) {
    ElMessage.error('shadow / on 模式必须选择一个已发布的候选版本')
    return
  }
  submitting.value = true
  try {
    await updateRecommendationRelease(direction.value, {
      execution_mode: rolloutForm.execution_mode,
      candidate_version_id:
        rolloutForm.execution_mode === 'off' ? null : rolloutForm.candidate_version_id,
      rollout_percentage: Number(rolloutForm.rollout_percentage) || 0,
      lock_version: release.value.lock_version,
      change_reason: reason,
    })
    ElMessage.success('灰度配置已更新')
    rolloutVisible.value = false
    await loadDirection()
  } catch (err) {
    handleError(err, '灰度发布失败')
  } finally {
    submitting.value = false
  }
}

async function onPromoteConfirm({ reason }) {
  if (!release.value) return
  submitting.value = true
  try {
    await promoteRecommendationRelease(direction.value, {
      lock_version: release.value.lock_version,
      change_reason: reason,
    })
    ElMessage.success('候选版本已升级为稳定版本')
    promoteVisible.value = false
    await loadDirection()
  } catch (err) {
    handleError(err, '全量发布失败')
  } finally {
    submitting.value = false
  }
}

function openRollback() {
  if (!canRollback.value) return
  rollbackForm.target_revision = null
  rollbackVisible.value = true
}

function onRollbackFromHistory(revision) {
  if (!canRollback.value) return
  rollbackForm.target_revision = revision
  rollbackVisible.value = true
}

async function onRollbackConfirm({ reason }) {
  if (!release.value) return
  if (!rollbackForm.target_revision) {
    ElMessage.error('请先选择要回滚到的 target_revision')
    return
  }
  submitting.value = true
  try {
    await rollbackRecommendationRelease(direction.value, {
      target_revision: Number(rollbackForm.target_revision),
      lock_version: release.value.lock_version,
      change_reason: reason,
    })
    ElMessage.success(`已回滚到 revision ${rollbackForm.target_revision}，并生成了新的 revision`)
    rollbackVisible.value = false
    await loadDirection()
  } catch (err) {
    handleError(err, '回滚失败')
  } finally {
    submitting.value = false
  }
}

function onKillToggle(next) {
  if (!canKillSwitch.value || !runtimeControl.value) return
  killTarget.value = !!next
  killVisible.value = true
}

async function onKillConfirm({ reason }) {
  if (!runtimeControl.value) return
  submitting.value = true
  try {
    const data = await updateRecommendationKillSwitch({
      enabled: killTarget.value,
      lock_version: runtimeControl.value.lock_version,
      change_reason: reason,
    })
    const seconds = data?.max_propagation_seconds ?? maxPropagationSeconds.value
    ElMessage.success(
      data?.broadcast
        ? `总开关已提交并广播，最大生效时间 ${seconds} 秒`
        : `总开关已提交，广播通道不可用，各进程最迟 ${seconds} 秒后由 DB 轮询收敛`,
    )
    killVisible.value = false
    await loadRuntime()
  } catch (err) {
    handleError(err, '切换总开关失败')
  } finally {
    submitting.value = false
  }
}

function revisionLabel(row) {
  const stable = row.stable_version_id ?? 'legacy'
  const candidate = row.candidate_version_id ?? '无候选'
  return `revision ${row.revision} · ${row.operation} · ${row.execution_mode} ${row.rollout_percentage ?? 0}% · stable=${stable} / candidate=${candidate} · ${formatDateTime(row.created_at)}`
}

refreshAll()
</script>

<style scoped>
.page-alert {
  margin-bottom: 14px;
}
.alert-action {
  margin-top: 8px;
}
.jb-card {
  margin-bottom: 14px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.card-title {
  font-weight: 600;
}
.kill-row {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.kill-note {
  margin-top: 8px;
  font-size: 12.5px;
  line-height: 1.7;
}
.direction-tabs {
  margin-bottom: 4px;
}
.action-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}
.action-hints {
  margin-top: 10px;
  display: grid;
  gap: 4px;
  font-size: 12.5px;
  line-height: 1.7;
}
.sub-tabs {
  background: var(--panel, var(--el-bg-color));
  border: 1px solid var(--line, var(--el-border-color-lighter));
  border-radius: 8px;
  padding: 8px 16px 16px;
}
</style>
