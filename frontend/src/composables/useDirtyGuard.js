import { ref, onBeforeUnmount } from 'vue'
import { ElMessageBox } from 'element-plus'

/**
 * Track a dirty flag and prompt on navigation-like transitions.
 * The component-level beforeRouteLeave hook still has to be wired by the caller
 * (composables cannot inject router guards into setup-script components safely).
 */
export function useDirtyGuard() {
  const dirty = ref(false)

  function markDirty() {
    dirty.value = true
  }
  function markClean() {
    dirty.value = false
  }

  const onBeforeUnload = (e) => {
    if (dirty.value) {
      e.preventDefault()
      e.returnValue = ''
    }
  }
  window.addEventListener('beforeunload', onBeforeUnload)
  onBeforeUnmount(() => {
    window.removeEventListener('beforeunload', onBeforeUnload)
  })

  async function confirmLeave(message = '有未保存的修改，确认离开？') {
    if (!dirty.value) return true
    try {
      await ElMessageBox.confirm(message, '未保存的修改', {
        confirmButtonText: '离开',
        cancelButtonText: '留下',
        type: 'warning',
      })
      return true
    } catch (_e) {
      return false
    }
  }

  return { dirty, markDirty, markClean, confirmLeave }
}
