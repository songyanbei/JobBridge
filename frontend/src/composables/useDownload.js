import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ERROR_CODES } from '@/utils/constants'

export function useDownload() {
  const downloading = ref(false)

  async function run(exporter, args = []) {
    if (downloading.value) return
    downloading.value = true
    try {
      const filename = await exporter(...args)
      ElMessage.success(`已导出${filename ? `：${filename}` : ''}`)
    } catch (err) {
      if (err && err.code === ERROR_CODES.PARAM_ERROR) {
        ElMessage.error(err.message || '导出数据量过大，请缩小筛选范围')
      } else if (err && err.message) {
        ElMessage.error(err.message)
      } else {
        ElMessage.error('导出失败')
      }
    } finally {
      downloading.value = false
    }
  }

  return { downloading, run }
}
