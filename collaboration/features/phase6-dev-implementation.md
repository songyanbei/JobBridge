# Phase 6 开发实施文档

> 基于：`collaboration/features/phase6-main.md`
> 面向角色：前端开发
> 状态：`draft`
> 创建日期：2026-04-17

## 1. 开发目标

本阶段开发目标，是在 `frontend/` 下搭建并实现一套正式 Vue 3 运营后台 SPA，完成 15 个页面、统一 API 层、通用组件、审核工作台核心交互和 Phase 5 API 联调。

开发时请始终记住：

- Phase 6 只改 `frontend/`，不改 `backend/`
- 后端接口以 Phase 5 文档和 Swagger 为准，接口缺口写 `collaboration/handoffs/frontend-to-backend.md`
- 原型 `frontend/prototype/index.html` 只做视觉参考，不直接复用 Tailwind 代码
- 审核工作台是最高优先级页面，必须按生产力工具实现，而不是普通表格 CRUD
- 所有 API 请求必须统一走 `src/api/`
- 所有列表页必须有分页、筛选、排序、导出、loading、空状态和错误状态

## 2. 当前代码现状

当前 `frontend/` 只有：

- `prototype/index.html`

本阶段需要新建正式工程：

- `package.json`
- `vite.config.js`
- `index.html`
- `src/` 全量目录

默认采用 JavaScript 实现，避免当前空工程下引入 TS 配置成本。如团队在开工前明确决定使用 TypeScript，必须同步补充 `tsconfig`、类型声明、API 类型规范，并保持全工程一致。

## 3. 开发原则

### 3.1 目录边界

- 前端只修改 `frontend/`
- 不修改 `backend/`
- 不修改 `docs/architecture.md`、`方案设计_v0.1.md`，除非项目负责人要求回写基线文档
- 阻塞项写入 `collaboration/handoffs/frontend-to-backend.md`

### 3.2 依赖边界

- `views/*` 只调用 `api/*`，不直接 import axios
- `api/*` 只封装请求，不写页面交互逻辑
- `stores/*` 管登录态和全局状态，不承载复杂业务表单
- `components/*` 做通用展示与交互，不直接绑定某个页面接口
- `composables/*` 封装可复用逻辑，如表格、下载、键盘、脏数据保护

### 3.3 交互边界

- 后端返回的敏感字段过滤结果直接展示，不在前端自行做权限兜底
- 前端可以做输入校验，但不能替代后端业务校验
- 任何危险操作前端必须二次确认，但审核工作台单条“通过”不弹确认，依赖 Undo 兜底
- 批量操作必须确认，且一次最多 20 条
- token 失效时统一跳登录，不在每个页面重复处理

## 4. 工程搭建

### 4.1 推荐依赖

`package.json` 至少包含：

```json
{
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "vite build",
    "preview": "vite preview --host 0.0.0.0",
    "lint": "eslint src --ext .js,.vue",
    "format": "prettier --write \"src/**/*.{js,vue,css}\""
  },
  "dependencies": {
    "@element-plus/icons-vue": "^2.3.1",
    "axios": "^1.6.0",
    "echarts": "^5.5.0",
    "element-plus": "^2.7.0",
    "pinia": "^2.1.0",
    "vue": "^3.4.0",
    "vue-echarts": "^6.7.0",
    "vue-router": "^4.3.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "eslint": "^8.57.0",
    "eslint-plugin-vue": "^9.26.0",
    "prettier": "^3.2.0",
    "vite": "^5.2.0"
  }
}
```

版本可以按实际安装时的兼容版本调整，但不得替换技术栈。

### 4.2 `vite.config.js`

```js
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/api/events': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
```

说明：`/api/events` 是小程序后端事件回传的预留代理，Phase 6 运营后台前端不应主动调用；如页面代码中出现直接调用，需要先确认是否为新增需求。

### 4.3 目标目录结构

```text
frontend/
├── prototype/
├── public/
├── src/
│   ├── main.js
│   ├── App.vue
│   ├── router/
│   │   └── index.js
│   ├── stores/
│   │   ├── auth.js
│   │   └── app.js
│   ├── api/
│   │   ├── request.js
│   │   ├── auth.js
│   │   ├── audit.js
│   │   ├── accounts.js
│   │   ├── jobs.js
│   │   ├── resumes.js
│   │   ├── dicts.js
│   │   ├── config.js
│   │   ├── reports.js
│   │   └── logs.js
│   ├── views/
│   ├── components/
│   ├── composables/
│   └── utils/
├── index.html
├── package.json
└── vite.config.js
```

## 5. 基础模块实现

### 5.1 `main.js`

```js
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')
```

### 5.2 `stores/auth.js`

状态字段：

- `token`
- `expiresAt`
- `admin`
- `passwordChanged`
- `initialized`

必须提供方法：

- `login(payload)`
- `loadMe()`
- `changePassword(payload)`
- `logout()`
- `restoreFromStorage()`
- `isAuthenticated()`
- `isExpired()`

存储约定：

- localStorage key：`jobbridge_admin_token`
- localStorage key：`jobbridge_admin_expires_at`

安全要求：

- 不存 `password`
- 不存 `password_hash`
- token 过期时本地立即清理
- `isAuthenticated()` 必须同时检查 token 存在和 `!isExpired()`，路由守卫不得只判断 localStorage 中是否有 token

### 5.3 `api/request.js`

```js
import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

const request = axios.create({
  timeout: 20000,
})

request.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token && !config.headers.Authorization) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

request.interceptors.response.use(
  (response) => {
    const body = response.data
    if (!body || typeof body.code === 'undefined') {
      return body
    }
    if (body.code === 0) {
      return body.data
    }
    if ([40901, 40902, 40903].includes(body.code)) {
      return Promise.reject(body)
    }
    if ([40001, 40002, 40003].includes(body.code)) {
      if (router.currentRoute.value.path === '/admin/login') {
        return Promise.reject(body)
      }
      const auth = useAuthStore()
      auth.logout()
      ElMessage.error(body.message || '登录已失效，请重新登录')
      router.push('/admin/login')
      return Promise.reject(body)
    }
    ElMessage.error(body.message || '系统繁忙')
    return Promise.reject(body)
  },
  (error) => {
    ElMessage.error(error.message || '网络异常')
    return Promise.reject(error)
  },
)

export default request
```

注意：

- `40901/40902/40903` 是审核页页面级交互，request 层不得 Toast，必须直接 reject 给页面弹窗或按钮状态处理
- 登录接口返回 `40001` 时必须停留在 `/admin/login`，不得触发全局 logout + redirect
- 其它错误码优先使用具体 code switch-case；只有后端继续保持子区间语义时，才可使用区间作为兜底策略
- 页面需要表单字段高亮时，可捕获 `40100-40199` 错误中的 `data.fields`
- 文件下载接口不要被默认 JSON 处理吞掉，单独使用 `responseType: 'blob'`

### 5.4 路由与守卫

路由必须完整覆盖 15 页：

```js
const routes = [
  { path: '/', redirect: '/admin/dashboard' },
  { path: '/admin/login', component: () => import('@/views/login/LoginView.vue'), meta: { public: true } },
  {
    path: '/admin',
    component: () => import('@/components/layout/AdminLayout.vue'),
    children: [
      { path: 'dashboard', component: () => import('@/views/dashboard/DashboardView.vue') },
      { path: 'audit', component: () => import('@/views/audit/AuditWorkbenchView.vue') },
      { path: 'accounts/factories', component: () => import('@/views/accounts/FactoriesView.vue') },
      { path: 'accounts/brokers', component: () => import('@/views/accounts/BrokersView.vue') },
      { path: 'accounts/workers', component: () => import('@/views/accounts/WorkersView.vue') },
      { path: 'accounts/blacklist', component: () => import('@/views/accounts/BlacklistView.vue') },
      { path: 'jobs', component: () => import('@/views/jobs/JobsView.vue') },
      { path: 'resumes', component: () => import('@/views/resumes/ResumesView.vue') },
      { path: 'dicts/cities', component: () => import('@/views/dicts/CitiesView.vue') },
      { path: 'dicts/job-categories', component: () => import('@/views/dicts/JobCategoriesView.vue') },
      { path: 'dicts/sensitive-words', component: () => import('@/views/dicts/SensitiveWordsView.vue') },
      { path: 'config', component: () => import('@/views/config/ConfigView.vue') },
      { path: 'reports', component: () => import('@/views/reports/ReportsView.vue') },
      { path: 'logs/conversations', component: () => import('@/views/logs/ConversationLogsView.vue') },
    ],
  },
]
```

守卫规则：

- `meta.public` 页面不要求 token
- 无 token 访问受保护页面跳登录
- 有 token 且未过期时访问登录页跳 Dashboard
- token 过期时调用 `auth.logout()` 后跳登录
- 首次进入后台时调用 `auth.loadMe()`
- `passwordChanged=false` 时打开强制改密弹窗，未完成前阻止关闭

## 6. API 模块实现

### 6.1 `api/auth.js`

```js
import request from './request'

export function login(data) {
  return request.post('/admin/login', data)
}

export function getMe() {
  return request.get('/admin/me')
}

export function changePassword(data) {
  return request.put('/admin/me/password', data)
}
```

### 6.2 `api/audit.js`

```js
import request from './request'

export function fetchAuditQueue(params) {
  return request.get('/admin/audit/queue', { params })
}

export function fetchPendingCount() {
  return request.get('/admin/audit/pending-count')
}

export function fetchAuditDetail(targetType, id) {
  return request.get(`/admin/audit/${targetType}/${id}`)
}

export function lockAuditItem(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/lock`)
}

export function unlockAuditItem(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/unlock`)
}

export function passAuditItem(targetType, id, version) {
  return request.post(`/admin/audit/${targetType}/${id}/pass`, { version })
}

export function rejectAuditItem(targetType, id, data) {
  return request.post(`/admin/audit/${targetType}/${id}/reject`, data)
}

export function editAuditItem(targetType, id, data) {
  return request.put(`/admin/audit/${targetType}/${id}/edit`, data)
}

export function undoAuditAction(targetType, id) {
  return request.post(`/admin/audit/${targetType}/${id}/undo`)
}
```

### 6.3 其它 API 文件

按 Phase 5 API 清单逐个封装。命名约定：

- `fetchXxxList(params)`
- `fetchXxxDetail(id)`
- `createXxx(data)`
- `updateXxx(id, data)`
- `deleteXxx(id)`
- `exportXxx(params)`
- `importXxx(file)`

不要在页面里临时拼路径。

## 7. 通用组件实现

### 7.1 `PageTable.vue`

职责：

- 渲染筛选区插槽
- 渲染表格
- 管理分页控件
- 透传排序事件
- 渲染导出按钮
- loading / empty / error 状态

建议 props：

- `columns`
- `rows`
- `loading`
- `total`
- `page`
- `size`
- `rowKey`
- `selectable`
- `exportable`

建议 emits：

- `update:page`
- `update:size`
- `sort-change`
- `selection-change`
- `export`
- `refresh`

### 7.2 `DetailDrawer.vue`

职责：

- 详情展示容器
- 可编辑状态切换
- 保存 / 取消
- 脏数据离开提醒

必须支持：

- `before-close` 拦截
- 保存 loading
- 错误展示

### 7.3 `ImagePreview.vue`

必须支持：

- 缩略图打开
- Modal 大图
- 左右切换
- ESC 关闭
- 图片加载失败占位

### 7.4 `ConfirmAction.vue`

用于危险操作：

- 封禁
- 解封
- 下架
- 招满
- 延期
- 删除字典项
- 危险配置变更
- 批量审核

审核单条“通过”不使用二次确认。

### 7.5 `UploadImportDialog.vue`

用于厂家 / 中介 Excel 导入：

- 仅允许 `.xlsx`
- 显示文件名和大小
- 上传中 loading
- 后端返回失败明细时用表格展示 row / error
- 全部成功显示 success_count
- Excel 导入是全量事务：只要存在失败行，本次整体失败，UI 必须显示 `success_count=0`，并用失败明细解释原因，不得展示“部分导入成功”

### 7.6 `JsonEditor.vue`

用于系统配置 JSON 类型：

- 使用 textarea 即可
- 保存前 `JSON.parse` 校验
- 校验失败阻止提交并高亮

## 8. 页面实现指引

### 8.1 登录页

文件：

- `views/login/LoginView.vue`

要求：

- 默认显示用户名、密码输入框
- Enter 触发登录
- 登录按钮 loading
- loading 期间禁用按钮，避免连续失败触发后端 sleep 1s 时重复提交
- 登录失败 Toast
- 登录失败必须停留在当前表单，不触发全局路由重定向
- 登录成功跳 `/admin/dashboard`
- `password_changed=false` 时打开强制改密弹窗
- 修复原型“admin123 反而失败”的逻辑错误

### 8.2 Dashboard

文件：

- `views/dashboard/DashboardView.vue`

实现步骤：

1. 调 `getDashboard()`
2. 调 `fetchPendingCount()`
3. 渲染指标卡片
4. 默认渲染 dashboard 响应内的 `trend_7d`
5. 渲染待办入口
6. 时间范围选择 30d 时，调用 `/admin/reports/trends?range=30d` 刷新趋势图；custom 不在 Dashboard 提供，跳转 `/admin/reports`
7. 设置 60 秒刷新定时器，组件卸载时清理

注意：

- 后端异常时保留页面骨架并显示错误状态
- 指标值为空时显示 `--`
- 命中率 / 空召回率按百分比格式化
- 不要给 `/admin/reports/dashboard` 追加 `range` 参数，该接口固定返回 today / yesterday / trend_7d

### 8.3 审核工作台

文件建议：

```text
views/audit/
├── AuditWorkbenchView.vue
├── components/
│   ├── AuditCard.vue
│   ├── AuditQueueList.vue
│   ├── AuditSuggestionPanel.vue
│   ├── RejectPanel.vue
│   ├── SubmitterHistoryDrawer.vue
│   └── UndoToast.vue
```

核心状态：

- `activeTab`
- `mode`：`card | list`
- `queue`
- `currentItem`
- `detail`
- `locked`
- `lockRenewTimer`
- `undoDeadline`
- `selectedRows`
- `networkOnline`

处理流程：

1. 进入页面加载队列
2. 选择第一条待审
3. 调 `lockAuditItem(target_type, id)`
4. lock 成功后调 `fetchAuditDetail`
5. 启动 4 分钟软锁续约定时器，重复调用 `lockAuditItem(target_type, id)` 刷新 TTL
6. 用户执行通过 / 驳回 / 编辑
7. 请求带 `version`
8. 成功后显示 Undo 倒计时
9. 切换下一条前停止续约并调用 unlock
10. 组件卸载时停止续约并调用 unlock

软锁续约要求：

- 后端锁 TTL 为 300 秒，前端续约间隔固定 4 分钟
- 再次调用 `/lock` 表示刷新 TTL，不重新拉详情
- 续约失败或返回 `40901` 时，页面退出编辑态，提示锁已失效并要求刷新队列
- 页面隐藏或浏览器卸载时尽力 unlock；如果 unlock 失败，不阻塞页面离开，但必须记录 console warning 便于联调排查

冲突处理：

- `40901` 软锁冲突：展示锁持有人，不进入编辑态
- `40902` 乐观锁冲突：提示刷新详情，按钮禁用
- `40903` Undo 超时：关闭 Undo 按钮并刷新状态

批量审核：

- 后端没有 batch_pass / batch_reject 契约，不允许伪造批量 API
- 前端批量模式仅对已选的最多 20 条执行串行单条审核
- 每条按 lock → detail/version 校验 → pass/reject → unlock 执行
- 任一条失败立即中断后续请求，不回滚已经成功的条目
- 结果面板必须展示已成功数量、失败条目、失败 code/message，并提供刷新队列入口

LLM 置信度：

- 如果详情响应含 `field_confidence`，渲染绿 / 黄 / 红点和低置信字段淡红高亮
- 如果详情响应不含 `field_confidence`，隐藏置信度 UI，不阻塞审核主流程
- 如果产品验收要求必须展示该字段，按 §9 写入 handoff

快捷键：

- `P`：当前 detail 存在且无弹窗时通过
- `R`：打开驳回面板
- `S`：跳过当前条，切下一条
- `E`：进入编辑态
- `U`：Undo 可用时撤销
- `?`：打开帮助 Modal

快捷键必须在输入框聚焦时失效，避免影响表单输入。

驳回面板：

- 预设理由建议：
  - 信息不完整
  - 联系方式异常
  - 薪资描述不清
  - 疑似虚假信息
  - 含敏感词
  - 重复发布
  - 岗位已失效
  - 其他
- 自由文本理由
- `notify`
- `block_user`
- 未填理由时提交按钮 disabled

### 8.4 账号管理

文件建议：

```text
views/accounts/
├── FactoriesView.vue
├── BrokersView.vue
├── WorkersView.vue
├── BlacklistView.vue
└── components/
    ├── AccountTable.vue
    ├── AccountDetailDrawer.vue
    ├── PreRegisterDialog.vue
    └── AccountImportDialog.vue
```

厂家 / 中介：

- 复用 `AccountTable`
- 预注册弹窗字段：
  - role
  - display_name
  - company
  - contact_person
  - phone
  - external_userid
  - 中介额外 `can_search_jobs / can_search_workers`
- Excel 导入弹窗复用 `UploadImportDialog`
- 导入失败时遵循全量回滚语义：任一行失败即整体失败，页面展示 `success_count=0` 和失败行明细

工人：

- 只读列表
- 不展示新增 / 导入 / 编辑按钮

黑名单：

- 解封操作必须输入理由
- 成功后刷新列表

### 8.5 岗位 / 简历管理

岗位文件：

- `views/jobs/JobsView.vue`

简历文件：

- `views/resumes/ResumesView.vue`

共用组件：

- `EntityTable`
- `EntityDetailDrawer`
- `EntityEditForm`

岗位操作：

- 编辑：`PUT /admin/jobs/{id}`，带 `version`
- 下架：`POST /admin/jobs/{id}/delist`，reason=`manual_delist`
- 招满：`POST /admin/jobs/{id}/delist`，reason=`filled`
- 延期：`POST /admin/jobs/{id}/extend`，days=`15 | 30`
- 取消下架：`POST /admin/jobs/{id}/restore`
- 导出：`GET /admin/jobs/export`

岗位筛选必须包含 `pay_type` 支付方式（日结 / 月结）。

简历操作：

- 编辑：`PUT /admin/resumes/{id}`，带 `version`
- 下架：`POST /admin/resumes/{id}/delist`
- 延期：`POST /admin/resumes/{id}/extend`，days=`15 | 30`
- 导出：`GET /admin/resumes/export`
- 简历没有 restore / 取消下架接口，页面不得展示对应按钮

### 8.6 字典管理

城市：

- 按省份折叠
- aliases 用 tag input
- 只允许保存 aliases

工种：

- 表格 CRUD
- sort_order 编辑
- 删除前确认

敏感词：

- 表格 CRUD
- 批量导入 textarea
- 一行一个词
- 返回 added / duplicated 后展示摘要

### 8.7 系统配置

实现要求：

- 接口返回分组对象，前端按分组渲染折叠面板
- 每项独立编辑，不做“保存全部”
- `value_type=bool` 用 Switch
- `value_type=int` 用 InputNumber
- `value_type=json` 用 JsonEditor
- `value_type=string` 用 Input
- 危险项保存前弹确认
- 危险项以接口返回的 `danger` 字段为准；下方默认 key 仅作为后端缺失 `danger` 字段时的兜底

危险项默认包含：

- `filter.enable_gender`
- `filter.enable_age`
- `filter.enable_ethnicity`
- `llm.provider`

### 8.8 数据看板

实现要求：

- ECharts 组件封装为 `ChartCard`
- `range=7d|30d|custom`
- custom 模式必须填写 from / to
- custom 的 from/to 跨度不得超过 90 天；前端优先阻止，后端返回 `40101` 时展示可读错误
- trends / top / funnel 独立 loading
- 导出按钮调用 Blob 下载

### 8.9 对话日志

实现要求：

- 查询前必须填写 userid 和 start/end
- 前端限制最大 30 天
- 时间线使用聊天气泡样式
- `criteria_snapshot` 默认折叠
- 导出按钮复用 `useDownload`
- CSV 导出超过 10000 行时，按 `40101` 展示“导出数据量过大，请缩小筛选范围”

## 9. Handoff 规则

如果发现以下问题，写入 `collaboration/handoffs/frontend-to-backend.md`：

- API 路径与 Phase 5 文档不一致
- 响应字段缺失
- 错误码不在文档范围内
- 分页结构不是 `items/total/page/size/pages`
- 审核详情缺少 `version / locked_by / risk_level / submitter_history`
- 审核详情缺少产品强制要求展示的 `field_confidence`
- 下载接口未返回 Blob 或缺文件名
- Excel 导入错误明细缺 row / error
- Excel 导入有失败行但仍返回部分成功，不符合全量回滚语义
- 配置项缺 value_type
- 配置项缺 danger，导致危险项只能硬编码判断

记录格式建议：

```md
## [open] 2026-04-17 Phase 6 前端发现：审核详情缺 field_confidence

- 发现人：
- 影响页面：`/admin/audit`
- 当前接口：`GET /admin/audit/job/1`
- 当前返回：
- 期望返回：
- 阻塞程度：高 / 中 / 低
```

形成结论后，回填到 `phase6-main.md` 或对应实施文档。

## 10. 开发顺序建议

1. 工程脚手架：package、Vite、main、App
2. 路由、Pinia、API request、登录页
3. Layout：侧边栏、顶栏、头像菜单、待审 badge
4. Dashboard：建立图表与指标展示模式
5. 审核工作台：先完成单卡全链路，再做列表模式和批量
6. 通用 PageTable、DetailDrawer、ImagePreview、ConfirmAction
7. P1 页面：厂家、岗位、简历
8. P2 页面：中介、工人、黑名单、系统配置
9. P3 页面：字典、数据看板、对话日志
10. 全量联调与 P0 原型缺陷关闭
11. 构建产物验证和 nginx 托管准备

## 11. 测试辅助

开发自测至少覆盖：

- `npm run dev` 可启动
- `npm run build` 成功
- 登录成功 / 失败
- 未登录访问后台跳登录
- token 失效跳登录
- 每个页面至少打开一次无白屏
- 每个 API 模块至少被页面调用一次
- 审核工作台完整走通一条：lock → 4 分钟续锁 → detail → pass → undo → unlock
- 导出 CSV 可下载
- 导出超过 10000 行时 `40101` 可读
- Excel 导入失败明细可展示
- Excel 导入任一行失败时按整体失败展示 `success_count=0`
- Vitest 至少覆盖 auth store、request 拦截器、PageTable、UploadImportDialog、ConfirmAction

## 12. 注意事项

1. 不要在页面里直接写 axios
2. 不要让 token 过期错误在页面里各自处理
3. 不要把审核工作台做成只有表格的普通列表
4. 不要省略软锁续约和 unlock，审核详情每 4 分钟续约，离开页面和切换条目都要释放软锁
5. 不要在输入框聚焦时触发快捷键
6. 不要用 Space 做通过快捷键
7. 不要让批量操作超过 20 条，也不要调用不存在的 batch API
8. 不要把 JSON 配置未校验就提交
9. 不要用前端隐藏字段替代后端权限过滤
10. 不要直接复用 prototype 的 Tailwind 类名和 DOM 结构
