# Phase 6 开发 Checklist

> 基于：`collaboration/features/phase6-main.md`
> 配套实施文档：`collaboration/features/phase6-dev-implementation.md`
> 面向角色：前端开发
> 状态：`draft`
> 创建日期：2026-04-17

## A. 开发前确认

- [ ] 已阅读 `collaboration/features/phase6-main.md`
- [ ] 已阅读 `collaboration/features/phase6-dev-implementation.md`
- [ ] 已阅读 `collaboration/architecture/frontend.md`
- [ ] 已确认本阶段只修改 `frontend/`
- [ ] 已确认 API 契约以 Phase 5 文档和 Swagger UI 为准
- [ ] 已确认发现后端缺口时写 `handoffs/frontend-to-backend.md`
- [ ] 已确认原型只做参考，不直接复用 Tailwind 生产代码
- [ ] 已确认本阶段默认 JavaScript 实现
- [ ] Node / npm 可用
- [ ] 后端开发环境或 mock 服务可用

## B. 工程搭建

- [ ] `frontend/package.json` 已创建
- [ ] `frontend/vite.config.js` 已创建
- [ ] `frontend/index.html` 已创建
- [ ] `frontend/src/main.js` 已创建
- [ ] `frontend/src/App.vue` 已创建
- [ ] Vue 3 已接入
- [ ] Element Plus 已接入
- [ ] Element Plus Icons 已接入
- [ ] Pinia 已接入
- [ ] Vue Router 已接入
- [ ] Axios 已接入
- [ ] ECharts / vue-echarts 已接入
- [ ] `npm install` 成功
- [ ] `npm run dev` 可启动
- [ ] `npm run build` 可成功
- [ ] Vite 代理 `/admin` 指向 `http://localhost:8000`
- [ ] Vite 代理 `/api/events` 指向 `http://localhost:8000`
- [ ] 代码中无 Phase 6 后台页面直接调用 `/api/events/*`

## C. 目录结构

- [ ] `src/router/index.js`
- [ ] `src/stores/auth.js`
- [ ] `src/stores/app.js`
- [ ] `src/api/request.js`
- [ ] `src/api/auth.js`
- [ ] `src/api/audit.js`
- [ ] `src/api/accounts.js`
- [ ] `src/api/jobs.js`
- [ ] `src/api/resumes.js`
- [ ] `src/api/dicts.js`
- [ ] `src/api/config.js`
- [ ] `src/api/reports.js`
- [ ] `src/api/logs.js`
- [ ] `src/components/layout/`
- [ ] `src/components/PageTable.vue`
- [ ] `src/components/DetailDrawer.vue`
- [ ] `src/components/ImagePreview.vue`
- [ ] `src/components/ConfirmAction.vue`
- [ ] `src/components/UploadImportDialog.vue`
- [ ] `src/components/JsonEditor.vue`
- [ ] `src/composables/usePageTable.js`
- [ ] `src/composables/useDirtyGuard.js`
- [ ] `src/composables/useKeyboard.js`
- [ ] `src/composables/useDownload.js`
- [ ] `src/utils/constants.js`
- [ ] `src/utils/format.js`
- [ ] `src/utils/validators.js`

## D. 路由与鉴权

- [ ] `/admin/login` 路由存在
- [ ] `/admin/dashboard` 路由存在
- [ ] `/admin/audit` 路由存在
- [ ] `/admin/accounts/factories` 路由存在
- [ ] `/admin/accounts/brokers` 路由存在
- [ ] `/admin/accounts/workers` 路由存在
- [ ] `/admin/accounts/blacklist` 路由存在
- [ ] `/admin/jobs` 路由存在
- [ ] `/admin/resumes` 路由存在
- [ ] `/admin/dicts/cities` 路由存在
- [ ] `/admin/dicts/job-categories` 路由存在
- [ ] `/admin/dicts/sensitive-words` 路由存在
- [ ] `/admin/config` 路由存在
- [ ] `/admin/reports` 路由存在
- [ ] `/admin/logs/conversations` 路由存在
- [ ] 未登录访问任意后台页跳 `/admin/login`
- [ ] 已登录访问 `/admin/login` 跳 `/admin/dashboard`
- [ ] 刷新页面后可恢复登录态
- [ ] token 过期后清理登录态并跳登录
- [ ] `password_changed=false` 时强制改密码

## E. API 封装

- [ ] `request.js` 自动注入 Bearer token
- [ ] `request.js` 对 `code=0` 返回 `data`
- [ ] `request.js` 对 `40001/40002/40003` 清 token 并跳登录
- [ ] 登录页收到 `40001` 停留在表单，不触发全局重定向
- [ ] `request.js` 对 `40901/40902/40903` 不做全局 Toast，只 reject 给页面处理
- [ ] `request.js` 对普通错误统一 Toast
- [ ] 表单错误 `40100-40199` 可被页面捕获
- [ ] Blob 下载独立封装
- [ ] Blob 导出收到 `40101` 时提示“数据量过大，请缩小筛选范围”
- [ ] Excel 上传独立封装
- [ ] view 内无直接 axios 调用
- [ ] 所有 API 路径与 Phase 5 文档一致

## F. Layout

- [ ] 左侧菜单包含全部 15 页入口
- [ ] 账号管理 / 字典管理折叠分组命中子路由时自动展开
- [ ] 审核工作台菜单展示待审 badge
- [ ] 侧边栏支持展开 / 收起
- [ ] 顶栏展示页面标题 / 面包屑
- [ ] 顶栏有全局 userid 搜索入口
- [ ] 顶栏有通知铃铛
- [ ] 顶栏有管理员头像菜单
- [ ] 头像菜单支持改密码
- [ ] 头像菜单支持退出登录
- [ ] 主内容区局部滚动
- [ ] 深色模式使用 Element Plus CSS 变量

## G. 登录页

- [ ] 用户名 / 密码输入框
- [ ] Enter 提交
- [ ] 登录按钮 loading
- [ ] 登录按钮 loading 期间禁用，避免重复提交
- [ ] `admin/admin123` 可登录
- [ ] 错误账号密码 Toast
- [ ] 错误账号密码停留在登录页
- [ ] 登录成功跳 Dashboard
- [ ] 首次登录强制改密弹窗
- [ ] 改密码校验旧密码、新密码长度、新旧不能相同
- [ ] 退出登录清 token 和管理员信息

## H. Dashboard

- [ ] 调用 `/admin/reports/dashboard`
- [ ] 调用 `/admin/audit/pending-count`
- [ ] 展示 DAU 指标
- [ ] 展示上传数指标
- [ ] 展示检索次数指标
- [ ] 展示命中率指标
- [ ] 展示空召回率指标
- [ ] 展示待审积压指标
- [ ] 展示昨日对比
- [ ] 展示 7 日趋势图
- [ ] 7d 趋势使用 dashboard 响应内 `trend_7d`
- [ ] 30d 趋势调用 `/admin/reports/trends?range=30d`
- [ ] Dashboard 不向 `/admin/reports/dashboard` 传 `range`
- [ ] custom 范围入口跳转 `/admin/reports`
- [ ] 有待办入口面板
- [ ] 指标卡片可跳转或显示禁用态
- [ ] 60 秒静默刷新
- [ ] 修复“岗+历”错别字

## I. 审核工作台

### I.1 队列与详情

- [ ] 待审 / 已通过 / 已驳回 tab
- [ ] 调用 `/admin/audit/queue`
- [ ] 调用 `/admin/audit/pending-count`
- [ ] 队列项展示 `locked_by`
- [ ] 打开条目前调用 lock
- [ ] lock 成功后拉详情
- [ ] 进入详情后每 4 分钟再次调用 lock 续约 TTL
- [ ] 续约失败时退出编辑态并提示刷新队列
- [ ] 切换条目前 unlock
- [ ] 离开页面时 unlock
- [ ] `40901` 软锁冲突有明确提示
- [ ] `40902` 版本冲突有明确提示
- [ ] `40903` Undo 超时有明确提示

### I.2 单卡模式

- [ ] A 块原始内容
- [ ] B 块 LLM 抽取字段
- [ ] C 块审核建议
- [ ] 提交者信息条
- [ ] 提交者 7 天历史摘要
- [ ] 提交者历史 Drawer
- [ ] 图片缩略图
- [ ] 图片大图预览
- [ ] 后端返回 `field_confidence` 时展示 LLM 置信度绿 / 黄 / 红点
- [ ] 后端返回 `field_confidence` 时低置信字段淡红高亮
- [ ] 后端未返回 `field_confidence` 时隐藏置信度 UI，不阻塞审核
- [ ] 风险等级整卡边框色
- [ ] 顶部风险警告条

### I.3 操作

- [ ] 通过带 `version`
- [ ] 驳回必须打开理由面板
- [ ] 驳回未填 reason 不可提交
- [ ] 驳回支持 `notify`
- [ ] 驳回支持 `block_user`
- [ ] 编辑字段带 `version`
- [ ] 编辑保存成功后刷新详情
- [ ] Undo 30 秒倒计时
- [ ] Undo 成功后刷新状态
- [ ] 稍后切换下一条
- [ ] 每 50 条提示休息

### I.4 快捷键

- [ ] `P` 通过
- [ ] `R` 驳回
- [ ] `S` 稍后
- [ ] `E` 编辑
- [ ] `U` Undo
- [ ] `?` 帮助
- [ ] 输入框聚焦时快捷键不触发
- [ ] 不使用 Space 作为通过快捷键
- [ ] 快捷键帮助 Modal 可打开

### I.5 列表与批量

- [ ] 单卡 / 列表模式可切换
- [ ] 列表模式可多选
- [ ] 批量上限 20 条
- [ ] 批量操作有二次确认
- [ ] 批量审核按单条接口串行执行，不调用不存在的 batch API
- [ ] 批量中途失败立即中断，不回滚已成功条目
- [ ] 批量结果可展示成功 / 失败明细

### I.6 边界状态

- [ ] 空队列庆祝态
- [ ] 网络断开顶部红条
- [ ] LLM 故障提示人工录入
- [ ] 超长内容局部滚动
- [ ] 编辑未保存切换条目有提醒或自动保存

## J. 账号管理

- [ ] 厂家列表分页 / 筛选 / 排序
- [ ] 厂家详情抽屉
- [ ] 厂家预注册
- [ ] 厂家 Excel 导入
- [ ] 厂家 Excel 导入任一行失败时展示 `success_count=0`
- [ ] 中介列表分页 / 筛选 / 排序
- [ ] 中介详情展示双向能力
- [ ] 中介预注册
- [ ] 中介 Excel 导入
- [ ] 中介 Excel 导入任一行失败时展示 `success_count=0`
- [ ] 工人列表只读
- [ ] 工人无新增 / 编辑 / 导入入口
- [ ] 黑名单列表
- [ ] 封禁必须填写理由
- [ ] 解封必须填写理由并二次确认
- [ ] 导入失败明细展示 row / error
- [ ] 导入失败不展示“部分成功”文案

## K. 岗位 / 简历管理

- [ ] 岗位列表筛选
- [ ] 岗位筛选包含 `pay_type`
- [ ] 岗位列表排序
- [ ] 岗位详情抽屉
- [ ] 岗位编辑带 version
- [ ] 岗位下架 reason=manual_delist
- [ ] 岗位招满 reason=filled
- [ ] 岗位延期 15 / 30 天
- [ ] 岗位取消下架
- [ ] 岗位 CSV 导出
- [ ] 岗位 CSV 导出超 10000 行时处理 `40101`
- [ ] 岗位 TTL 剩余绿 / 黄 / 红
- [ ] 简历列表筛选
- [ ] 简历详情抽屉
- [ ] 简历编辑带 version
- [ ] 简历下架 / 延期 / 导出
- [ ] 简历不展示取消下架 / restore 按钮
- [ ] 简历 CSV 导出超 10000 行时处理 `40101`
- [ ] 附件图片可预览

## L. 字典管理

- [ ] 城市按省份折叠
- [ ] 城市 aliases tag 输入
- [ ] 城市只允许编辑 aliases
- [ ] 工种列表
- [ ] 工种新增
- [ ] 工种编辑
- [ ] 工种删除确认
- [ ] 工种 sort_order 编辑
- [ ] 敏感词列表筛选
- [ ] 敏感词新增
- [ ] 敏感词删除确认
- [ ] 敏感词批量粘贴导入
- [ ] 敏感词批量结果展示 added / duplicated

## M. 系统配置

- [ ] 配置按命名空间分组
- [ ] 每项独立保存
- [ ] int 用数字输入
- [ ] bool 用 Switch
- [ ] json 用 JsonEditor
- [ ] string 用 Input
- [ ] JSON 保存前前端校验
- [ ] 危险项以接口返回 `danger` 字段判断
- [ ] 接口缺 `danger` 时才使用硬编码危险 key 兜底
- [ ] 危险项二次确认
- [ ] 保存失败保留用户输入
- [ ] 保存成功 Toast

## N. 数据看板

- [ ] 时间范围 7d
- [ ] 时间范围 30d
- [ ] custom 时间范围
- [ ] custom from/to 跨度超过 90 天时阻止或展示 `40101`
- [ ] trends 图表
- [ ] top 榜单
- [ ] funnel 漏斗
- [ ] CSV 导出
- [ ] CSV 导出超 10000 行时处理 `40101`
- [ ] 图表 loading 状态
- [ ] 图表空状态
- [ ] 图表错误状态

## O. 对话日志

- [ ] userid 必填
- [ ] start/end 必填
- [ ] 前端限制最大 30 天
- [ ] 支持 direction 筛选
- [ ] 支持 intent 筛选
- [ ] 聊天气泡时间线
- [ ] criteria_snapshot 可展开
- [ ] CSV 导出
- [ ] CSV 导出超 10000 行时处理 `40101`

## P. 通用交互

- [ ] 所有列表有 loading
- [ ] 所有列表有空状态
- [ ] 所有列表有错误状态
- [ ] 所有列表支持分页
- [ ] 所有列表支持筛选
- [ ] 所有列表支持排序
- [ ] 所有列表支持导出
- [ ] 所有危险操作有二次确认
- [ ] 所有编辑有脏数据保护
- [ ] 所有保存按钮有 loading
- [ ] 所有成功操作有 Toast
- [ ] 所有失败操作有可读错误

## Q. P0 原型缺陷关闭

- [ ] LLM 审核建议 C 块
- [ ] 驳回理由面板
- [ ] 登录逻辑修正
- [ ] 图片大图预览
- [ ] 提交者历史 Drawer
- [ ] 列表模式
- [ ] 风险等级整卡边框色
- [ ] 侧边菜单补齐
- [ ] 顶栏通知铃铛 + 头像菜单
- [ ] Dashboard 文案修正
- [ ] Dashboard 命中率 + 时间范围
- [ ] Dashboard 卡片跳转
- [ ] 快捷键改为 P/R/S/E/U/?
- [ ] 快捷键帮助 Modal
- [ ] 边界状态补齐

## R. 构建与交付

- [ ] `npm run lint` 通过或已有明确未启用说明
- [ ] `npm run build` 通过
- [ ] `frontend/dist/` 生成
- [ ] 浏览器直接打开开发环境无控制台红色错误
- [ ] 主要页面移动到生产构建预览后无白屏
- [ ] Phase 5 API 缺口已写 handoff
- [ ] 已关闭的 handoff 已回填文档
