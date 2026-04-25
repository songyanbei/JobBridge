import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'entry',
    component: () => import('@/views/MockEntryView.vue'),
  },
  {
    path: '/split',
    name: 'split',
    component: () => import('@/views/MockSplitView.vue'),
  },
  {
    path: '/single',
    name: 'single',
    component: () => import('@/views/MockSingleView.vue'),
  },
  // 兜底：其它任意路径回根
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
