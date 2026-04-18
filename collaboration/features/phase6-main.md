# Feature: Phase 6 运营后台前端

> 状态：`draft`
> 创建日期：2026-04-17
> 对应实施阶段：Phase 6
> 关联实施文档：`docs/implementation-plan.md` §4.7
> 关联方案设计章节：§13（运营后台）、§15（原型状态）、§17.1（验收指标）
> 关联架构章节：`docs/architecture.md` §七（前端架构、通信契约、Admin API 清单）
> 依赖阶段：Phase 5 运营后台后端接口
> 配套文档：
> - 开发实施文档：`collaboration/features/phase6-dev-implementation.md`
> - 开发 Checklist：`collaboration/features/phase6-dev-checklist.md`
> - 测试实施文档：`collaboration/features/phase6-test-implementation.md`
> - 测试 Checklist：`collaboration/features/phase6-test-checklist.md`

## 1. 阶段目标

Phase 6 的目标，是把 `frontend/` 从原型目录升级为一套可运行、可联调、可交付的 Vue 3 运营后台 SPA，让运营人员可以通过浏览器完成日常审核、账号管理、岗位/简历维护、字典维护、配置修改、数据查看和对话溯源。

本阶段完成后，项目至少应具备以下能力：

- 管理员可访问 `/admin/login` 登录，获取 JWT 后进入后台，刷新页面后登录态可恢复
- 所有 `/admin/*` 页面均通过统一 Layout 进入，侧边菜单、顶栏、面包屑、待审 badge、头像菜单可用
- 审核工作台支持待审 / 已通过 / 已驳回 tab、单卡精读模式、列表速览模式、软锁、乐观锁、通过、驳回、编辑、Undo、键盘快捷键
- Dashboard 可展示核心指标、昨日对比、趋势图、待办入口，并支持 60 秒静默刷新
- 厂家、中介、工人、黑名单、岗位、简历、字典、配置、报表、对话日志页面可按 Phase 5 API 完成完整操作闭环
- 所有 API 调用统一经过 `src/api/request.js`，统一处理 token、响应解包、错误提示和登录跳转
- 所有列表页统一支持分页、筛选、排序、导出 CSV；所有危险操作有二次确认；所有编辑操作有脏数据离开提醒
- `方案设计_v0.1.md` §15.3 中影响 MVP 使用效率的 P0 原型缺陷全部关闭
- 前端构建产物 `frontend/dist/` 可被 nginx 或后端静态托管，支持 Phase 7 Docker 联调

## 2. 当前代码现状

当前 `frontend/` 目录只有原型参考：

- `frontend/prototype/index.html`：单文件 HTML + Tailwind CSS + Font Awesome CDN
- 原型覆盖 P0 三页：登录 / Dashboard / 审核工作台
- 原型存在 15 个 P0 缺陷，详见方案设计 §15.3
- 当前没有正式 Vue 工程，没有 `package.json`、`src/`、`vite.config.js`、路由、Pinia、API 封装、Element Plus 集成

Phase 6 需要从零搭建正式前端工程。原型只作为视觉信息层级、交互流程和审核工作台体验参考，不允许直接复用 Tailwind 原型代码作为生产实现。

当前后端契约来源：

- Phase 5 主需求文档：`collaboration/features/phase5-main.md`
- 架构文档：`docs/architecture.md` §7.3、§7.4
- 后端 Swagger UI：开发环境 `http://localhost:8000/docs`

如 Phase 6 开发中发现 Phase 5 API 字段、错误码或行为与文档不一致，前端同事必须在 `collaboration/handoffs/frontend-to-backend.md` 追加 `open` 记录，不直接修改 `backend/`。

## 3. 本阶段范围

### 3.1 必须完成的模块

#### 模块 A：前端工程基础

- 创建正式 Vue 3 + Vite 项目结构
- 集成 Element Plus、Pinia、Vue Router、Axios、ECharts
- 建立 `src/api/`、`src/stores/`、`src/router/`、`src/views/`、`src/components/`、`src/composables/`、`src/utils/` 目录
- 配置 Vite dev server 端口 `5173`
- 配置 Vite 代理：`/admin`、`/api/events` 转发到 `http://localhost:8000`
  - `/api/events` 仅为未来扩展预留，Phase 6 运营后台前端不直接调用事件回传接口
- 配置基础 lint / format 脚本
- 生成可部署构建产物 `frontend/dist/`

#### 模块 B：统一 API 层

- `src/api/request.js`
  - Axios 实例
  - 自动注入 `Authorization: Bearer <token>`
  - `code === 0` 自动返回 `data`
  - `40001/40002/40003` 鉴权错误清 token 并跳登录；当前页面是 `/admin/login` 时不得再次重定向，只由登录表单展示错误
  - `40100-40199` 表单错误可被页面接收并高亮
  - `40901/40902/40903` 不做全局 Toast，直接 reject 给审核页处理锁冲突、版本冲突和 Undo 超时
  - 其它错误统一 Toast；错误码优先按具体 code 处理，不按大区间吞掉页面级交互
- 每个业务模块一个 API 文件：`auth.js`、`audit.js`、`accounts.js`、`jobs.js`、`resumes.js`、`dicts.js`、`config.js`、`reports.js`、`logs.js`
- 文件下载统一封装，支持 CSV 导出文件名和 Blob 处理
- Excel 上传统一封装，支持厂家 / 中介批量导入

#### 模块 C：登录、鉴权与路由守卫

- `/admin/login`
  - 用户名 / 密码登录
  - Enter 提交
  - 错误 Toast
  - 登录失败停留在登录页，不清理其它页面态，不触发路由重定向
  - 登录按钮请求中保持 loading，防止服务端连续 3 次失败 sleep 1s 时重复提交
  - 登录成功保存 token、expires_at、password_changed
  - `password_changed=false` 时强制进入改密码流程
- `GET /admin/me`
  - 初始化登录态
  - 刷新页面后恢复管理员信息
- `PUT /admin/me/password`
  - 首次登录强制改密
  - 头像菜单手动改密
- 路由守卫：
  - 未登录访问后台页面跳 `/admin/login`
  - 已登录访问 `/admin/login` 跳 `/admin/dashboard`
  - token 过期或无效清理登录态并跳登录

#### 模块 D：全局框架 Layout

- 左侧菜单：
  - Dashboard
  - 审核工作台（带待审 badge）
  - 账号管理：厂家 / 中介 / 工人 / 黑名单
  - 岗位管理
  - 简历管理
  - 字典管理：城市 / 工种 / 敏感词
  - 系统配置
  - 数据看板
  - 对话日志
- 菜单层级约定：
  - 15 个页面入口均必须可见或可通过一级分组展开后可见
  - “账号管理”和“字典管理”允许作为折叠分组；当前路由命中子页面时父分组必须自动展开，待审 badge 不得被折叠隐藏
- 顶栏：
  - 页面标题 / 面包屑
  - 全局 userid 搜索入口
  - 通知铃铛
  - 管理员头像菜单：改密码 / 退出登录
- 主内容区局部滚动，不让整个浏览器页面滚动失控
- 支持侧边栏展开 / 收起
- 支持深色模式开关，使用 Element Plus CSS 变量，不硬写颜色

#### 模块 E：Dashboard

- 路由：`/admin/dashboard`
- API：
  - `GET /admin/reports/dashboard`
  - `GET /admin/audit/pending-count`
  - `GET /admin/reports/trends?range=30d`，仅当 Dashboard 选择 30d 趋势时调用
- 页面能力：
  - 5-6 个指标卡片：DAU、上传数、检索次数、命中率、空召回率、待审积压
  - 昨日对比展示
  - 7 天趋势图，数据来自 dashboard 响应内的 `trend_7d`
  - 待办入口面板
  - 时间范围切换入口：7d 使用 dashboard 内 `trend_7d`；30d 调 `/admin/reports/trends?range=30d`；custom 范围仅在 `/admin/reports` 数据看板页提供
  - 60 秒静默刷新
  - 指标卡片可点击跳转到对应页面或报表页
- 必须修复原型缺陷：
  - “岗+历”改为“岗+简”或更明确的“岗位+简历”
  - 补“命中率”指标
  - 补时间范围切换器
  - 所有指标卡片绑定跳转或明确置灰不可点状态

#### 模块 F：审核工作台

- 路由：`/admin/audit`
- API：
  - `GET /admin/audit/queue`
  - `GET /admin/audit/pending-count`
  - `GET /admin/audit/{target_type}/{target_id}`
  - `POST /admin/audit/{target_type}/{target_id}/lock`
  - `POST /admin/audit/{target_type}/{target_id}/unlock`
  - `POST /admin/audit/{target_type}/{target_id}/pass`
  - `POST /admin/audit/{target_type}/{target_id}/reject`
  - `PUT /admin/audit/{target_type}/{target_id}/edit`
  - `POST /admin/audit/{target_type}/{target_id}/undo`
- 页面能力：
  - 待审 / 已通过 / 已驳回 tab
  - 单卡精读模式
  - 列表速览模式
  - 批量模式，上限 20 条，批量操作必须二次确认
  - 批量审核无独立后端 batch API，本阶段按前端串行循环单条接口实现；逐条执行 lock → detail/version 校验 → pass/reject → unlock，遇到任一失败立即中断，已成功条目不回滚，结果面板展示成功数、失败条目和失败原因
  - 打开详情自动 lock，离开 / 切换 / 页面卸载时 unlock
  - 审核详情停留超过 4 分钟时必须再次调用 `/lock` 续约软锁；lock TTL 为 300 秒，续约失败后退出编辑态并提示刷新
  - lock 冲突展示“xxx 正在审核此条目”，支持刷新队列，不默认强制接管
  - 通过请求带 `version`，成功后进入下一条，并出现 Undo 倒计时
  - 驳回必须填写理由，支持预设理由 + 自由文本 + `notify` + `block_user`
  - 编辑结构化字段必须带 `version`，保存成功刷新详情
  - Undo 30 秒内可点，超时后按钮自动失效
  - 键盘快捷键：`P` 通过、`R` 驳回、`S` 稍后、`E` 编辑、`U` Undo、`?` 帮助
  - 不使用 Space 作为通过快捷键
  - 图片缩略图 + 大图预览 Modal + 左右切换
  - 提交者 7 天审核历史内联展示，完整历史 Drawer
  - LLM 置信度用绿 / 黄 / 红视觉标识，低置信字段整行淡红；仅当后端返回 `field_confidence` 时展示，未返回时降级为不展示且不阻塞审核
  - 风险等级影响整卡边框和顶部警告条
  - 每 50 条出现温和休息提示
- 必须修复原型缺陷：
  - 增加 LLM 审核建议 C 块
  - 增加驳回理由面板
  - 增加图片大图预览
  - 增加提交者历史 Drawer
  - 实现列表模式切换
  - 增加风险等级整卡边框色
  - 快捷键改为 `P/R/S/E/U/?`
  - 增加快捷键帮助 Modal
  - 增加空队列 / 断网 / LLM 故障 / 并发冲突 / 版本冲突等边界状态

#### 模块 G：账号管理

- 厂家路由：`/admin/accounts/factories`
- 中介路由：`/admin/accounts/brokers`
- 工人路由：`/admin/accounts/workers`
- 黑名单路由：`/admin/accounts/blacklist`
- 页面能力：
  - 厂家 / 中介 / 工人共用列表模板：搜索、筛选、分页、排序、详情抽屉、导出
  - 厂家 / 中介支持预注册弹窗
  - 厂家 / 中介支持 Excel 批量导入，显示成功 / 失败明细；导入具备事务性全量回滚语义，任一行失败则本次整体失败，页面显示 `success_count=0` 与失败行明细
  - 中介详情支持 `can_search_jobs / can_search_workers`
  - 工人只读，不提供新增、编辑、导入入口
  - 封禁必须填写理由，解封必须二次确认并填写理由
  - 黑名单列表支持解封

#### 模块 H：岗位管理与简历管理

- 岗位路由：`/admin/jobs`
- 简历路由：`/admin/resumes`
- 页面能力：
  - 高级筛选
  - 表格列表
  - 详情抽屉
  - 编辑字段表单
  - 乐观锁冲突处理
  - 下架 / 招满 / 延期 / 取消下架
  - CSV 导出
  - TTL 剩余天数用绿 / 黄 / 红分级
  - 图片附件预览
- 岗位筛选维度至少覆盖：
  - 城市、区县、工种、薪资区间、支付方式 `pay_type`、审核状态、下架原因、发布人、创建时间、过期时间
- 简历筛选维度至少覆盖：
  - 性别、年龄区间、期望城市、期望工种、审核状态、发布人、创建时间
- 简历管理不提供“取消下架 / restore”动作；简历仅支持后端已定义的编辑、下架、延期、导出等操作

#### 模块 I：字典管理

- 城市字典：`/admin/dicts/cities`
  - 342 城按省份折叠分组
  - 仅编辑别名 aliases
  - 别名使用 Tag 输入
- 工种字典：`/admin/dicts/job-categories`
  - CRUD
  - 排序通过 `sort_order` 编辑
  - 删除前展示后端返回的引用冲突
- 敏感词字典：`/admin/dicts/sensitive-words`
  - CRUD
  - 按等级 / 分类 / 关键词筛选
  - 批量粘贴导入，一行一个词
  - 展示新增数量和重复数量

#### 模块 J：系统配置

- 路由：`/admin/config`
- API：
  - `GET /admin/config`
  - `PUT /admin/config/{key}`
- 页面能力：
  - 按命名空间折叠分组：`ttl / match / filter / audit / llm / session / upload / report / rate_limit / event / account`
  - 每项独立编辑和保存，避免误改
  - 根据 `value_type` 渲染输入控件：int、bool、json、string
  - JSON 值保存前做前端语法校验
  - 危险项保存前二次确认，并展示影响说明；危险项以接口返回的 `danger` 字段为准，`filter.* / llm.provider` 仅作为接口缺字段时的兜底默认
  - 保存成功 Toast，失败保留用户输入

#### 模块 K：数据看板

- 路由：`/admin/reports`
- API：
  - `GET /admin/reports/trends`
  - `GET /admin/reports/top`
  - `GET /admin/reports/funnel`
  - `GET /admin/reports/export`
- 页面能力：
  - 时间范围选择器：7d / 30d / custom
  - custom 的 from/to 跨度不得超过 90 天；超过时前端阻止提交或展示后端 `40101` 可读错误
  - 多指标趋势图
  - TOP 榜单
  - 转化漏斗
  - CSV 导出；单次导出超过 10000 行时后端返回 `40101`，前端必须展示可读提示并建议缩小筛选范围
  - 图表加载、空数据、错误状态
- 本阶段不做自定义看板、不做告警规则配置。

#### 模块 L：对话日志

- 路由：`/admin/logs/conversations`
- API：
  - `GET /admin/logs/conversations`
  - `GET /admin/logs/conversations/export`
- 页面能力：
  - 必须先输入 userid + 时间范围才允许查询
  - 时间范围最大 30 天
  - 聊天气泡时间线
  - 支持方向、intent 筛选
  - 系统回复可展开 `criteria_snapshot`
  - CSV 导出；单次导出超过 10000 行时后端返回 `40101`，前端必须展示可读提示并建议缩小筛选范围
- 本阶段不做全文检索。

#### 模块 M：通用组件与通用交互

- `Layout`
- `PageTable`
- `DetailDrawer`
- `ImagePreview`
- `ConfirmAction`
- `JsonEditor`
- `CsvExportButton`
- `UploadImportDialog`
- `EmptyState`
- `ErrorState`
- `KeyboardHelpModal`
- `usePageTable`
- `useDirtyGuard`
- `useKeyboard`
- `useDownload`

通用规则：

- 所有页面必须有 loading 状态
- 所有列表必须有空状态
- 所有接口错误必须可见
- 所有编辑表单必须有保存中状态
- 所有危险操作必须有二次确认
- 所有导出必须显示导出中状态
- 所有 CSV 导出单次上限 10000 行；收到 `40101` 时提示用户缩小筛选范围后重试
- 所有表格分页参数固定 `page / size`

### 3.2 本阶段明确不做

- 不开发独立 Web / App 用户端
- 不开发文档未定义的新后台页面
- 不实现 RBAC、多管理员权限分级、找回密码
- 不实现 Prompt 热更新 UI
- 不实现自定义看板、告警规则配置、审核员绩效统计
- 不实现对话日志全文检索
- 不在前端做真实权限控制兜底，权限与敏感字段过滤必须依赖后端
- 不在前端绕过 Phase 5 API 直接访问数据库、Redis 或后端内部服务
- 不修改 `backend/` 代码；如发现契约缺口，写入 handoff
- 不把 Tailwind 原型代码作为生产代码直接拷贝
- 不在 view 组件里直接写 axios

## 4. 真值来源与实现基线

出现冲突时，按以下优先级执行：

1. `docs/implementation-plan.md` §4.7
2. `docs/frontend-handoff.md`
3. `方案设计_v0.1.md` §13、§15
4. `docs/architecture.md` §7
5. `collaboration/features/phase5-main.md`
6. `collaboration/architecture/frontend.md`
7. 本文档

本阶段额外锁定以下实现约束：

- 正式前端工程使用 Vue 3 + Vite + Element Plus + Pinia + Vue Router + Axios + ECharts
- 当前仓库无正式前端工程，本阶段默认使用 JavaScript 实现；如团队决定使用 TypeScript，必须一次性补齐类型规范，不允许半 JS 半 TS 混乱落地
- 所有 API 调用必须通过 `src/api/*`
- 所有 admin API 默认返回统一格式 `code/message/data`
- 所有页面路由必须与方案 §13.4 一致
- 审核工作台必须优先保证生产效率，不能退化为普通 CRUD 列表
- 审核工作台中 `target_type` 固定为 `job | resume`
- 审核详情、通过、驳回、编辑、Undo 均必须携带并处理 `version`
- 审核软锁 TTL 为 300 秒，审核详情页必须每 4 分钟通过再次调用 `/lock` 续约，离开详情或卸载页面时释放锁
- 批量审核单次最多 20 条，按前端串行单条接口实现，不具备事务性回滚；遇到失败立即中断并展示已成功 / 失败明细
- CSV 导出由后端返回 Blob，前端只负责触发下载和展示状态
- CSV 导出单次上限 10000 行，超出时按 `40101` 展示可读错误
- Excel 导入为全量事务：任一行失败则整体失败，成功数必须按 0 展示，并展示失败行明细
- 任何 Phase 5 API 不满足前端需求时，走 `handoffs/frontend-to-backend.md`

## 5. 路由清单

| 优先级 | 页面 | 路由 | 说明 |
|---|---|---|---|
| P0 | 登录 | `/admin/login` | 登录、首次改密 |
| P0 | Dashboard | `/admin/dashboard` | 首页指标与待办 |
| P0 | 审核工作台 | `/admin/audit` | 高频核心生产力页面 |
| P1 | 厂家管理 | `/admin/accounts/factories` | 预注册、导入、封禁 |
| P1 | 岗位管理 | `/admin/jobs` | 岗位查询、编辑、下架、延期 |
| P1 | 简历管理 | `/admin/resumes` | 简历查询、编辑、下架、延期 |
| P2 | 中介管理 | `/admin/accounts/brokers` | 预注册、导入、双向能力 |
| P2 | 工人列表 | `/admin/accounts/workers` | 只读查询 |
| P2 | 黑名单 | `/admin/accounts/blacklist` | 解封 |
| P2 | 系统配置 | `/admin/config` | 配置查看与修改 |
| P3 | 城市字典 | `/admin/dicts/cities` | 城市别名维护 |
| P3 | 工种字典 | `/admin/dicts/job-categories` | 工种 CRUD |
| P3 | 敏感词字典 | `/admin/dicts/sensitive-words` | 敏感词维护 |
| P3 | 数据看板 | `/admin/reports` | 趋势、TOP、漏斗 |
| P3 | 对话日志 | `/admin/logs/conversations` | 客诉溯源 |

## 6. P0 原型缺陷关闭要求

| # | 缺陷 | Phase 6 关闭标准 |
|---|---|---|
| 1 | 审核工作台缺 LLM 审核建议块 | 审核详情中有 C 块，展示风险等级、触发规则、审核建议、相似内容提示、提交者 7 天历史 |
| 2 | 驳回无理由面板 | 驳回必须弹出理由面板，未填理由不可提交 |
| 3 | 登录逻辑倒置 | `admin/admin123` 可登录，错误账号密码失败 |
| 4 | 图片大图预览缺失 | 所有图片缩略图可打开预览 Modal |
| 5 | 提交者历史 Drawer 缺失 | 卡片展示摘要，点击可打开 Drawer |
| 6 | 列表模式未实现 | 审核工作台可在单卡和列表模式间切换 |
| 7 | 风险等级整卡边框色未做 | low/mid/high 显示不同边框和警告条 |
| 8 | 侧边菜单缺 5 项 | 全部 15 个页面入口可见 |
| 9 | 顶栏缺通知铃铛 + 头像菜单 | 顶栏补齐，头像菜单可改密/退出 |
| 10 | Dashboard 错别字 | 改为“岗+简”或明确字段文案 |
| 11 | Dashboard 缺命中率 + 时间范围 | 补命中率指标与时间范围控件 |
| 12 | Dashboard 卡片无跳转 | 可跳转的卡片绑定跳转，不可跳转的卡片展示禁用态 |
| 13 | 快捷键 Space/X/S 不合理 | 改为 `P/R/S/E/U/?` |
| 14 | 缺快捷键帮助 Modal | `?` 可打开帮助 Modal |
| 15 | 缺边界状态 | 空队列、断网、LLM 故障、并发冲突、版本冲突均有明确 UI |

## 7. 验收标准

- [ ] `frontend/` 已形成正式 Vue 3 工程，`npm install`、`npm run dev`、`npm run build` 可运行
- [ ] 本地访问 `http://localhost:5173/admin/login` 正常
- [ ] 登录、刷新恢复登录态、退出登录、改密码流程可用
- [ ] 路由守卫生效：未登录不可进入任意后台页面
- [ ] 15 个页面路由全部存在，侧边菜单入口完整
- [ ] 所有 API 调用均来自 `src/api/*`，view 内无直接 axios
- [ ] 统一响应处理生效，鉴权错误会清 token 并跳登录
- [ ] Dashboard 指标、趋势、待办入口、60 秒刷新可用
- [ ] 审核工作台可完成：lock → 4 分钟续锁 → 查看详情 → 编辑 → 通过 / 驳回 → Undo → unlock
- [ ] 审核工作台支持单卡模式、列表模式、快捷键、图片预览、历史 Drawer、风险可视化
- [ ] 厂家 / 中介支持预注册与 Excel 导入；工人列表只读；黑名单可解封
- [ ] 岗位支持筛选、排序、分页、详情、编辑、下架、延期、取消下架、导出；简历支持筛选、排序、分页、详情、编辑、下架、延期、导出，不提供取消下架
- [ ] 城市 / 工种 / 敏感词字典页面功能完整
- [ ] 系统配置可分组查看、单项保存、危险项二次确认、JSON 校验
- [ ] 数据看板趋势、TOP、漏斗、导出可用
- [ ] 对话日志必须按 userid + 时间范围查询，支持 criteria_snapshot 展开和导出
- [ ] 所有列表页具备 loading / 空状态 / 错误状态 / 分页 / 筛选 / 排序 / 导出
- [ ] 所有危险操作有二次确认，所有编辑有脏数据离开提醒
- [ ] §15.3 的 15 个 P0 原型缺陷全部关闭
- [ ] 前后端联调通过，Phase 5 API 缺口均已记录 handoff 或关闭
- [ ] `frontend/dist/` 构建产物可被 nginx 托管

## 8. 进入条件

| 条件 | 状态 | 说明 | 如未满足的应对 |
|---|---|---|---|
| Phase 5 API 契约冻结 | 待确认 | Phase 6 强依赖 `/admin/*` 接口 | 未满足时，前端可先基于 `phase5-main.md` mock 开发 |
| 后端 Swagger UI 可访问 | 待确认 | 用于接口自测和字段确认 | 不可访问时，先按文档 mock，记录 handoff |
| 默认管理员账号可用 | 待确认 | 登录联调依赖 | 缺失时由后端补 seed 或临时账号 |
| 前端开发环境 Node 可用 | 待确认 | 需要安装依赖和启动 Vite | 缺失时先补环境，不开始页面开发 |

## 9. 风险与备注

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Phase 5 API 字段与前端页面需求不一致 | 页面无法联调 | 所有缺口写入 `handoffs/frontend-to-backend.md`，主文档回填最终结论 |
| 审核工作台复杂度高 | 影响运营效率与进度 | 优先完成 P0 审核流，再做 P1-P3 页面 |
| 当前无正式前端工程 | 初期搭建工作量不可忽略 | 先搭工程、路由、API 层、Layout，再铺页面 |
| CSV 导出 / Excel 导入浏览器兼容问题 | 运营无法批量操作 | 统一封装 `useDownload` 和 `UploadImportDialog` |
| 大列表性能问题 | 工人列表 5000+ 卡顿 | 工人列表使用分页，必要时使用虚拟滚动 |
| 深色模式硬编码颜色冲突 | UI 不一致 | 使用 Element Plus CSS 变量，不在组件内硬写主题色 |

## 10. 文件变更清单

| 操作 | 文件 / 目录 | 说明 |
|---|---|---|
| 新建 | `frontend/package.json` | 依赖与脚本 |
| 新建 | `frontend/vite.config.js` | Vite 与代理配置 |
| 新建 | `frontend/index.html` | SPA 入口 |
| 新建 | `frontend/src/main.js` | Vue 入口 |
| 新建 | `frontend/src/App.vue` | 根组件 |
| 新建 | `frontend/src/router/index.js` | 15 个页面路由与守卫 |
| 新建 | `frontend/src/stores/auth.js` | 登录态 |
| 新建 | `frontend/src/stores/app.js` | 全局 UI 状态 |
| 新建 | `frontend/src/api/request.js` | Axios 统一封装 |
| 新建 | `frontend/src/api/*.js` | 各业务 API |
| 新建 | `frontend/src/components/layout/*` | Layout 组件 |
| 新建 | `frontend/src/components/PageTable.vue` | 通用表格 |
| 新建 | `frontend/src/components/DetailDrawer.vue` | 详情抽屉 |
| 新建 | `frontend/src/components/ImagePreview.vue` | 图片预览 |
| 新建 | `frontend/src/components/*` | 通用状态、确认、导入、JSON 编辑等组件 |
| 新建 | `frontend/src/composables/*` | 表格、键盘、下载、脏数据保护 |
| 新建 | `frontend/src/utils/*` | 常量、格式化、校验 |
| 新建 | `frontend/src/views/login/*` | 登录页 |
| 新建 | `frontend/src/views/dashboard/*` | Dashboard |
| 新建 | `frontend/src/views/audit/*` | 审核工作台 |
| 新建 | `frontend/src/views/accounts/*` | 账号管理 |
| 新建 | `frontend/src/views/jobs/*` | 岗位管理 |
| 新建 | `frontend/src/views/resumes/*` | 简历管理 |
| 新建 | `frontend/src/views/dicts/*` | 字典管理 |
| 新建 | `frontend/src/views/config/*` | 系统配置 |
| 新建 | `frontend/src/views/reports/*` | 数据看板 |
| 新建 | `frontend/src/views/logs/*` | 对话日志 |
