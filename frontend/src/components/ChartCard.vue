<template>
  <div v-loading="loading" class="chart-card">
    <div v-if="title || $slots.header" class="chart-head">
      <div class="chart-title">{{ title }}</div>
      <div class="chart-extra"><slot name="header" /></div>
    </div>
    <div v-if="error" class="chart-err">
      <el-result icon="warning" :title="String(error)" sub-title="请重试">
        <template #extra>
          <el-button type="primary" size="small" @click="$emit('retry')">重试</el-button>
        </template>
      </el-result>
    </div>
    <div v-else-if="empty" class="chart-empty">
      <el-empty description="暂无数据" :image-size="80" />
    </div>
    <v-chart v-else :option="option" :style="{ height: height }" autoresize />
  </div>
</template>

<script setup>
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import {
  LineChart,
  BarChart,
  PieChart,
  FunnelChart,
} from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  PieChart,
  FunnelChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
])

defineProps({
  title: { type: String, default: '' },
  option: { type: Object, default: () => ({}) },
  loading: { type: Boolean, default: false },
  error: { type: [Error, String, Object], default: null },
  empty: { type: Boolean, default: false },
  height: { type: String, default: '320px' },
})

defineEmits(['retry'])
</script>

<style scoped>
.chart-card {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px 16px;
}
.chart-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.chart-title {
  font-weight: 600;
}
.chart-empty,
.chart-err {
  padding: 30px 0;
}
</style>
