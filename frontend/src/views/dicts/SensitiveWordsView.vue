<template>
  <div class="jb-page">
    <div class="jb-page-header">
      <div class="jb-page-title">敏感词字典</div>
      <div>
        <el-button type="primary" @click="openCreate">新增</el-button>
        <el-button @click="openBatch">批量导入</el-button>
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
          placeholder="关键词"
          clearable
          style="width: 220px"
          @keyup.enter="applyFilters"
        />
        <el-select v-model="filters.level" placeholder="等级" clearable style="width: 120px">
          <el-option label="高" value="high" />
          <el-option label="中" value="mid" />
          <el-option label="低" value="low" />
        </el-select>
        <el-input v-model="filters.category" placeholder="分类" clearable style="width: 140px" />
        <el-button type="primary" @click="applyFilters">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>

      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="word" label="敏感词" />
      <el-table-column prop="level" label="等级" width="80">
        <template #default="{ row }">
          <el-tag
            :type="{ high: 'danger', mid: 'warning', low: 'info' }[row.level] || 'info'"
            size="small"
          >
            {{ { high: '高', mid: '中', low: '低' }[row.level] || '--' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="category" label="分类" width="140" />
      <el-table-column prop="created_at" label="创建时间" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </PageTable>

    <el-dialog v-model="formVisible" title="新增敏感词" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="敏感词" prop="word">
          <el-input v-model="form.word" />
        </el-form-item>
        <el-form-item label="等级" prop="level">
          <el-select v-model="form.level" style="width: 100%">
            <el-option label="高" value="high" />
            <el-option label="中" value="mid" />
            <el-option label="低" value="low" />
          </el-select>
        </el-form-item>
        <el-form-item label="分类">
          <el-input v-model="form.category" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onSubmit">提交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="batchVisible" title="批量导入（一行一个词）" width="520px">
      <el-form :model="batch" label-position="top">
        <el-form-item label="敏感词（必填）">
          <el-input v-model="batch.words" type="textarea" :rows="8" placeholder="每行一个词" />
        </el-form-item>
        <el-form-item label="默认等级">
          <el-select v-model="batch.level" style="width: 160px">
            <el-option label="高" value="high" />
            <el-option label="中" value="mid" />
            <el-option label="低" value="low" />
          </el-select>
        </el-form-item>
        <el-form-item label="默认分类">
          <el-input v-model="batch.category" style="width: 240px" />
        </el-form-item>
      </el-form>
      <div v-if="batchResult" class="jb-muted" style="margin-top: 6px">
        新增 {{ batchResult.added ?? 0 }} 条，重复忽略 {{ batchResult.duplicated ?? 0 }} 条
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">关闭</el-button>
        <el-button type="primary" :loading="batchSubmitting" @click="onBatchSubmit">
          执行导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageTable from '@/components/PageTable.vue'
import {
  fetchSensitiveWords,
  createSensitiveWord,
  deleteSensitiveWord,
  batchCreateSensitiveWords,
} from '@/api/dicts'
import { usePageTable } from '@/composables/usePageTable'
import { formatDateTime } from '@/utils/format'

const { state, filters, load, setPage, setSize, applyFilters, resetFilters } = usePageTable({
  fetcher: fetchSensitiveWords,
  initialFilters: { q: '', level: '', category: '' },
})

const formVisible = ref(false)
const formRef = ref(null)
const form = reactive({ word: '', level: 'mid', category: '' })
const rules = {
  word: [{ required: true, message: '请输入敏感词', trigger: 'blur' }],
  level: [{ required: true, message: '请选择等级', trigger: 'change' }],
}
const submitting = ref(false)

const batchVisible = ref(false)
const batchSubmitting = ref(false)
const batch = reactive({ words: '', level: 'mid', category: '' })
const batchResult = ref(null)

load()

function openCreate() {
  form.word = ''
  form.level = 'mid'
  form.category = ''
  formVisible.value = true
}

function openBatch() {
  batch.words = ''
  batch.level = 'mid'
  batch.category = ''
  batchResult.value = null
  batchVisible.value = true
}

async function onSubmit() {
  try {
    await formRef.value.validate()
  } catch (_e) {
    return
  }
  submitting.value = true
  try {
    await createSensitiveWord({ ...form })
    ElMessage.success('已新增')
    formVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除敏感词「${row.word}」？`, '删除确认', {
      type: 'warning',
    })
  } catch (_e) {
    return
  }
  await deleteSensitiveWord(row.id)
  ElMessage.success('已删除')
  load()
}

async function onBatchSubmit() {
  const items = batch.words
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  if (items.length === 0) {
    ElMessage.warning('请输入至少 1 条')
    return
  }
  batchSubmitting.value = true
  try {
    const payload = items.map((w) => ({
      word: w,
      level: batch.level,
      category: batch.category,
    }))
    const res = await batchCreateSensitiveWords({ items: payload })
    batchResult.value = res
    ElMessage.success(`完成：新增 ${res.added ?? 0}，重复 ${res.duplicated ?? 0}`)
    load()
  } finally {
    batchSubmitting.value = false
  }
}
</script>
