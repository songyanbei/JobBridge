import { onMounted, onUnmounted } from 'vue'

/**
 * Bind keydown shortcuts scoped to the current page.
 * Handlers are skipped while an editable element (input/textarea/contenteditable)
 * is focused, to avoid interfering with form input.
 *
 * bindings: [{ key: 'p', handler: fn, shift?: bool }]
 */
export function useKeyboard(bindings) {
  function isEditableTarget(e) {
    const el = e.target
    if (!el) return false
    const tag = (el.tagName || '').toLowerCase()
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true
    if (el.isContentEditable) return true
    return false
  }

  function onKeyDown(e) {
    if (isEditableTarget(e)) return
    if (e.ctrlKey || e.metaKey || e.altKey) return
    const key = e.key
    for (const b of bindings) {
      if (b.disabled && typeof b.disabled === 'function' && b.disabled()) continue
      const matches = b.key === '?' ? key === '?' || (e.shiftKey && key === '/') : key.toLowerCase() === b.key.toLowerCase()
      if (!matches) continue
      if (b.shift && !e.shiftKey) continue
      e.preventDefault()
      b.handler(e)
      break
    }
  }

  onMounted(() => window.addEventListener('keydown', onKeyDown))
  onUnmounted(() => window.removeEventListener('keydown', onKeyDown))
}
