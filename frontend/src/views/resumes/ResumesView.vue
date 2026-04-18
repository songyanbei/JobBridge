<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">简历管理</div>
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
        <el-select v-model="filters.gender" placeholder="性别" clearable style="width: 100px">
          <el-option v-for="o in GENDER_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-input-number v-model="filters.age_min" :min="16" :max="80" placeholder="年龄下限" style="width: 130px" />
        <el-input-number v-model="filters.age_max" :min="16" :max="80" placeholder="年龄上限" style="width: 130px" />
        <el-input v-model="filters.expected_cities" placeholder="期望城市" clearable style="width: 150px" />
        <el-input
          v-model="filters.expected_job_categories"
          placeholder="期望工种"
          clearable
          style="width: 150px"
        />
        <el-select v-model="filters.audit_status" placeholder="审核状态" clearable style="width: 120px">
          <el-option label="待审" value="pending" />
          <el-option label="已通过" value="passed" />
          <el-option label="已驳回" value="rejected" />
        </el-select>
        <el-input v-model="filters.owner_userid" placeholder="发布人 userid" clearable style="width: 160px" />
        <el-date-picker
          v-model="createdRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="创建起"
          end-placeholder="创建止"
          style="width: 240px"
        />
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="onReset">重置</el-button>
      </template>

      <el-table-column prop="id" label="ID" width="80" sortable="custom" />
      <el-table-column prop="display_name" label="姓名" width="100" />
      <el-table-column prop="gender" label="性别" width="60">
        <template #default="{ row }">
          {{ row.gender === 'male' ? '男' : row.gender === 'female' ? '女' : '--' }}
        </template>
      </el-table-column>
      <el-table-column prop="age" label="年龄" width="60" />
      <el-table-column prop="expected_job_categories" label="期望工种" show-overflow-tooltip />
      <el-table-column prop="expected_cities" label="期望城市" show-overflow-tooltip />
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
      <el-table-column prop="created_at" label="创建时间" width="160" sortable="custom">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </PageTable>

    <ResumeDetailDrawer
      v-model="detailVisible"
      :resume-id="currentId"
      @updated="() => load()"
    />
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import PageTable from '@/components/PageTable.vue'
import ResumeDetailDrawer from './components/ResumeDetailDrawer.vue'
import { fetchResumes, exportResumes } from '@/api/resumes'
import { usePageTable } from '@/composables/usePageTable'
import { useDownload } from '@/composables/useDownload'
import { GENDER_OPTIONS } from '@/utils/constants'
import { formatDate, formatDateTime, ttlLevel } from '@/utils/format'

const { state, filters, load, setPage, setSize, setSort, applyFilters, resetFilters } =
  usePageTable({
    fetcher: fetchResumes,
    initialFilters: {
      gender: '',
      age_min: null,
      age_max: null,
      expected_cities: '',
      expected_job_categories: '',
      audit_status: '',
      owner_userid: '',
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

function onReset() {
  createdRange.value = null
  resetFilters()
}

const { downloading, run } = useDownload()
function onExport() {
  run(exportResumes, [
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
