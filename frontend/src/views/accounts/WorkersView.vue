<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">工人列表（只读）</div>
    </div>

    <PageTable
      :rows="state.rows"
      :loading="state.loading"
      :total="state.total"
      :page="state.page"
      :size="state.size"
      :exportable="false"
      @update:page="setPage"
      @update:size="setSize"
      @refresh="load()"
    >
      <template #filter>
        <el-input
          v-model="filters.q"
          placeholder="搜索 userid / 姓名 / 手机号"
          clearable
          style="width: 260px"
          @keyup.enter="applyFilters"
        />
        <el-select v-model="filters.gender" placeholder="性别" clearable style="width: 100px">
          <el-option v-for="o in GENDER_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-input v-model="filters.city" placeholder="期望城市" clearable style="width: 140px" />
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>

      <el-table-column prop="userid" label="UserID" width="160" />
      <el-table-column prop="display_name" label="姓名" />
      <el-table-column prop="gender" label="性别" width="80">
        <template #default="{ row }">
          {{ row.gender === 'male' ? '男' : row.gender === 'female' ? '女' : '--' }}
        </template>
      </el-table-column>
      <el-table-column prop="age" label="年龄" width="80" />
      <el-table-column prop="expected_cities" label="期望城市" show-overflow-tooltip />
      <el-table-column prop="expected_job_categories" label="期望工种" show-overflow-tooltip />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'blocked' ? 'danger' : 'success'" size="small">
            {{ row.status === 'blocked' ? '已禁用' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="注册时间" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </PageTable>
  </div>
</template>

<script setup>
import PageTable from '@/components/PageTable.vue'
import { fetchWorkers } from '@/api/accounts'
import { usePageTable } from '@/composables/usePageTable'
import { GENDER_OPTIONS } from '@/utils/constants'
import { formatDateTime } from '@/utils/format'

const { state, filters, load, setPage, setSize, applyFilters, resetFilters } = usePageTable({
  fetcher: fetchWorkers,
  initialFilters: { q: '', gender: '', city: '' },
})

load()
</script>
