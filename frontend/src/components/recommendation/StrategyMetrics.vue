<template>
  <div class="strategy-metrics">
    <div class="metrics-head">
      <el-radio-group v-model="days" size="small" @change="load">
        <el-radio-button :label="7">7 天</el-radio-button>
        <el-radio-button :label="14">14 天</el-radio-button>
        <el-radio-button :label="30">30 天</el-radio-button>
      </el-radio-group>
      <span v-if="metrics?.window" class="jb-muted window-hint">
        统计窗口 {{ metrics.window.start_utc }} ~ {{ metrics.window.end_utc }}（UTC）·
        业务时区 {{ metrics.window.business_timezone }}
      </span>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      class="metrics-alert"
    />

    <div v-loading="loading" class="metrics-body">
      <div class="stat-row">
        <el-statistic title="推荐请求数" :value="metrics?.requests || 0" />
        <el-statistic title="曝光数" :value="metrics?.impressions || 0" />
        <el-statistic title="曝光用户数" :value="metrics?.exposed_users || 0" />
        <el-statistic title="曝光候选数" :value="metrics?.unique_candidates || 0" />
        <el-statistic title="归因点击数" :value="metrics?.clicks || 0" />
        <el-statistic title="CTR" :value="metrics?.ctr || 0" suffix="%" :precision="2" />
        <el-statistic
          title="零结果率"
          :value="ratioToPercent(metrics?.zero_result_rate)"
          suffix="%"
          :precision="2"
        />
      </div>

      <el-collapse v-model="activeGroups">
        <el-collapse-item name="requests" title="请求（request）">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="请求总数">{{ num(group('request_metrics').total) }}</el-descriptions-item>
            <el-descriptions-item label="零结果请求">{{ num(group('request_metrics').zero_result) }}</el-descriptions-item>
            <el-descriptions-item label="业务零结果">{{ num(group('request_metrics').business_zero_result) }}</el-descriptions-item>
            <el-descriptions-item label="Top3 单主体占满率">{{ pct(group('request_metrics').top3_single_owner_rate) }}</el-descriptions-item>
            <el-descriptions-item label="Top3 重复曝光率">{{ pct(group('request_metrics').top3_repeat_rate) }}</el-descriptions-item>
            <el-descriptions-item label="show_more 耗尽">{{ num(group('request_metrics').show_more_exhausted) }}</el-descriptions-item>
            <el-descriptions-item label="端到端 P95">{{ ms(group('request_metrics').total_latency_p95_ms) }}</el-descriptions-item>
            <el-descriptions-item label="端到端 P99">{{ ms(group('request_metrics').total_latency_p99_ms) }}</el-descriptions-item>
            <el-descriptions-item label="结果被截断">{{ bool(group('request_metrics').truncated) }}</el-descriptions-item>
            <el-descriptions-item label="按请求类型" :span="3">
              {{ dist(group('request_metrics').by_kind) }}
            </el-descriptions-item>
            <el-descriptions-item label="执行模式分布" :span="3">
              {{ dist(group('request_metrics').execution_mode_counts) }}
            </el-descriptions-item>
            <el-descriptions-item label="分流归属分布" :span="3">
              {{ dist(group('request_metrics').assignment_counts) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item name="attempts" title="排序尝试（attempt）">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="尝试总数">{{ num(group('attempts').total) }}</el-descriptions-item>
            <el-descriptions-item label="重排回退率">{{ pct(group('attempts').reranker_fallback_rate) }}</el-descriptions-item>
            <el-descriptions-item label="零候选率">{{ pct(group('attempts').zero_candidate_rate) }}</el-descriptions-item>
            <el-descriptions-item label="LLM 重试次数">{{ num(group('attempts').llm_retry_count) }}</el-descriptions-item>
            <el-descriptions-item label="排序耗时 P95">{{ ms(group('attempts').ranking_latency_p95_ms) }}</el-descriptions-item>
            <el-descriptions-item label="排序耗时 P99">{{ ms(group('attempts').ranking_latency_p99_ms) }}</el-descriptions-item>
            <el-descriptions-item label="回退原因" :span="3">
              {{ dist(group('attempts').fallback_by_reason) }}
            </el-descriptions-item>
            <el-descriptions-item label="LLM 状态分布" :span="3">
              {{ dist(group('attempts').llm_status_counts) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item name="exposure" title="曝光与召回池集中度">
          <el-alert
            type="info"
            :closable="false"
            class="inline-note"
            title="「已曝光候选集中度」的分母只含出现过曝光的候选，「召回池集中度」的分母含零曝光候选，两者口径不同，不可混用。"
          />
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="曝光数">{{ num(group('exposure').impressions) }}</el-descriptions-item>
            <el-descriptions-item label="曝光用户数">{{ num(group('exposure').exposed_users) }}</el-descriptions-item>
            <el-descriptions-item label="曝光候选数">{{ num(group('exposure').exposed_candidates) }}</el-descriptions-item>
            <el-descriptions-item label="探索位曝光数">{{ num(group('exposure').exploration_impressions) }}</el-descriptions-item>
            <el-descriptions-item label="探索位占比">{{ pct(group('exposure').exploration_share) }}</el-descriptions-item>
            <el-descriptions-item label="已曝光候选集中度 Gini">{{ ratio(group('exposure').exposed_candidate_gini) }}</el-descriptions-item>
            <el-descriptions-item label="召回池 attempt 数">{{ num(group('recall_pool').attempts) }}</el-descriptions-item>
            <el-descriptions-item label="召回池候选数">{{ num(group('recall_pool').pool_candidates) }}</el-descriptions-item>
            <el-descriptions-item label="召回池曝光覆盖率">{{ pct(group('recall_pool').coverage) }}</el-descriptions-item>
            <el-descriptions-item label="召回池集中度 Gini">{{ ratio(group('recall_pool').gini) }}</el-descriptions-item>
            <el-descriptions-item label="召回池被截断">{{ bool(group('recall_pool').truncated) }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item name="clicks" title="点击与归因">
          <el-alert
            v-if="!group('clicks').attribution_direction_scoped"
            type="warning"
            :closable="false"
            class="inline-note"
            title="legacy 点击没有 delivery_id，无法判定方向，归因比例不跟随方向筛选。"
          />
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="归因点击数">{{ num(group('clicks').attributed_impression_clicks) }}</el-descriptions-item>
            <el-descriptions-item label="CTR">{{ pct(group('clicks').ctr) }}</el-descriptions-item>
            <el-descriptions-item label="探索位 CTR">{{ pct(group('clicks').exploration_ctr) }}</el-descriptions-item>
            <el-descriptions-item label="非探索位 CTR">{{ pct(group('clicks').non_exploration_ctr) }}</el-descriptions-item>
            <el-descriptions-item label="点击可归因率">{{ pct(group('clicks').attributed_click_rate) }}</el-descriptions-item>
            <el-descriptions-item label="归因来源分布" :span="3">
              {{ dist(group('clicks').attribution_counts) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-table
            v-if="(group('clicks').by_strategy_version || []).length"
            :data="group('clicks').by_strategy_version"
            border
            size="small"
            class="sub-table"
          >
            <el-table-column label="策略版本" width="120">
              <template #default="{ row }">
                <span class="mono">{{ row.strategy_version_id ?? 'legacy' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="曝光" prop="impressions" width="110" />
            <el-table-column label="点击" prop="clicks" width="110" />
            <el-table-column label="CTR">
              <template #default="{ row }">{{ pct(row.ctr) }}</template>
            </el-table-column>
          </el-table>
        </el-collapse-item>

        <el-collapse-item name="delivery" title="投递与曝光落库">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="投递总数">{{ num(group('delivery').total) }}</el-descriptions-item>
            <el-descriptions-item label="unknown 比例">{{ pct(group('delivery').unknown_rate) }}</el-descriptions-item>
            <el-descriptions-item label="曝光积压">{{ num(group('delivery').impression_backlog) }}</el-descriptions-item>
            <el-descriptions-item label="曝光积压率">{{ pct(group('delivery').impression_backlog_rate) }}</el-descriptions-item>
            <el-descriptions-item label="发送到曝光 P95">{{ ms(group('delivery').sent_to_impression_p95_ms) }}</el-descriptions-item>
            <el-descriptions-item label="发送到曝光 P99">{{ ms(group('delivery').sent_to_impression_p99_ms) }}</el-descriptions-item>
            <el-descriptions-item label="prepared 会话冲突">{{ num(group('delivery').prepared_session_conflicts) }}</el-descriptions-item>
            <el-descriptions-item label="调度认领延迟 P95">{{ ms(group('delivery').dispatcher_claim_latency_p95_ms) }}</el-descriptions-item>
            <el-descriptions-item label="结果被截断">{{ bool(group('delivery').truncated) }}</el-descriptions-item>
            <el-descriptions-item label="状态分布" :span="3">
              {{ dist(group('delivery').status_counts) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item name="llm" title="LLM 成本与 shadow">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="legacy 输入 token">{{ num(group('llm').legacy_input_tokens) }}</el-descriptions-item>
            <el-descriptions-item label="legacy 输出 token">{{ num(group('llm').legacy_output_tokens) }}</el-descriptions-item>
            <el-descriptions-item label="shadow 输入 token">{{ num(group('llm').shadow_input_tokens) }}</el-descriptions-item>
            <el-descriptions-item label="shadow 输出 token">{{ num(group('llm').shadow_output_tokens) }}</el-descriptions-item>
            <el-descriptions-item label="provider 限流率">{{ pct(group('llm').provider_throttle_rate) }}</el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-if="!group('shadow').available"
            type="info"
            :closable="false"
            class="inline-note"
            :title="`shadow 链路数据不可用，下列数值不可解读。缺失来源：${(group('shadow').missing_sources || []).join('、') || '未说明'}`"
          />
          <el-descriptions v-else :column="3" border size="small">
            <el-descriptions-item label="shadow 请求数">{{ num(group('shadow').requests) }}</el-descriptions-item>
            <el-descriptions-item label="Top N 重合率">{{ pct(group('shadow').top_n_overlap_rate) }}</el-descriptions-item>
            <el-descriptions-item label="平均位次差">{{ ratio(group('shadow').average_position_delta) }}</el-descriptions-item>
            <el-descriptions-item label="超时次数">{{ num(group('shadow').timeout_count) }}</el-descriptions-item>
            <el-descriptions-item label="本地容量跳过">{{ num(group('shadow').local_capacity_skip_count) }}</el-descriptions-item>
            <el-descriptions-item label="全局容量跳过">{{ num(group('shadow').global_capacity_skip_count) }}</el-descriptions-item>
            <el-descriptions-item label="落库丢弃">{{ num(group('shadow').persistence_drop_count) }}</el-descriptions-item>
            <el-descriptions-item label="队列等待 P95">{{ ms(group('shadow').queue_wait_p95_ms) }}</el-descriptions-item>
            <el-descriptions-item label="执行耗时 P95">{{ ms(group('shadow').duration_p95_ms) }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>
      </el-collapse>

      <ChartCard
        title="自然日曝光聚合（Asia/Shanghai 业务日）"
        :option="exposureOption"
        :loading="dailyLoading"
        :empty="!dailyPoints.length"
        :error="dailyError"
        height="300px"
        @retry="loadDaily"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import ChartCard from '@/components/ChartCard.vue'
import {
  apiErrorMessage,
  getRecommendationExposureDaily,
  getRecommendationMetrics,
} from '@/api/recommendationStrategies'
import { formatNumber, formatPercent } from '@/utils/format'

const props = defineProps({
  direction: { type: String, required: true },
})

const days = ref(7)
const loading = ref(false)
const error = ref('')
const metrics = ref(null)
const activeGroups = ref(['requests', 'exposure'])

const dailyLoading = ref(false)
const dailyError = ref('')
const dailyPoints = ref([])

const EMPTY_GROUP = {}

function group(name) {
  return metrics.value?.[name] || EMPTY_GROUP
}

function num(value) {
  if (value === null || value === undefined) return '—'
  return formatNumber(value)
}

function pct(value) {
  if (value === null || value === undefined) return '—'
  return formatPercent(value, 2)
}

function ratio(value) {
  if (value === null || value === undefined) return '—'
  const parsed = Number(value)
  return Number.isNaN(parsed) ? '—' : parsed.toFixed(3)
}

function ms(value) {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toFixed(0)} ms`
}

function bool(value) {
  if (value === null || value === undefined) return '—'
  return value ? '是' : '否'
}

function dist(mapping) {
  const entries = Object.entries(mapping || {})
  if (!entries.length) return '—'
  return entries.map(([key, value]) => `${key}: ${formatNumber(value)}`).join('  ·  ')
}

function ratioToPercent(value) {
  if (value === null || value === undefined) return 0
  const parsed = Number(value)
  return Number.isNaN(parsed) ? 0 : parsed * 100
}

const exposureTargetType = computed(() =>
  props.direction === 'search_job' ? 'job' : 'resume',
)

const exposureOption = computed(() => {
  const dates = dailyPoints.value.map((point) => point.stat_date)
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['曝光数', '曝光候选数', '单候选最大曝光'] },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: dates },
    yAxis: { type: 'value' },
    series: [
      {
        name: '曝光数',
        type: 'line',
        smooth: true,
        data: dailyPoints.value.map((point) => point.impressions ?? 0),
      },
      {
        name: '曝光候选数',
        type: 'line',
        smooth: true,
        data: dailyPoints.value.map((point) => point.candidates ?? 0),
      },
      {
        name: '单候选最大曝光',
        type: 'line',
        smooth: true,
        data: dailyPoints.value.map((point) => point.max_candidate_impressions ?? 0),
      },
    ],
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    metrics.value = await getRecommendationMetrics({
      direction: props.direction,
      days: days.value,
    })
  } catch (err) {
    metrics.value = null
    error.value = apiErrorMessage(err, '推荐指标加载失败')
  } finally {
    loading.value = false
  }
  loadDaily()
}

function isoDate(offsetDays) {
  const date = new Date(Date.now() - offsetDays * 86400000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

async function loadDaily() {
  dailyLoading.value = true
  dailyError.value = ''
  try {
    // stat_date 是 Asia/Shanghai 自然日；这里按浏览器本地日期取窗口，边界可能差一天。
    const data = await getRecommendationExposureDaily({
      target_type: exposureTargetType.value,
      from: isoDate(days.value - 1),
      to: isoDate(0),
    })
    dailyPoints.value = data?.points || []
  } catch (err) {
    dailyPoints.value = []
    dailyError.value = apiErrorMessage(err, '曝光日聚合加载失败')
  } finally {
    dailyLoading.value = false
  }
}

watch(() => props.direction, load, { immediate: true })
</script>

<style scoped>
.strategy-metrics {
  display: grid;
  gap: 14px;
}
.metrics-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.window-hint {
  font-size: 12.5px;
}
.metrics-body {
  display: grid;
  gap: 14px;
}
.stat-row {
  display: flex;
  gap: 36px;
  flex-wrap: wrap;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.metrics-alert,
.inline-note {
  margin-bottom: 10px;
}
.sub-table {
  margin-top: 10px;
}
</style>
