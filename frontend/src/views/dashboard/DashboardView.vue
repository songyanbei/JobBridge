<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">Dashboard</div>
      <div>
        <el-radio-group v-model="range" size="small" @change="onRangeChange">
          <el-radio-button label="7d">近 7 天</el-radio-button>
          <el-radio-button label="30d">近 30 天</el-radio-button>
          <el-radio-button label="custom">自定义</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <el-row v-loading="loading" :gutter="16" class="metric-row">
      <el-col
        v-for="m in metrics"
        :key="m.key"
        :xs="12"
        :sm="8"
        :md="4"
      >
        <div
          class="metric-card"
          :class="{ 'metric-clickable': !!m.to }"
          @click="onMetricClick(m)"
        >
          <div class="metric-label">{{ m.label }}</div>
          <div class="metric-value">{{ m.value }}</div>
          <div class="metric-delta jb-muted">
            昨日 {{ m.yesterday }}
            <span v-if="m.delta !== null" :class="deltaClass(m.delta)">
              {{ m.delta > 0 ? '+' : '' }}{{ m.delta }}%
            </span>
          </div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="lower-row">
      <el-col :xs="24" :md="16">
        <ChartCard
          :title="`趋势（${range === '7d' ? '7 天' : '30 天'}）`"
          :option="trendOption"
          :loading="trendLoading"
          :empty="trendEmpty"
          height="340px"
        />
      </el-col>
      <el-col :xs="24" :md="8">
        <div class="todo-card jb-card">
          <div class="todo-head">
            <span class="todo-title">待办入口</span>
          </div>
          <el-space direction="vertical" fill style="width: 100%">
            <el-button type="primary" @click="$router.push('/admin/audit')">
              审核工作台（待审 {{ pendingCount.total }}）
            </el-button>
            <el-button @click="$router.push('/admin/jobs')">岗位管理</el-button>
            <el-button @click="$router.push('/admin/resumes')">简历管理</el-button>
            <el-button @click="$router.push('/admin/reports')">打开数据看板</el-button>
          </el-space>
        </div>
      </el-col>
    </el-row>
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

let refreshTimer = null

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
.metric-row {
  margin-bottom: 16px;
}
.metric-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  padding: 12px 14px;
  border-radius: 6px;
  height: 100%;
}
.metric-clickable {
  cursor: pointer;
  transition: box-shadow 0.15s ease;
}
.metric-clickable:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
}
.metric-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.metric-value {
  font-size: 22px;
  font-weight: 600;
  margin: 4px 0;
}
.metric-delta {
  font-size: 12px;
}
.lower-row {
  margin-top: 4px;
}
.todo-card {
  height: 100%;
}
.todo-head {
  margin-bottom: 8px;
  font-weight: 600;
}
</style>
