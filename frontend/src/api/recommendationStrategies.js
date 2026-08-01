import request from './request'

const STRATEGY_BASE = '/admin/recommendation-strategies'
const RUNTIME_BASE = '/admin/recommendation-runtime-control'
const ROLES_BASE = '/admin/recommendation-roles'
const METRICS_BASE = '/admin/recommendation-metrics'

// ---------------------------------------------------------------------------
// 只读
// ---------------------------------------------------------------------------

export const listRecommendationStrategies = () => request.get(STRATEGY_BASE)
export const getRecommendationStrategy = (direction) =>
  request.get(`${STRATEGY_BASE}/${direction}`)
export const listRecommendationVersions = (direction) =>
  request.get(`${STRATEGY_BASE}/${direction}/versions`)
export const getRecommendationReleaseHistory = (direction) =>
  request.get(`${STRATEGY_BASE}/${direction}/history`)

// ---------------------------------------------------------------------------
// 草稿与模拟
// ---------------------------------------------------------------------------

export const createRecommendationDraft = (direction, payload) =>
  request.post(`${STRATEGY_BASE}/${direction}/drafts`, payload)
export const updateRecommendationDraft = (versionId, payload) =>
  request.put(`${STRATEGY_BASE}/drafts/${versionId}`, payload)
export const simulateRecommendationDraft = (versionId, payload) =>
  request.post(`${STRATEGY_BASE}/drafts/${versionId}/simulate`, payload)

// ---------------------------------------------------------------------------
// 发布 / 灰度 / 全量 / 回滚
// ---------------------------------------------------------------------------

export const publishRecommendationCandidate = (versionId, payload) =>
  request.post(`${STRATEGY_BASE}/drafts/${versionId}/publish-candidate`, payload)
export const updateRecommendationRelease = (direction, payload) =>
  request.put(`${STRATEGY_BASE}/${direction}/release`, payload)
export const promoteRecommendationRelease = (direction, payload) =>
  request.post(`${STRATEGY_BASE}/${direction}/promote`, payload)
export const rollbackRecommendationRelease = (direction, payload) =>
  request.post(`${STRATEGY_BASE}/${direction}/rollback`, payload)

// ---------------------------------------------------------------------------
// 总开关（§7.5）—— 独立 router，不挂在 recommendation-strategies 下
// ---------------------------------------------------------------------------

export const getRecommendationRuntimeControl = () => request.get(RUNTIME_BASE)
export const updateRecommendationKillSwitch = (payload) =>
  request.put(`${RUNTIME_BASE}/kill-switch`, payload)

// ---------------------------------------------------------------------------
// 控制台 RBAC（§9.10）—— /admin/me 不返回 role，必须单独取
// ---------------------------------------------------------------------------

export const getRecommendationRoleMe = () => request.get(`${ROLES_BASE}/me`)

// ---------------------------------------------------------------------------
// 指标（§11.9）
// ---------------------------------------------------------------------------

export const getRecommendationMetrics = (params) => request.get(METRICS_BASE, { params })
export const getRecommendationExposureDaily = (params) =>
  request.get(`${METRICS_BASE}/exposure-daily`, { params })

// ---------------------------------------------------------------------------
// 错误契约
//
// `@/api/request.js` 的响应拦截器对业务失败 reject 的是**后端响应体本身**
// （`{ code, message, data }`，HTTP 状态码始终是 200），网络异常才 reject
// axios Error。两者都没有 `response.data.detail`，读它永远拿到 undefined。
// ---------------------------------------------------------------------------

/** 乐观锁 / 并发冲突码，拦截器对这几个码静默 reject，由调用方自行提示。 */
export const CONFLICT_CODES = [40901, 40902, 40903]

export function apiErrorCode(err) {
  return typeof err?.code === 'number' ? err.code : null
}

export function apiErrorMessage(err, fallback = '操作失败') {
  if (!err) return fallback
  if (typeof err === 'string') return err.trim() || fallback
  const message = err.message
  if (typeof message === 'string' && message.trim()) return message.trim()
  return fallback
}

export function isConflictError(err) {
  return CONFLICT_CODES.includes(apiErrorCode(err))
}

// ---------------------------------------------------------------------------
// 策略常量（§5.2 / §5.3）
// ---------------------------------------------------------------------------

export const STRATEGY_DIRECTIONS = [
  { label: '岗位推荐', value: 'search_job', targetLabel: '岗位' },
  { label: '工人推荐', value: 'search_worker', targetLabel: '简历' },
]

/** §5.2 官方模板与建议默认值。四项权重之和恒为 100。 */
export const STRATEGY_TEMPLATES = [
  {
    key: 'balanced',
    label: '综合均衡',
    description: '默认模板，兼顾匹配、信息质量、新鲜度和召回池内曝光分布。',
    parameters: {
      match_weight: 70,
      quality_weight: 10,
      freshness_weight: 8,
      exposure_weight: 12,
      diversity_level: 'medium',
      exploration_percentage: 20,
      repeat_cooldown_hours: 24,
      same_owner_top_n_limit: 1,
    },
  },
  {
    key: 'match_first',
    label: '匹配优先',
    description: '适用于用户条件明确、候选质量差异较大的场景，降低探索和曝光调整。',
    parameters: {
      match_weight: 85,
      quality_weight: 8,
      freshness_weight: 5,
      exposure_weight: 2,
      diversity_level: 'low',
      exploration_percentage: 5,
      repeat_cooldown_hours: 12,
      same_owner_top_n_limit: 2,
    },
  },
  {
    key: 'exposure_balanced',
    label: '曝光均衡',
    description: '适用于候选较多、头部集中明显的场景，加强去重、低曝光候选机会和多样性。',
    parameters: {
      match_weight: 65,
      quality_weight: 10,
      freshness_weight: 5,
      exposure_weight: 20,
      diversity_level: 'high',
      exploration_percentage: 30,
      repeat_cooldown_hours: 72,
      same_owner_top_n_limit: 1,
    },
  },
]

export const DIVERSITY_LEVEL_OPTIONS = [
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
]

/** 求和必须等于 100 的四项权重（§5.3 强校验）。 */
export const WEIGHT_KEYS = [
  'match_weight',
  'quality_weight',
  'freshness_weight',
  'exposure_weight',
]

/** §5.3 后台允许调整的八项参数，顺序即页面展示顺序。 */
export const STRATEGY_PARAM_META = [
  {
    key: 'match_weight',
    label: '匹配质量权重',
    type: 'int',
    min: 60,
    max: 85,
    unit: '',
    desc: '候选与用户条件的匹配质量在综合分中的占比。',
    risk: '调高会更贴合用户条件，但头部集中度上升、长尾候选更难获得曝光。',
  },
  {
    key: 'quality_weight',
    label: '信息质量权重',
    type: 'int',
    min: 5,
    max: 15,
    unit: '',
    desc: '信息完整度和有效性在综合分中的占比。',
    risk: '调高会偏向信息填写完整的老发布者，新发布者更难冒头。',
  },
  {
    key: 'freshness_weight',
    label: '新鲜度权重',
    type: 'int',
    min: 0,
    max: 15,
    unit: '',
    desc: '对新发布内容的轻量扶持。',
    risk: '调高会加快内容轮换，但可能把匹配度更好的老候选挤出 Top 3。',
  },
  {
    key: 'exposure_weight',
    label: '曝光机会权重',
    type: 'int',
    min: 0,
    max: 20,
    unit: '',
    desc: '对召回池内低曝光候选的机会补偿。',
    risk: '调高能压平曝光分布，但会牺牲部分匹配精度。',
  },
  {
    key: 'diversity_level',
    label: '多样性强度',
    type: 'enum',
    options: DIVERSITY_LEVEL_OPTIONS,
    desc: '多样性约束的惩罚强度（低 / 中 / 高）。',
    risk: '设为高时同城市、同工种、同主体的候选会被更强打散，候选不足时约束会降级。',
  },
  {
    key: 'exploration_percentage',
    label: '探索请求比例',
    type: 'int',
    min: 0,
    max: 30,
    unit: '%',
    desc: '有多少比例的搜索请求启用一个探索位。',
    risk: '调高会让更多请求的第 3 位让给探索候选，短期匹配满意度可能下降。',
  },
  {
    key: 'repeat_cooldown_hours',
    label: '重复曝光冷却',
    type: 'int',
    min: 0,
    max: 168,
    unit: '小时',
    desc: '同一用户近期已看过的候选在该窗口内降权。',
    risk: '窗口过长会在候选池较小的城市/工种导致可推荐候选不足。',
  },
  {
    key: 'same_owner_top_n_limit',
    label: '同主体 Top 3 上限',
    type: 'int',
    min: 1,
    max: 3,
    unit: '条',
    desc: 'Top 3 中同一发布主体（owner_userid）最多出现的条数。',
    risk: '放宽到 2~3 会让单个企业/中介占据大部分展示位。',
  },
]

export const EXECUTION_MODE_OPTIONS = [
  { label: 'off（只走 legacy）', value: 'off' },
  { label: 'shadow（双算，仍发 legacy）', value: 'shadow' },
  { label: 'on（候选版本对外生效）', value: 'on' },
]

export const EXECUTION_MODE_LABEL = {
  off: 'off · 只走 legacy',
  shadow: 'shadow · 双算不外发',
  on: 'on · 候选对外生效',
}

/** §7.5 首次上线固定步骤对应的比例档位。 */
export const ROLLOUT_STEPS = [0, 5, 25, 50, 100]

export const VERSION_STATUS_LABEL = {
  draft: '草稿',
  published: '已发布',
  archived: '已归档',
}

export const RELEASE_OPERATION_LABEL = {
  init: '初始化',
  publish_candidate: '发布候选版本',
  mode_change: '切换执行模式',
  rollout: '调整灰度比例',
  promote: '全量发布',
  rollback: '回滚',
}

export const ADMIN_ROLE_LABEL = {
  viewer: '只读（viewer）',
  operator: '运营（operator）',
  super_admin: '超级管理员（super_admin）',
}

/** §8.2 排名变化方向。 */
export const MOVEMENT_LABEL = {
  up: '升位',
  down: '降位',
  entered: '新进入',
  left: '被挤出',
  unchanged: '不变',
}

/**
 * §8.2 排名变化原因码 + 打分链路自带的候选原因码。
 * 未收录的码原样展示，避免后端新增码时前端静默吞掉信息。
 */
export const REASON_CODE_LABEL = {
  rank_up: '位次上升',
  rank_down: '位次下降',
  entered_top_n: '进入 Top N',
  left_top_n: '退出 Top N',
  match_up: '匹配分升高',
  match_down: '匹配分降低',
  quality_up: '信息质量分升高',
  quality_down: '信息质量分降低',
  freshness_up: '新鲜度分升高',
  freshness_down: '新鲜度分降低',
  exposure_opportunity_up: '曝光机会分升高',
  exposure_opportunity_down: '曝光机会分降低',
  base_score_up: '综合基础分升高',
  base_score_down: '综合基础分降低',
  repeat_penalty_stronger: '重复曝光惩罚更强',
  repeat_penalty_weaker: '重复曝光惩罚更弱',
  exploration_slot_gained: '获得探索位',
  exploration_slot_lost: '失去探索位',
  constraint_relaxed: '候选不足，多样性约束降级',
  legacy_baseline: 'legacy 原始顺序',
  freshness_created_at_invalid: '创建时间无效，新鲜度按中性处理',
  freshness_clock_anomaly: '时间异常，新鲜度按中性处理',
  hard_filter_contract_broken: '硬过滤契约异常',
  match_components_all_missing: '匹配分项全部缺失',
}

export function reasonCodeLabel(code) {
  return REASON_CODE_LABEL[code] || code
}

export function reasonCodeTagType(code) {
  if (!code) return 'info'
  if (code.endsWith('_up') || code === 'entered_top_n' || code === 'exploration_slot_gained') {
    return 'success'
  }
  if (code.endsWith('_down') || code === 'left_top_n' || code === 'exploration_slot_lost') {
    return 'danger'
  }
  if (code === 'constraint_relaxed' || code.startsWith('freshness_') || code.startsWith('hard_filter')) {
    return 'warning'
  }
  return 'info'
}

export function templateByKey(key) {
  return STRATEGY_TEMPLATES.find((item) => item.key === key) || null
}

export function templateLabel(key) {
  return templateByKey(key)?.label || key || '—'
}

/** 返回模板默认参数的副本，避免调用方改到常量本体。 */
export function templateParameters(key) {
  const template = templateByKey(key) || STRATEGY_TEMPLATES[0]
  return { ...template.parameters }
}

export function normalizeParameters(raw) {
  const base = templateParameters('balanced')
  if (!raw || typeof raw !== 'object') return base
  const next = { ...base }
  for (const meta of STRATEGY_PARAM_META) {
    const value = raw[meta.key]
    if (value === null || value === undefined || value === '') continue
    next[meta.key] = meta.type === 'int' ? Number(value) : String(value)
  }
  return next
}

export function sameParameters(a, b) {
  if (!a || !b) return false
  return STRATEGY_PARAM_META.every((meta) => {
    const left = a[meta.key]
    const right = b[meta.key]
    if (meta.type === 'int') return Number(left) === Number(right)
    return String(left) === String(right)
  })
}

export function weightSum(parameters) {
  if (!parameters) return 0
  return WEIGHT_KEYS.reduce((sum, key) => sum + (Number(parameters[key]) || 0), 0)
}
