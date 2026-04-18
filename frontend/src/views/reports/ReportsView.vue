<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">数据看板</div>
      <div class="header-right">
        <el-radio-group v-model="range" size="small" @change="onRangeChange">
          <el-radio-button label="7d">7 天</el-radio-button>
          <el-radio-button label="30d">30 天</el-radio-button>
          <el-radio-button label="custom">自定义</el-radio-button>
        </el-radio-group>
        <el-date-picker
          v-if="range === 'custom'"
          v-model="customRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始"
          end-placeholder="结束"
          style="margin-left: 8px; width: 240px"
          :disabled-date="() => false"
          @change="onCustomChange"
        />
        <el-button :loading="downloading" style="margin-left: 8px" @click="onExport">
          导出 CSV
        </el-button>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :span="24">
        <ChartCard
          :title="`趋势图（${range}）`"
          :option="trendOption"
          :loading="trendLoading"
          :empty="!trendData.length"
          :error="trendError"
          height="320px"
          @retry="loadTrends"
        />
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <ChartCard
          title="TOP 榜单"
          :loading="topLoading"
          :option="topOption"
          :empty="!topItems.length"
          :error="topError"
          height="320px"
          @retry="loadTop"
        >
          <template #header>
            <el-select v-model="topDim" size="small" style="width: 140px" @change="loadTop">
              <el-option label="按城市" value="city" />
              <el-option label="按工种" value="job_category" />
              <el-option label="按角色" value="role" />
            </el-select>
          </template>
        </ChartCard>
      </el-col>
      <el-col :span="12">
        <ChartCard
          title="转化漏斗（近 30 天）"
          :loading="funnelLoading"
          :option="funnelOption"
          :empty="!funnelStages.length"
          :error="funnelError"
          height="320px"
          @retry="loadFunnel"
        />
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ChartCard from '@/components/ChartCard.vue'
import { fetchTrends, fetchTop, fetchFunnel, exportReports } from '@/api/reports'
import { useDownload } from '@/composables/useDownload'
import { MAX_CUSTOM_RANGE_DAYS } from '@/utils/constants'
import { rangeDays } from '@/utils/validators'

const range = ref('7d')
const customRange = ref(null)

const trendLoading = ref(false)
const trendError = ref(null)
const trendData = ref([])

const topLoading = ref(false)
const topError = ref(null)
const topItems = ref([])
const topDim = ref('city')

const funnelLoading = ref(false)
const funnelError = ref(null)
const funnelStages = ref([])

const trendOption = computed(() => {
  const dates = trendData.value.map((d) => d.date)
  const dau = trendData.value.map((d) => d.dau ?? 0)
  const uploads = trendData.value.map(
    (d) => (d.job_uploads ?? 0) + (d.resume_uploads ?? 0),
  )
  const search = trendData.value.map((d) => d.search_count ?? 0)
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['DAU', '上传数', '检索次数'] },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value' },
    series: [
      { name: 'DAU', type: 'line', smooth: true, data: dau },
      { name: '上传数', type: 'line', smooth: true, data: uploads },
      { name: '检索次数', type: 'line', smooth: true, data: search },
    ],
  }
})

const topOption = computed(() => {
  return {
    tooltip: { trigger: 'item' },
    grid: { left: 80, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'value' },
    yAxis: {
      type: 'category',
      data: topItems.value.map((i) => i.key || i.name),
      inverse: true,
    },
    series: [
      {
        type: 'bar',
        data: topItems.value.map((i) => i.count ?? i.value ?? 0),
      },
    ],
  }
})

const funnelOption = computed(() => {
  return {
    tooltip: { trigger: 'item' },
    series: [
      {
        type: 'funnel',
        left: '10%',
        right: '10%',
        data: funnelStages.value.map((s) => ({
          name: s.stage || s.name,
          value: s.count ?? s.value ?? 0,
        })),
      },
    ],
  }
})

async function loadTrends() {
  trendLoading.value = true
  trendError.value = null
  try {
    const params = { range: range.value }
    if (range.value === 'custom') {
      if (!customRange.value || customRange.value.length !== 2) {
        trendLoading.value = false
        trendData.value = []
        return
      }
      const d = rangeDays(customRange.value[0], customRange.value[1])
      if (d !== null && d > MAX_CUSTOM_RANGE_DAYS) {
        ElMessage.error(`自定义区间最大 ${MAX_CUSTOM_RANGE_DAYS} 天`)
        trendLoading.value = false
        trendData.value = []
        return
      }
      params.from = customRange.value[0]
      params.to = customRange.value[1]
    }
    const data = await fetchTrends(params)
    // Backend returns { range, from, to, days: [...] }.
    trendData.value = data?.days || data?.items || data?.points || []
  } catch (err) {
    trendError.value = err?.message || '加载失败'
  } finally {
    trendLoading.value = false
  }
}

function pickArray(...candidates) {
  for (const c of candidates) {
    if (Array.isArray(c)) return c
  }
  return []
}

async function loadTop() {
  topLoading.value = true
  topError.value = null
  try {
    const data = await fetchTop({ dim: topDim.value, limit: 10 })
    // Never assign the raw response envelope — `topOption.map()` would crash.
    topItems.value = pickArray(data?.items, data)
  } catch (err) {
    topError.value = err?.message || '加载失败'
  } finally {
    topLoading.value = false
  }
}

async function loadFunnel() {
  funnelLoading.value = true
  funnelError.value = null
  try {
    const data = await fetchFunnel()
    funnelStages.value = pickArray(data?.stages, data?.items, data)
  } catch (err) {
    funnelError.value = err?.message || '加载失败'
  } finally {
    funnelLoading.value = false
  }
}

function onRangeChange() {
  if (range.value !== 'custom') {
    customRange.value = null
    loadTrends()
  }
}

function onCustomChange() {
  if (!customRange.value || customRange.value.length !== 2) return
  const d = rangeDays(customRange.value[0], customRange.value[1])
  if (d !== null && d > MAX_CUSTOM_RANGE_DAYS) {
    ElMessage.error(`自定义区间最大 ${MAX_CUSTOM_RANGE_DAYS} 天`)
    return
  }
  loadTrends()
}

const { downloading, run } = useDownload()
function onExport() {
  const params = { metric: 'daily', format: 'csv' }
  if (range.value === 'custom' && customRange.value?.length === 2) {
    params.from = customRange.value[0]
    params.to = customRange.value[1]
  } else if (range.value === '30d') {
    const to = new Date()
    const from = new Date(to.getTime() - 29 * 86400000)
    params.from = from.toISOString().slice(0, 10)
    params.to = to.toISOString().slice(0, 10)
  } else {
    const to = new Date()
    const from = new Date(to.getTime() - 6 * 86400000)
    params.from = from.toISOString().slice(0, 10)
    params.to = to.toISOString().slice(0, 10)
  }
  run(exportReports, [params])
}

loadTrends()
loadTop()
loadFunnel()
</script>

<style scoped>
.header-right {
  display: flex;
  align-items: center;
}
</style>
