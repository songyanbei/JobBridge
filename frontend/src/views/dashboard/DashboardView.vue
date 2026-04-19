<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div>
        <div class="jb-page-title">Dashboard</div>
        <div class="page-subtitle jb-muted">实时运营概览 · 更新于 {{ updatedAt }}</div>
      </div>
      <div>
        <el-radio-group v-model="range" size="small" @change="onRangeChange">
          <el-radio-button label="7d">近 7 天</el-radio-button>
          <el-radio-button label="30d">近 30 天</el-radio-button>
          <el-radio-button label="custom">自定义</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <div v-loading="loading" class="metric-grid">
      <div
        v-for="m in metrics"
        :key="m.key"
        class="metric"
        :class="{ 'metric-clickable': !!m.to }"
        @click="onMetricClick(m)"
      >
        <div class="metric-label mono">{{ m.label }}</div>
        <div class="metric-value">{{ m.value }}</div>
        <div class="metric-delta">
          <span class="jb-muted">昨日 {{ m.yesterday }}</span>
          <span v-if="m.delta !== null" :class="deltaClass(m.delta)" class="mono">
            {{ m.delta > 0 ? '▲' : m.delta < 0 ? '▼' : '·' }} {{ Math.abs(m.delta) }}%
          </span>
        </div>
      </div>
    </div>

    <div class="lower-grid">
      <div class="trend-card">
        <ChartCard
          :title="`趋势（${range === '7d' ? '7 天' : '30 天'}）`"
          :option="trendOption"
          :loading="trendLoading"
          :empty="trendEmpty"
          height="340px"
        />
      </div>
      <div class="todo-card">
        <div class="todo-head">
          <span class="todo-title">待办入口</span>
          <span class="mono todo-hint">QUICK&nbsp;NAV</span>
        </div>
        <div class="todo-list">
          <el-button type="primary" @click="$router.push('/admin/audit')">
            审核工作台（待审 {{ pendingCount.total }}）
          </el-button>
          <el-button @click="$router.push('/admin/jobs')">岗位管理</el-button>
          <el-button @click="$router.push('/admin/resumes')">简历管理</el-button>
          <el-button @click="$router.push('/admin/reports')">打开数据看板</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import ChartCard from '@/components/ChartCard.vue'
import { fetchDashboard, fetchTrends } from '@/api/reports'
import { fetchPendingCount } from '@/api/audit'
import { formatNumber, formatPercent } from '@/utils/format'
import { DASHBOARD_REFRESH_MS } from '@/utils/constants'

const router = useRouter()
const loading = ref(false)
const trendLoading = ref(false)
const range = ref('7d')

const today = ref({})
const yesterday = ref({})
const trend7d = ref([])
const trend30d = ref([])
const pendingCount = ref({ job: 0, resume: 0, total: 0 })
const updatedAt = ref('--')

let refreshTimer = null

function formatClock(d = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function deltaPercent(curr, prev) {
  const c = Number(curr)
  const p = Number(prev)
  if (!Number.isFinite(c) || !Number.isFinite(p) || p === 0) return null
  return Number((((c - p) / p) * 100).toFixed(1))
}

const metrics = computed(() => [
  {
    key: 'dau',
    label: 'DAU',
    value: formatNumber(today.value.dau),
    yesterday: formatNumber(yesterday.value.dau),
    delta: deltaPercent(today.value.dau, yesterday.value.dau),
    to: '/admin/reports',
  },
  {
    key: 'upload',
    label: '岗位+简历上传数',
    value: formatNumber(
      (today.value.job_uploads || 0) + (today.value.resume_uploads || 0),
    ),
    yesterday: formatNumber(
      (yesterday.value.job_uploads || 0) + (yesterday.value.resume_uploads || 0),
    ),
    delta: null,
    to: '/admin/jobs',
  },
  {
    key: 'search',
    label: '检索次数',
    value: formatNumber(today.value.search_count),
    yesterday: formatNumber(yesterday.value.search_count),
    delta: deltaPercent(today.value.search_count, yesterday.value.search_count),
    to: '/admin/reports',
  },
  {
    key: 'hit',
    label: '命中率',
    value: formatPercent(today.value.hit_rate),
    yesterday: formatPercent(yesterday.value.hit_rate),
    delta: null,
    to: '/admin/reports',
  },
  {
    key: 'empty',
    label: '空召回率',
    value: formatPercent(today.value.empty_rate),
    yesterday: formatPercent(yesterday.value.empty_rate),
    delta: null,
    to: '/admin/reports',
  },
  {
    key: 'pending',
    label: '待审积压',
    value: formatNumber(today.value.audit_pending ?? pendingCount.value.total),
    yesterday: '--',
    delta: null,
    to: '/admin/audit',
  },
])

const trendEmpty = computed(() => {
  const arr = range.value === '7d' ? trend7d.value : trend30d.value
  return !arr || arr.length === 0
})

const trendOption = computed(() => {
  const source = range.value === '7d' ? trend7d.value : trend30d.value
  const dates = source.map((d) => d.date)
  const dau = source.map((d) => d.dau ?? 0)
  const upload = source.map((d) => (d.job_uploads ?? 0) + (d.resume_uploads ?? 0))
  const search = source.map((d) => d.search_count ?? 0)
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['DAU', '上传数', '检索次数'] },
    grid: { left: 40, right: 20, top: 40, bottom: 28 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value' },
    series: [
      { name: 'DAU', type: 'line', smooth: true, data: dau },
      { name: '上传数', type: 'line', smooth: true, data: upload },
      { name: '检索次数', type: 'line', smooth: true, data: search },
    ],
  }
})

function deltaClass(v) {
  if (v === null) return ''
  if (v > 0) return 'jb-success-text'
  if (v < 0) return 'jb-danger-text'
  return 'jb-muted'
}

async function loadDashboard() {
  loading.value = true
  try {
    const data = await fetchDashboard()
    today.value = data?.today || {}
    yesterday.value = data?.yesterday || {}
    trend7d.value = data?.trend_7d || []
    updatedAt.value = formatClock()
  } finally {
    loading.value = false
  }
  try {
    const p = await fetchPendingCount()
    pendingCount.value = {
      job: p.job ?? 0,
      resume: p.resume ?? 0,
      total: p.total ?? (p.job ?? 0) + (p.resume ?? 0),
    }
  } catch (_e) {}
}

async function loadTrends30d() {
  trendLoading.value = true
  try {
    const data = await fetchTrends({ range: '30d' })
    // Backend returns { range, from, to, days: [...] }. Prefer `days`; keep
    // legacy fallbacks defensively but never dump the whole object into the
    // array — trendOption iterates it with .map.
    trend30d.value = data?.days || data?.items || data?.points || []
  } finally {
    trendLoading.value = false
  }
}

function onRangeChange(v) {
  if (v === 'custom') {
    ElMessage.info('自定义区间请前往“数据看板”')
    router.push('/admin/reports')
    range.value = '7d'
    return
  }
  if (v === '30d' && trend30d.value.length === 0) {
    loadTrends30d()
  }
}

function onMetricClick(m) {
  if (m.to) router.push(m.to)
}

onMounted(() => {
  loadDashboard()
  refreshTimer = window.setInterval(() => {
    loadDashboard()
  }, DASHBOARD_REFRESH_MS)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.page-subtitle {
  font-size: var(--text-base);
  color: var(--ink-muted);
  margin-top: 6px;
  font-weight: 400;
  line-height: 1.5;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: var(--gap);
  margin-bottom: 18px;
}
@media (max-width: 1280px) {
  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.metric {
  position: relative;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 16px 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color 0.12s ease, transform 0.12s ease, box-shadow 0.12s ease;
  box-shadow: var(--elev-1);
}
.metric-clickable {
  cursor: pointer;
}
.metric-clickable:hover {
  border-color: var(--line-strong);
  transform: translateY(-1px);
  box-shadow: var(--elev-2);
}
.metric-label {
  font-size: 11.5px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--ink-muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.metric-value {
  font-size: 30px;
  font-weight: 600;
  letter-spacing: -0.025em;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
  color: var(--ink);
}
.metric-delta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--ink-muted);
}
.metric-delta :deep(.jb-success-text) {
  color: var(--success);
  font-weight: 500;
}
.metric-delta :deep(.jb-danger-text) {
  color: var(--danger);
  font-weight: 500;
}

.lower-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: var(--gap);
}
@media (max-width: 960px) {
  .lower-grid {
    grid-template-columns: 1fr;
  }
}

.trend-card :deep(.chart-card) {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--panel);
  box-shadow: var(--elev-1);
}
.trend-card :deep(.chart-head) {
  padding: 14px var(--card-pad);
  border-bottom: 1px solid var(--line);
  margin-bottom: 0;
}
.trend-card :deep(.chart-title) {
  font-weight: 600;
  font-size: var(--text-md);
  color: var(--ink);
}

.todo-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  box-shadow: var(--elev-1);
  padding: var(--card-pad);
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.todo-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}
.todo-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--ink);
}
.todo-hint {
  font-size: 10px;
  color: var(--ink-muted);
  letter-spacing: 0.18em;
}
.todo-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.todo-list :deep(.el-button) {
  width: 100%;
  justify-content: flex-start;
  height: 38px;
}
</style>
