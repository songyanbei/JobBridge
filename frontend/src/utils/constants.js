export const ERROR_CODES = {
  AUTH_INVALID: 40001,
  TOKEN_EXPIRED: 40002,
  TOKEN_INVALID: 40003,
  PARAM_ERROR: 40101,
  FORBIDDEN: 40301,
  NOT_FOUND: 40401,
  CONFLICT: 40900,
  LOCKED: 40901,
  VERSION_CONFLICT: 40902,
  UNDO_EXPIRED: 40903,
  BIZ_CONFLICT: 40904,
  INTERNAL: 50001,
  LLM_ERROR: 50101,
}

export const AUDIT_STATUS = {
  PENDING: 'pending',
  PASSED: 'passed',
  REJECTED: 'rejected',
}

export const AUDIT_STATUS_OPTIONS = [
  { label: '待审', value: 'pending' },
  { label: '已通过', value: 'passed' },
  { label: '已驳回', value: 'rejected' },
]

export const TARGET_TYPE_OPTIONS = [
  { label: '岗位', value: 'job' },
  { label: '简历', value: 'resume' },
]

export const RISK_LEVELS = {
  LOW: 'low',
  MID: 'mid',
  HIGH: 'high',
}

export const RISK_LABEL = {
  low: '低风险',
  mid: '中风险',
  high: '高风险',
}

export const PAY_TYPE_OPTIONS = [
  { label: '日结', value: 'daily' },
  { label: '月结', value: 'monthly' },
]

export const DELIST_REASON_OPTIONS = [
  { label: '人工下架', value: 'manual_delist' },
  { label: '招满', value: 'filled' },
  { label: '到期自动下架', value: 'expired' },
]

export const PREDEFINED_REJECT_REASONS = [
  '信息不完整',
  '联系方式异常',
  '薪资描述不清',
  '疑似虚假信息',
  '含敏感词',
  '重复发布',
  '岗位已失效',
  '其他',
]

export const DANGEROUS_CONFIG_KEYS = [
  'filter.enable_gender',
  'filter.enable_age',
  'filter.enable_ethnicity',
  'llm.provider',
]

export const BATCH_AUDIT_LIMIT = 20

export const LOCK_RENEW_INTERVAL_MS = 240_000 // 4 minutes
export const UNDO_WINDOW_MS = 30_000
export const DASHBOARD_REFRESH_MS = 60_000
export const BREAK_PROMPT_EVERY = 50

export const MAX_CSV_EXPORT_ROWS = 10000
export const MAX_CUSTOM_RANGE_DAYS = 90
export const MAX_LOG_RANGE_DAYS = 30

export const GENDER_OPTIONS = [
  { label: '男', value: 'male' },
  { label: '女', value: 'female' },
]

export const INTENT_OPTIONS = [
  { label: '搜索岗位', value: 'search_job' },
  { label: '搜索工人', value: 'search_worker' },
  { label: '发布岗位', value: 'publish_job' },
  { label: '发布简历', value: 'publish_resume' },
  { label: '其他', value: 'other' },
]

export const DIRECTION_OPTIONS = [
  { label: '用户发送', value: 'in' },
  { label: '系统回复', value: 'out' },
]
