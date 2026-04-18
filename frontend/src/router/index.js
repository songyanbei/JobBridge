import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  { path: '/', redirect: '/admin/dashboard' },
  {
    path: '/admin/login',
    name: 'login',
    component: () => import('@/views/login/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/admin',
    component: () => import('@/components/layout/AdminLayout.vue'),
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/DashboardView.vue'),
        meta: { title: '首页', menu: 'dashboard' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/views/audit/AuditWorkbenchView.vue'),
        meta: { title: '审核工作台', menu: 'audit' },
      },
      {
        path: 'accounts/factories',
        name: 'factories',
        component: () => import('@/views/accounts/FactoriesView.vue'),
        meta: { title: '厂家管理', menu: 'accounts/factories', parent: 'accounts' },
      },
      {
        path: 'accounts/brokers',
        name: 'brokers',
        component: () => import('@/views/accounts/BrokersView.vue'),
        meta: { title: '中介管理', menu: 'accounts/brokers', parent: 'accounts' },
      },
      {
        path: 'accounts/workers',
        name: 'workers',
        component: () => import('@/views/accounts/WorkersView.vue'),
        meta: { title: '工人列表', menu: 'accounts/workers', parent: 'accounts' },
      },
      {
        path: 'accounts/blacklist',
        name: 'blacklist',
        component: () => import('@/views/accounts/BlacklistView.vue'),
        meta: { title: '黑名单', menu: 'accounts/blacklist', parent: 'accounts' },
      },
      {
        path: 'jobs',
        name: 'jobs',
        component: () => import('@/views/jobs/JobsView.vue'),
        meta: { title: '岗位管理', menu: 'jobs' },
      },
      {
        path: 'resumes',
        name: 'resumes',
        component: () => import('@/views/resumes/ResumesView.vue'),
        meta: { title: '简历管理', menu: 'resumes' },
      },
      {
        path: 'dicts/cities',
        name: 'dict-cities',
        component: () => import('@/views/dicts/CitiesView.vue'),
        meta: { title: '城市字典', menu: 'dicts/cities', parent: 'dicts' },
      },
      {
        path: 'dicts/job-categories',
        name: 'dict-job-categories',
        component: () => import('@/views/dicts/JobCategoriesView.vue'),
        meta: { title: '工种字典', menu: 'dicts/job-categories', parent: 'dicts' },
      },
      {
        path: 'dicts/sensitive-words',
        name: 'dict-sensitive-words',
        component: () => import('@/views/dicts/SensitiveWordsView.vue'),
        meta: { title: '敏感词字典', menu: 'dicts/sensitive-words', parent: 'dicts' },
      },
      {
        path: 'config',
        name: 'config',
        component: () => import('@/views/config/ConfigView.vue'),
        meta: { title: '系统配置', menu: 'config' },
      },
      {
        path: 'reports',
        name: 'reports',
        component: () => import('@/views/reports/ReportsView.vue'),
        meta: { title: '数据看板', menu: 'reports' },
      },
      {
        path: 'logs/conversations',
        name: 'conversation-logs',
        component: () => import('@/views/logs/ConversationLogsView.vue'),
        meta: { title: '对话日志', menu: 'logs/conversations' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/admin/dashboard',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    return { top: 0 }
  },
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  auth.restoreFromStorage()

  if (to.meta && to.meta.public) {
    if (to.path === '/admin/login' && auth.isAuthenticated) {
      return { path: '/admin/dashboard' }
    }
    return true
  }

  if (!auth.isAuthenticated) {
    auth.clear()
    return { path: '/admin/login' }
  }

  if (!auth.admin) {
    try {
      await auth.loadMe()
    } catch (_e) {
      // request interceptor will redirect to login
      return { path: '/admin/login' }
    }
  }

  return true
})

router.afterEach((to) => {
  const base = 'JobBridge 运营后台'
  document.title = to.meta?.title ? `${to.meta.title} · ${base}` : base
})

export default router
