import request from './request'

export const listRecommendationStrategies = () => request.get('/admin/recommendation-strategies')
export const getRecommendationStrategy = (direction) => request.get(`/admin/recommendation-strategies/${direction}`)
export const createRecommendationDraft = (direction, payload) => request.post(`/admin/recommendation-strategies/${direction}/drafts`, payload)
export const updateRecommendationDraft = (id, payload) => request.put(`/admin/recommendation-strategies/drafts/${id}`, payload)
export const simulateRecommendationDraft = (id, payload) => request.post(`/admin/recommendation-strategies/drafts/${id}/simulate`, payload)
export const getRecommendationMetrics = (params) => request.get('/admin/recommendation-metrics', { params })
