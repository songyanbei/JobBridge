import request from './request'

export const listRecommendationStrategies = () => request.get('/admin/recommendation-strategies')
export const getRecommendationStrategy = (direction) => request.get(`/admin/recommendation-strategies/${direction}`)
export const createRecommendationDraft = (direction, payload) => request.post(`/admin/recommendation-strategies/${direction}/drafts`, payload)
export const updateRecommendationDraft = (id, payload) => request.put(`/admin/recommendation-strategies/drafts/${id}`, payload)
export const simulateRecommendationDraft = (id, payload) => request.post(`/admin/recommendation-strategies/drafts/${id}/simulate`, payload)
export const getRecommendationMetrics = (params) => request.get('/admin/recommendation-metrics', { params })
export const publishRecommendationCandidate = (id) => request.post(`/admin/recommendation-strategies/drafts/${id}/publish-candidate`)
export const updateRecommendationRelease = (direction, payload) => request.put(`/admin/recommendation-strategies/${direction}/release`, payload)
export const promoteRecommendationRelease = (direction, payload) => request.post(`/admin/recommendation-strategies/${direction}/promote`, payload)
export const rollbackRecommendationRelease = (direction, payload) => request.post(`/admin/recommendation-strategies/${direction}/rollback`, payload)
export const updateRecommendationKillSwitch = (payload) => request.put('/admin/recommendation-strategies/runtime-control/kill-switch', payload)
