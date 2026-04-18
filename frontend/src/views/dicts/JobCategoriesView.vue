<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">工种字典</div>
      <div>
        <el-button type="primary" @click="openCreate">新增工种</el-button>
      </div>
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
          placeholder="搜索名称"
          clearable
          style="width: 260px"
          @keyup.enter="applyFilters"
        />
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>

      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="name" label="名称" />
      <el-table-column prop="aliases" label="别名">
        <template #default="{ row }">
          <el-tag v-for="a in row.aliases || []" :key="a" size="small" style="margin-right: 4px">
            {{ a }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="sort_order" label="排序" width="100" />
      <el-table-column prop="enabled" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled === false ? 'info' : 'success'" size="small">
            {{ row.enabled === false ? '禁用' : '启用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </PageTable>

    <el-dialog v-model="formVisible" :title="editingId ? '编辑工种' : '新增工种'" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="别名">
          <el-select
            v-model="form.aliases"
            multiple
            filterable
            allow-create
            default-first-option
            style="width: 100%"
            placeholder="输入别名后回车"
          />
        </el-form-item>
        <el-form-item label="排序（数值越小越靠前）">
          <el-input-number v-model="form.sort_order" :min="0" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onSubmit">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageTable from '@/components/PageTable.vue'
import {
  fetchJobCategories,
  createJobCategory,
  updateJobCategory,
  deleteJobCategory,
} from '@/api/dicts'
import { usePageTable } from '@/composables/usePageTable'
import { ERROR_CODES } from '@/utils/constants'

const { state, filters, load, setPage, setSize, applyFilters, resetFilters } = usePageTable({
  fetcher: fetchJobCategories,
  initialFilters: { q: '' },
})

const formVisible = ref(false)
const editingId = ref(null)
const editingVersion = ref(null)
const submitting = ref(false)
const formRef = ref(null)
const form = reactive({
  name: '',
  aliases: [],
  sort_order: 100,
  enabled: true,
})

const rules = {
  name: [{ required: true, message: '请输入工种名称', trigger: 'blur' }],
}

load()

function openCreate() {
  editingId.value = null
  editingVersion.value = null
  form.name = ''
  form.aliases = []
  form.sort_order = 100
  form.enabled = true
  formVisible.value = true
}

function openEdit(row) {
  editingId.value = row.id
  editingVersion.value = row.version
  form.name = row.name
  form.aliases = [...(row.aliases || [])]
  form.sort_order = row.sort_order ?? 100
  form.enabled = row.enabled !== false
  formVisible.value = true
}

async function onSubmit() {
  try {
    await formRef.value.validate()
  } catch (_e) {
    return
  }
  submitting.value = true
  try {
    if (editingId.value) {
      await updateJobCategory(editingId.value, {
        version: editingVersion.value,
        ...form,
      })
    } else {
      await createJobCategory({ ...form })
    }
    ElMessage.success('已保存')
    formVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除工种「${row.name}」？若已被引用将无法删除。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch (_e) {
    return
  }
  try {
    await deleteJobCategory(row.id)
    ElMessage.success('已删除')
    load()
  } catch (err) {
    if (err && err.code === ERROR_CODES.BIZ_CONFLICT) {
      ElMessage.error(err.message || '该工种已被引用，无法删除')
    }
  }
}
</script>
