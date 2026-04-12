# 前端架构速览（给前端开发看）

> 完整架构见 `docs/architecture.md` §七，本文只摘要前端开发日常需要的关键信息。

## 技术栈

- Vue 3（Composition API + `<script setup>`）
- Element Plus
- Vite 5+
- Pinia（状态管理）
- Vue Router 4
- Axios（统一封装）
- ECharts 5（通过 vue-echarts，数据看板用）
- ESLint + Prettier

## 目录结构

```
frontend/
├── prototype/              原型 Demo（视觉参考，不进生产构建）
│   └── index.html
├── src/
│   ├── main.js             入口
│   ├── App.vue
│   ├── router/index.js     路由配置（15 个页面路径）
│   ├── stores/
│   │   ├── auth.js          登录态 / JWT token
│   │   └── app.js           全局状态（侧边栏收起、通知列表等）
│   ├── api/                 后端 API 调用封装（每个模块一个文件）
│   │   ├── request.js       Axios 实例 + 拦截器
│   │   ├── auth.js          login / me
│   │   ├── audit.js         审核工作台
│   │   ├── accounts.js      账号管理
│   │   ├── jobs.js          岗位管理
│   │   ├── resumes.js       简历管理
│   │   ├── dicts.js         字典管理
│   │   ├── config.js        系统配置
│   │   ├── reports.js       数据看板
│   │   └── logs.js          对话日志
│   ├── views/               页面组件（每页一个 .vue）
│   ├── components/
│   │   ├── layout/          侧边菜单 + 顶栏 + 主内容区
│   │   ├── PageTable.vue    通用表格（分页+筛选+排序+导出）
│   │   ├── DetailDrawer.vue 通用详情抽屉
│   │   └── ImagePreview.vue 图片预览 Modal
│   ├── composables/
│   │   ├── usePageTable.js  表格分页逻辑
│   │   └── useKeyboard.js   审核工作台键盘快捷键
│   └── utils/
│       ├── constants.js     枚举值 / 字典映射
│       └── format.js        日期格式化 / 脱敏显示
├── vite.config.js
└── package.json
```

## 开发规范（10 条）

1. **路由路径必须与方案设计 §13.4 一致**（`/admin/login`、`/admin/dashboard`、`/admin/audit` 等）
2. **API 调用统一走 `src/api/` 封装**，不在 view 里直接写 axios
3. **分页参数统一用 `page` + `size`**，与后端 `PageParams` 对齐
4. **表格组件尽量复用 `PageTable.vue`**，避免每页重写分页/排序逻辑
5. **审核工作台键盘快捷键**逻辑放在 `composables/useKeyboard.js`，与 UI 解耦
6. **所有列表页必须支持**：分页 / 排序 / 筛选 / 导出 CSV
7. **所有编辑操作必须有**：脏数据保护（离开前提醒）/ 保存 loading / 成功 Toast
8. **所有危险操作必须有**：二次确认弹窗
9. **深色模式**：使用 Element Plus 的 `dark` CSS 变量方案，不要硬写颜色
10. **错误处理统一在 Axios 拦截器**，view 里不需要重复 try/catch

## 页面清单（15 页）

| 优先级 | 页面 | 路由 |
|--------|------|------|
| **P0** | 登录 | `/admin/login` |
| **P0** | Dashboard | `/admin/dashboard` |
| **P0** | 审核工作台 | `/admin/audit` |
| **P1** | 厂家管理 | `/admin/accounts/factories` |
| **P1** | 岗位管理 | `/admin/jobs` |
| **P1** | 简历管理 | `/admin/resumes` |
| **P2** | 中介管理 | `/admin/accounts/brokers` |
| **P2** | 工人列表 | `/admin/accounts/workers` |
| **P2** | 黑名单 | `/admin/accounts/blacklist` |
| **P2** | 系统配置 | `/admin/config` |
| **P3** | 城市字典 | `/admin/dicts/cities` |
| **P3** | 工种字典 | `/admin/dicts/job-categories` |
| **P3** | 敏感词字典 | `/admin/dicts/sensitive-words` |
| **P3** | 数据看板 | `/admin/reports` |
| **P3** | 对话日志 | `/admin/logs/conversations` |

## 与后端通信

### 基础约定

| 项 | 规范 |
|---|---|
| 基础路径 | `/admin/*` |
| 数据格式 | `Content-Type: application/json`，UTF-8 |
| 鉴权 | JWT Bearer Token：`Authorization: Bearer <token>` |
| Token 获取 | `POST /admin/login` → `{ token, expires_at }` |
| Token 过期 | 24 小时，过期跳登录页 |

### 统一响应格式

```json
// 成功
{"code": 0, "message": "ok", "data": {...}}

// 分页
{"code": 0, "message": "ok", "data": {"items": [...], "total": 127, "page": 1, "size": 20, "pages": 7}}

// 错误
{"code": 40001, "message": "用户名或密码错误", "data": null}
```

### Axios 拦截器要点

**请求拦截器**：
- 从 Pinia store 读 JWT token，自动带 `Authorization` header
- token 为空且不是 `/admin/login` 请求 → 直接跳登录页

**响应拦截器**：
- `code === 0` → 返回 `response.data.data`
- `code === 40001`（token 过期）→ 清 token → 跳登录页
- 其它错误 → `ElMessage.error(response.data.message)`

### 错误码处理

| 范围 | 前端处理 |
|------|----------|
| `0` | 正常渲染 |
| `40001-40099` | 跳转登录页 |
| `40100-40199` | 表单字段高亮 |
| `40300-40399` | Toast 提示 |
| `40400-40499` | Toast 或空状态 |
| `50000-50099` | Toast "系统繁忙" |
| `50100-50199` | Toast "AI 服务暂时不可用" |

## 本地开发联调

```js
// vite.config.js
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
```

```bash
# 终端 1：基础设施（WSL2）
wsl -d Ubuntu-24.04 -- bash -c "cd /mnt/d/work/JobBridge && docker compose up -d"

# 终端 2：后端
cd backend && source .venv/Scripts/activate && uvicorn app.main:app --reload

# 终端 3：前端
cd frontend && npm run dev

# 浏览器打开 http://localhost:5173/admin/login
```

## 原型参考

`frontend/prototype/index.html` 是 P0 三页的 Tailwind CSS 原型 Demo。

**注意**：原型用 Tailwind，最终开发用 Element Plus，样式体系不同。原型作为**视觉参考和交互逻辑验证**，不可直接复用代码。对齐原型的信息层级、交互流程、键盘快捷键即可。

原型有 15 项已知缺陷（方案设计 §15.3），Phase 6 会修复其中影响 MVP 的 P0 项。

## 后端会依赖你的

- 前端 build 产物 `frontend/dist/` 会被 nginx 托管
- 审核工作台 UX 复杂度最高（方案设计 §13.9），需要前端重点投入
- 前端页面的边界状态（空队列、断网、并发冲突、超长内容等）需要主动处理
