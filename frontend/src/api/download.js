import request from './request'

/**
 * Download a CSV/binary blob from the backend.
 * On error (most commonly 40101 for over-10000-rows), the server returns a JSON
 * payload even though the client requested a blob. We detect and re-throw with
 * the normalized `{code, message, data}` shape so `useDownload` can render a
 * single consistent toast instead of duplicating it here.
 */
export async function downloadBlob(url, params, fallbackName = 'export.csv') {
  const resp = await request.get(url, {
    params,
    responseType: 'blob',
  })
  const blob = resp.data
  const contentType = resp.headers?.['content-type'] || ''
  if (contentType.includes('application/json')) {
    const text = await blob.text()
    let body = null
    try {
      body = JSON.parse(text)
    } catch (_e) {
      // fall through — throw a synthetic error below
    }
    if (body && typeof body.code !== 'undefined') {
      throw body
    }
    throw { code: -1, message: '导出失败' }
  }
  const disposition = resp.headers?.['content-disposition'] || ''
  let filename = fallbackName
  const match = /filename\*?=(?:UTF-8''|")?([^;"\s]+)"?/i.exec(disposition)
  if (match && match[1]) {
    try {
      filename = decodeURIComponent(match[1])
    } catch (_e) {
      filename = match[1]
    }
  }
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(objectUrl)
  return filename
}

/**
 * Upload a single file as multipart/form-data. Used for Excel import.
 */
export function uploadFile(url, file, extra = {}) {
  const fd = new FormData()
  fd.append('file', file)
  for (const [k, v] of Object.entries(extra)) {
    fd.append(k, v)
  }
  return request.post(url, fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
