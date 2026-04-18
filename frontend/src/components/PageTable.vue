<template>
  <div class="page-table">
    <div v-if="$slots.filter" class="jb-filter-bar">
      <slot name="filter" />
    </div>

    <div class="table-toolbar">
      <div class="toolbar-left">
        <slot name="toolbar-left" />
      </div>
      <div class="toolbar-right">
        <slot name="toolbar-right" />
        <el-button v-if="exportable" :loading="exporting" @click="$emit('export')">
          导出 CSV
        </el-button>
        <el-button :icon="Refresh" circle @click="$emit('refresh')" />
      </div>
    </div>

    <el-table
      v-loading="loading"
      :data="rows"
      :row-key="rowKey"
      :empty-text="emptyText"
      stripe
      border
      style="width: 100%"
      @selection-change="(v) => $emit('selection-change', v)"
      @sort-change="onSortChange"
    >
      <el-table-column v-if="selectable" type="selection" width="48" />
      <slot />
    </el-table>

    <div class="jb-pagination-wrap">
      <el-pagination
        background
        :current-page="page"
        :page-size="size"
        :page-sizes="pageSizes"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        @current-change="(v) => $emit('update:page', v)"
        @size-change="(v) => $emit('update:size', v)"
      />
    </div>
  </div>
</template>

<script setup>
import { Refresh } from '@element-plus/icons-vue'

defineProps({
  rows: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  size: { type: Number, default: 20 },
  rowKey: { type: String, default: 'id' },
  selectable: { type: Boolean, default: false },
  exportable: { type: Boolean, default: true },
  exporting: { type: Boolean, default: false },
  emptyText: { type: String, default: '暂无数据' },
  pageSizes: { type: Array, default: () => [10, 20, 50, 100] },
})

const emit = defineEmits([
  'update:page',
  'update:size',
  'sort-change',
  'selection-change',
  'export',
  'refresh',
])

function onSortChange({ prop, order }) {
  if (!prop || !order) {
    emit('sort-change', '')
    return
  }
  const dir = order === 'ascending' ? 'asc' : 'desc'
  emit('sort-change', `${prop}:${dir}`)
}
</script>

<style scoped>
.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.toolbar-left,
.toolbar-right {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
