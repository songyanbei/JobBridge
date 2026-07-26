<template>
  <div class="recommendation-page">
    <div class="page-header">
      <div><h2>推荐策略</h2><p>岗位 / 工人排序、模拟与曝光指标</p></div>
      <el-button type="primary" @click="load">刷新</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon />
    <el-tabs v-model="direction" @tab-change="load">
      <el-tab-pane label="岗位策略" name="search_job" />
      <el-tab-pane label="工人策略" name="search_worker" />
    </el-tabs>
    <el-card v-loading="loading">
      <template #header><span>当前版本</span></template>
      <el-descriptions v-if="strategy" :column="2" border>
        <el-descriptions-item label="运行模式">{{ strategy.release?.execution_mode || 'off' }}</el-descriptions-item>
        <el-descriptions-item label="revision">{{ strategy.release?.revision || 0 }}</el-descriptions-item>
        <el-descriptions-item label="候选版本">{{ strategy.release?.candidate_version_id || '—' }}</el-descriptions-item>
        <el-descriptions-item label="模板">{{ activeVersion?.template_key || 'balanced' }}</el-descriptions-item>
      </el-descriptions>
      <el-empty v-else description="暂无策略版本" />
    </el-card>
    <el-card class="metric-card" v-loading="metricsLoading">
      <template #header><span>近期开口指标</span></template>
      <el-statistic title="请求数" :value="metrics?.requests || 0" />
      <el-statistic title="曝光数" :value="metrics?.impressions || 0" />
      <el-statistic title="点击数" :value="metrics?.clicks || 0" />
      <el-statistic title="CTR" :value="metrics?.ctr || 0" suffix="%" />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getRecommendationMetrics, getRecommendationStrategy } from '@/api/recommendationStrategies'

const direction = ref('search_job')
const strategy = ref(null)
const metrics = ref(null)
const loading = ref(false)
const metricsLoading = ref(false)
const error = ref('')
const activeVersion = computed(() => strategy.value?.versions?.find(
  item => item.id === strategy.value?.release?.stable_version_id
))

async function load () {
  error.value = ''
  loading.value = true
  try {
    strategy.value = await getRecommendationStrategy(direction.value)
  } catch (e) {
    error.value = e?.response?.data?.detail || '策略加载失败'
  } finally {
    loading.value = false
  }
  metricsLoading.value = true
  try { metrics.value = await getRecommendationMetrics({ direction: direction.value }) } catch (_e) { /* optional */ } finally { metricsLoading.value = false }
}
onMounted(load)
</script>

<style scoped>
.recommendation-page { padding: 24px; display: grid; gap: 16px }
.page-header { display: flex; justify-content: space-between; align-items: center }
.page-header h2 { margin: 0 0 6px }
.page-header p { margin: 0; color: var(--el-text-color-secondary) }
.metric-card { display: flex; gap: 48px }
</style>
