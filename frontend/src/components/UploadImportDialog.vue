<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="(v) => $emit('update:modelValue', v)"
    @close="onClose"
  >
    <el-alert v-if="hint" :title="hint" type="info" :closable="false" style="margin-bottom: 12px" />

    <el-upload
      :auto-upload="false"
      :limit="1"
      :on-change="onFileChange"
      :on-remove="onFileRemove"
      :file-list="fileList"
      accept=".xlsx"
      drag
    >
      <el-icon class="el-icon--upload"><upload-filled /></el-icon>
      <div class="el-upload__text">
        拖动 <em>.xlsx</em> 文件到此，或点击选择
      </div>
    </el-upload>

    <div v-if="result && result.success_count > 0 && (!result.failed || result.failed.length === 0)" class="result-ok">
      已成功导入 {{ result.success_count }} 条
    </div>

    <div v-if="result && result.failed && result.failed.length > 0" class="result-fail">
      <el-alert
        title="本次导入未完成：Excel 中存在错误行，后端已整体回滚，未写入任何数据。请修正 Excel 后重新提交。"
        type="error"
        :closable="false"
        show-icon
      />
      <el-table :data="result.failed" style="margin-top: 10px" border size="small">
        <el-table-column prop="row" label="行号" width="80" />
        <el-table-column prop="error" label="错误原因" />
      </el-table>
    </div>

    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!file" @click="onSubmit">
        开始导入
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: 'Excel 批量导入' },
  hint: {
    type: String,
    default: '仅支持 .xlsx 格式；任一行失败将整体回滚，需修正后重新上传',
  },
  uploader: { type: Function, required: true },
})

const emit = defineEmits(['update:modelValue', 'success'])

const file = ref(null)
const fileList = ref([])
const submitting = ref(false)
const result = ref(null)

watch(
  () => props.modelValue,
  (v) => {
    if (!v) {
      file.value = null
      fileList.value = []
      result.value = null
    }
  },
)

function onFileChange(f) {
  if (!f || !f.raw) return
  if (!f.name.toLowerCase().endsWith('.xlsx')) {
    ElMessage.error('仅支持 .xlsx 文件')
    fileList.value = []
    file.value = null
    return
  }
  file.value = f.raw
  fileList.value = [f]
  result.value = null
}

function onFileRemove() {
  file.value = null
  fileList.value = []
}

async function onSubmit() {
  if (!file.value) return
  submitting.value = true
  result.value = null
  try {
    const data = await props.uploader(file.value)
    const failed = data?.failed || []
    // All-or-nothing contract: if ANY row failed, the backend rolled the whole
    // batch back. Display success_count=0 even if the server incorrectly echoes
    // a non-zero value, so operators aren't misled into thinking part landed.
    const success = failed.length > 0 ? 0 : data?.success_count ?? 0
    result.value = { success_count: success, failed }
    if (failed.length === 0 && success > 0) {
      ElMessage.success(`已导入 ${success} 条`)
      emit('success', result.value)
    } else if (failed.length === 0 && success === 0) {
      ElMessage.warning('本次未导入任何数据')
    }
  } catch (err) {
    if (err && err.data) {
      result.value = {
        success_count: 0,
        failed: err.data.failed || [],
      }
    }
  } finally {
    submitting.value = false
  }
}

function onClose() {
  // nothing extra
}
</script>

<style scoped>
.result-ok {
  margin-top: 12px;
  color: var(--el-color-success);
}
.result-fail {
  margin-top: 12px;
}
</style>
