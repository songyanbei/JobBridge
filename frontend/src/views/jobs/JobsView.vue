<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">岗位管理</div>
    </div>

    <PageTable
      :rows="state.rows"
      :loading="state.loading"
      :total="state.total"
      :page="state.page"
      :size="state.size"
      :exporting="downloading"
      @update:page="setPage"
      @update:size="setSize"
      @sort-change="setSort"
      @export="onExport"
      @refresh="load()"
    >
      <template #filter>
        <el-input v-model="filters.city" placeholder="城市" clearable style="width: 120px" />
        <el-input v-model="filters.district" placeholder="区县" clearable style="width: 120px" />
        <el-input v-model="filters.job_category" placeholder="工种" clearable style="width: 130px" />
        <el-select v-model="filters.pay_type" placeholder="支付方式" clearable style="width: 120px">
          <el-option v-for="o in PAY_TYPE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-select
          v-model="filters.audit_status"
          placeholder="审核状态"
          clearable
          style="width: 120px"
        >
          <el-option label="待审" value="pending" />
          <el-option label="已通过" value="passed" />
          <el-option label="已驳回" value="rejected" />
        </el-select>
        <el-select
          v-model="filters.delist_reason"
          placeholder="下架原因"
          clearable
          style="width: 130px"
        >
          <el-option v-for="o in DELIST_REASON_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-input v-model="filters.owner_userid" placeholder="发布人 userid" clearable style="width: 160px" />
        <el-input-number v-model="filters.salary_min" placeholder="薪资下限" :min="0" style="width: 130px" />
        <el-input-number v-model="filters.salary_max" placeholder="薪资上限" :min="0" style="width: 130px" />
        <el-date-picker
          v-model="createdRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="创建起"
          end-placeholder="创建止"
          style="width: 240px"
        />
        <el-button type="primary" @click="onApply">查询</el-button>
        <el-button @click="onReset">重置</el-button>
      </template>

      <el-table-column prop="id" label="ID" width="80" sortable="custom" />
      <el-table-column prop="title" label="标题" show-overflow-tooltip />
      <el-table-column prop="city" label="城市" width="90" />
      <el-table-column prop="job_category" label="工种" width="120" />
      <el-table-column prop="pay_type" label="支付" width="80">
        <template #default="{ row }">
          {{ row.pay_type === 'daily' ? '日结' : row.pay_type === 'monthly' ? '月结' : '--' }}
        </template>
      </el-table-column>
      <el-table-column label="薪资" width="140">
        <template #default="{ row }">
          {{ row.salary_floor_monthly || '-' }} ~ {{ row.salary_ceiling_monthly || '-' }}
        </template>
      </el-table-column>
      <el-table-column prop="audit_status" label="审核" width="90">
        <template #default="{ row }">
          <el-tag
            :type="{ passed: 'success', rejected: 'danger', pending: 'warning' }[row.audit_status] || 'info'"
            size="small"
          >
            {{ { passed: '已通过', rejected: '已驳回', pending: '待审' }[row.audit_status] || '--' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="到期" width="130">
        <template #default="{ row }">
          <span :class="`jb-${ttlLevel(row.expires_at)}-text`">
            {{ formatDate(row.expires_at) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column
        prop="created_at"
        label="创建时间"
        width="160"
        sortable="custom"
      >
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </PageTable>

    <JobDetailDrawer
      v-model="detailVisible"
      :job-id="currentId"
      @updated="() => load()"
    />
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import PageTable from '@/components/PageTable.vue'
import JobDetailDrawer from './components/JobDetailDrawer.vue'
import { fetchJobs, exportJobs } from '@/api/jobs'
import { usePageTable } from '@/composables/usePageTable'
import { useDownload } from '@/composables/useDownload'
import {
  PAY_TYPE_OPTIONS,
  DELIST_REASON_OPTIONS,
} from '@/utils/constants'
import { formatDate, formatDateTime, ttlLevel } from '@/utils/format'

const { state, filters, load, setPage, setSize, setSort, applyFilters, resetFilters } =
  usePageTable({
    fetcher: fetchJobs,
    initialFilters: {
      city: '',
      district: '',
      job_category: '',
      pay_type: '',
      audit_status: '',
      delist_reason: '',
      owner_userid: '',
      salary_min: null,
      salary_max: null,
      created_from: '',
      created_to: '',
    },
  })

const createdRange = ref(null)
watch(createdRange, (v) => {
  if (v && v.length === 2) {
    filters.created_from = v[0]
    filters.created_to = v[1]
  } else {
    filters.created_from = ''
    filters.created_to = ''
  }
})

function onApply() {
  applyFilters()
}
function onReset() {
  createdRange.value = null
  resetFilters()
}

const { downloading, run } = useDownload()

function onExport() {
  run(exportJobs, [
    Object.fromEntries(
      Object.entries(filters).filter(([_, v]) => v !== '' && v !== null && v !== undefined),
    ),
  ])
}

const detailVisible = ref(false)
const currentId = ref(null)
function openDetail(row) {
  currentId.value = row.id
  detailVisible.value = true
}

load()
</script>
