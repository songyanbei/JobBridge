import { reactive } from 'vue'

/**
 * Generic paged list state container.
 *
 * Usage:
 *   const { state, filters, load, reset } = usePageTable({
 *     fetcher: fetchJobs,
 *     initialFilters: { city: '' },
 *   })
 */
export function usePageTable({ fetcher, initialFilters = {}, pageSize = 20 } = {}) {
  const state = reactive({
    loading: false,
    error: null,
    rows: [],
    total: 0,
    page: 1,
    size: pageSize,
    sort: '',
  })
  const filters = reactive({ ...initialFilters })

  function buildParams(extra = {}) {
    const params = { page: state.page, size: state.size }
    if (state.sort) params.sort = state.sort
    for (const [k, v] of Object.entries(filters)) {
      if (v === '' || v === null || v === undefined) continue
      if (Array.isArray(v) && v.length === 0) continue
      params[k] = Array.isArray(v) ? v.join(',') : v
    }
    return { ...params, ...extra }
  }

  async function load(extra) {
    state.loading = true
    state.error = null
    try {
      const data = await fetcher(buildParams(extra))
      state.rows = data.items || []
      state.total = data.total || 0
      state.page = data.page || state.page
      state.size = data.size || state.size
    } catch (err) {
      state.error = err
      if (err && err.code !== 40001 && err.code !== 40002 && err.code !== 40003) {
        // already toasted by request interceptor unless suppressed code
      }
    } finally {
      state.loading = false
    }
  }

  function setPage(page) {
    state.page = page
    load()
  }
  function setSize(size) {
    state.size = size
    state.page = 1
    load()
  }
  function setSort(sort) {
    state.sort = sort
    load()
  }
  function setFilter(key, value) {
    filters[key] = value
  }
  function applyFilters() {
    state.page = 1
    load()
  }
  function resetFilters() {
    for (const k of Object.keys(filters)) {
      filters[k] = initialFilters[k] ?? (Array.isArray(filters[k]) ? [] : '')
    }
    state.page = 1
    load()
  }

  return {
    state,
    filters,
    load,
    setPage,
    setSize,
    setSort,
    setFilter,
    applyFilters,
    resetFilters,
    buildParams,
  }
}
