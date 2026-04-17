# 运营后台前端联调手册

> 面向角色：Phase 6 前端开发
> 前端技术栈：Vue 3 + Vite + Element Plus + Pinia + Vue Router + axios
> 后端版本：Phase 5 交付（60 个 `/admin/*` + 1 个 `/api/events/*`）
> 真值来源：`docs/architecture.md §7`、`方案设计_v0.1.md §13 / §17`、`collaboration/features/phase5-main.md`
> 配套：`/docs`（Swagger UI，开发环境可直接调通）、`/redoc`（只读文档视图）

---

## 目录

1. [联调准备](#1-联调准备)
2. [鉴权与会话](#2-鉴权与会话)
3. [统一响应协议与错误码](#3-统一响应协议与错误码)
4. [通用约定](#4-通用约定)
5. [典型调用链](#5-典型调用链)
6. [模块路由清单](#6-模块路由清单)
7. [前端侧实现建议](#7-前端侧实现建议)
8. [不在本期范围](#8-不在本期范围)
9. [对接问题反馈](#9-对接问题反馈)

---

## 1. 联调准备

### 1.1 后端本地启动

```bash
# 假设已经 docker compose up -d 起了 MySQL + Redis
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后：

- **Swagger UI**：http://localhost:8000/docs
- **ReDoc**：http://localhost:8000/redoc
- **健康检查**：`GET http://localhost:8000/health`

### 1.2 Base URL

| 环境 | Base URL |
|---|---|
| 开发（本地） | `http://localhost:8000` |
| 预发 | 待运维下发 |
| 生产 | 待运维下发 |

前端将 base URL 放到 `.env` / `.env.production`：

```dotenv
VITE_API_BASE=http://localhost:8000
```

### 1.3 CORS

后端通过 `CORS_ORIGINS` 环境变量控制允许的来源。本地开发默认放开 `*`；
上线前运维会在 `.env` 写入前端域名。前端不需要额外配置。

### 1.4 默认管理员账号

| 字段 | 值 |
|---|---|
| username | `admin` |
| password | `admin123` |

**⚠️ 生产环境部署前必须改密，`password_changed=0` 时前端应强制跳改密页（§2.4）。**

### 1.5 `.env` 关键变量

后端 `.env` 中前端联调会间接依赖的变量：

| key | 说明 | 前端影响 |
|---|---|---|
| `ADMIN_JWT_SECRET` | JWT 签名密钥 | 无（服务端） |
| `ADMIN_JWT_EXPIRES_HOURS` | JWT 过期小时数，默认 24 | 前端需按 `expires_at` 处理过期 |
| `EVENT_API_KEY` | 事件回传 API Key | 小程序后端调用 `/api/events/*` 时使用，后台前端一般不用 |

---

## 2. 鉴权与会话

### 2.1 登录流程

```
POST /admin/login
Content-Type: application/json

{ "username": "admin", "password": "admin123" }
```

**成功响应：**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "access_token": "eyJhbGciOi...",
    "token_type": "bearer",
    "expires_at": "2026-04-18T03:00:00+00:00",
    "password_changed": false
  }
}
```

**失败响应：**

- `40001` 用户名或密码错误（连续 3 次失败后服务端会额外 sleep 1s，防暴力）
- `40301` 账号已禁用

### 2.2 后续请求鉴权

所有 `/admin/*` 请求需在 Header 带：

```
Authorization: Bearer <access_token>
```

前端建议用 axios 拦截器统一加（见 §7.2）。

### 2.3 Token 过期处理

- JWT 载荷：`{sub, username, exp}`，过期由服务端 `exp` 校验
- 过期时返回 `code=40002`，前端应跳回登录页
- 一期不做 refresh token，用户需重新输密码

### 2.4 首次登录强制改密

- `POST /admin/login` 响应中若 `password_changed=false`，前端需强制跳改密页
- `PUT /admin/me/password` 带 `{old_password, new_password}`
- 新密码长度 < 8 返回 `40101`
- 新密码与旧密码相同返回 `40101`
- 旧密码错误返回 `40001`
- **成功后旧 token 仍有效**（服务端不做强制下线，前端也不需要重登）

### 2.5 获取当前用户

```
GET /admin/me
Authorization: Bearer <token>

响应：{ code, message, data: AdminUserRead }
```

用于 Layout 顶部栏显示用户名、判断是否首次登录等。

---

## 3. 统一响应协议与错误码

### 3.1 成功响应

```json
{ "code": 0, "message": "ok", "data": <payload> }
```

### 3.2 分页响应

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [...],
    "total": 127,
    "page": 1,
    "size": 20,
    "pages": 7
  }
}
```

### 3.3 错误响应

```json
{ "code": 40001, "message": "用户名或密码错误", "data": null }
```

**关键约定**：所有业务错误都以 **HTTP 200** 返回，通过 `code` 字段区分成败。
前端 axios 拦截器应基于 `data.code !== 0` 而不是 HTTP 状态码来判定失败。

例外：

- `HTTP 403 + Content-Type: text/plain body="success"` 只出现在企微 Webhook（`/webhook/*`），不影响后台
- `HTTP 5xx` 才是真正的服务端故障（网络中断 / 后端 crash），前端应走兜底错误页

### 3.4 错误码速查表

| code | 含义 | 前端建议动作 |
|---|---|---|
| `0` | 成功 | —— |
| `40001` | 用户名/密码错误，或 API Key 无效 | 登录页：表单校验错误提示；其它接口：跳登录页 |
| `40002` | Token 过期 | 清除本地 token，跳登录页 |
| `40003` | Token 无效（缺失 / 签名错误 / 用户被禁） | 同上 |
| `40101` | 参数错误（含 Pydantic 校验失败、时间范围越界、值不在白名单等） | Toast / 表单字段错误提示；`data.fields` 含详情 |
| `40301` | 权限不足（账号被禁用等） | Toast + 跳登录页 |
| `40401` | 资源不存在 | Toast；详情页跳回列表 |
| `40900` | 资源冲突（通用） | Toast |
| `40901` | **审核软锁冲突** | 弹窗提示 `"此条目正在被 {locked_by} 处理"`；给操作员"稍后重试"按钮；data 含 `{locked_by}` |
| `40902` | **乐观锁冲突**（版本不一致） | 弹窗提示 `"此条目已被其他人修改，请刷新"`；data 含 `{current_version}`；前端应重新拉详情 |
| `40903` | 撤销窗口已过（Undo 30s 超时） | Toast `"撤销窗口已过期"`；隐藏 Undo 按钮 |
| `40904` | 业务冲突（已封禁 / 已下架 / 字典被引用等） | Toast |
| `50001` | 内部错误（服务端兜底异常） | Toast "服务异常，请稍后重试"；可选上报 |
| `50101` | LLM 异常 | 仅在相关业务接口出现 |

### 3.5 前端 axios 拦截器（示例）

```typescript
// src/api/http.ts
import axios, { AxiosError } from "axios";
import { ElMessage } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import router from "@/router";

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE,
  timeout: 15_000,
});

http.interceptors.request.use((config) => {
  const auth = useAuthStore();
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`;
  }
  return config;
});

http.interceptors.response.use(
  (resp) => {
    const { code, message, data } = resp.data ?? {};
    if (code === 0) {
      return data; // 直接返回 data，业务代码不关心外层信封
    }
    // 业务错误：按 code 分发
    handleBusinessError(code, message, data);
    return Promise.reject({ code, message, data });
  },
  (err: AxiosError) => {
    // HTTP 5xx / 网络错误
    ElMessage.error("网络异常，请稍后重试");
    return Promise.reject(err);
  },
);

function handleBusinessError(code: number, message: string, data: any) {
  switch (code) {
    case 40001:
    case 40002:
    case 40003:
      useAuthStore().logout();
      router.replace({ name: "login" });
      ElMessage.warning(message || "请重新登录");
      return;
    case 40901:
      // 软锁冲突：由调用方处理 UI（弹窗 + locked_by）
      return;
    case 40902:
      // 乐观锁冲突：由调用方处理（提示刷新 + current_version）
      return;
    default:
      ElMessage.error(message || `操作失败 (${code})`);
  }
}

export default http;
```

---

## 4. 通用约定

### 4.1 分页

所有列表接口支持：

- `?page=<int>`：从 1 开始，默认 1
- `?size=<int>`：默认 20，最大 100（对话日志最大 200）
- 响应含 `items / total / page / size / pages`

### 4.2 排序

岗位 / 简历 / 部分列表支持：

- `?sort=field:desc,field2:asc`
- 字段必须在白名单内，否则 `40101`
- 默认排序：`created_at:desc`

### 4.3 乐观锁 version

编辑、下架、延期、取消下架、pass/reject/edit 等**所有写操作**都必须回带 `version`（审核详情 / 列表项 / 详情页都会返回 `version`）：

```json
PUT /admin/jobs/123
{ "version": 3, "fields": { "salary_floor_monthly": 5000 } }
```

- 版本不一致 → `40902`，响应 `data: {current_version: 5}`
- 前端处理：提示刷新，重新拉详情后允许重试

**注意**：`restore` 接口虽然没有其他业务参数，也必须传 `{"version": N}` 请求体。

### 4.4 审核软锁（`/admin/audit/*/{id}/lock`）

- 进入审核详情页 → 自动调 `POST /admin/audit/{type}/{id}/lock`
- 成功返回 `{code:0}`；TTL 300 秒，前端建议每 4 分钟续锁（再次调 `/lock` 会刷新 TTL）
- 失败 `40901`：响应 `data: {locked_by: "<其他管理员>"}`
- 离开页面（路由切换 / 关闭 tab） → 调 `POST /admin/audit/{type}/{id}/unlock`
- unlock 仅持有者有效，非持有者调用不影响状态

### 4.5 Undo 30 秒窗口

- pass / reject / edit 执行后，服务端保留 30 秒 Undo 快照
- 前端在详情页操作按钮旁显示"撤销"按钮，30 秒后变灰
- `POST /admin/audit/{type}/{id}/undo` 无请求体
- 过窗返回 `40903`

### 4.6 CSV 导出

- 所有 `export` 类接口返回 `Content-Type: text/csv; charset=utf-8`，含 UTF-8 BOM（Excel 打开中文无乱码）
- `Content-Disposition: attachment; filename="jobs_202604180930.csv"`
- 前端用 `<a href target="_blank">` 或 `window.open` 直接触发下载，或用 axios 收 blob

```typescript
const blob = await http.get("/admin/jobs/export", {
  params: filters,
  responseType: "blob",
});
const url = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = `jobs_${format(new Date(), "yyyyMMddHHmm")}.csv`;
a.click();
URL.revokeObjectURL(url);
```

- 单次导出上限 10000 行，超出返回 `40101`，提示前端缩小筛选范围

### 4.7 时间范围与格式

- 所有 `datetime` 字段使用 ISO 8601 带时区（如 `2026-04-17T09:00:00+08:00`）
- `date` 字段使用 `YYYY-MM-DD`
- 对话日志查询 `start` / `end` 跨度 > 30 天 → `40101`
- 看板 trends `range=custom` 的 `from` / `to` 跨度 > 90 天 → `40101`

### 4.8 字段白名单

岗位 / 简历列表筛选字段严格白名单：

- 岗位：`city / district / job_category / pay_type / audit_status / delist_reason / owner_userid / salary_min / salary_max / created_from / created_to / expires_from / expires_to`
- 简历：`gender / age_min / age_max / expected_cities / expected_job_categories / audit_status / owner_userid / created_from / created_to`

传白名单外字段不会报错，但会被忽略，不要依赖这个行为。

### 4.9 危险配置项

系统配置更新接口（`PUT /admin/config/{key}`）返回：

```json
{ "changed": true, "danger": true, "notice": "该配置变更将立即影响业务，请确认" }
```

当 `danger=true` 时（`filter.enable_gender / filter.enable_age / filter.enable_ethnicity / llm.provider`），前端应在提交前弹二次确认。

---

## 5. 典型调用链

### 5.1 登录 + 首次改密

```
1. POST /admin/login { username, password }
   → { access_token, expires_at, password_changed: false }
2. 存 token 到 Pinia / localStorage
3. 如果 password_changed=false → 跳改密页
   PUT /admin/me/password { old_password, new_password }
4. 跳 Dashboard
   GET /admin/reports/dashboard
```

### 5.2 审核工作台完整闭环

```
1. 进入队列页
   GET /admin/audit/queue?status=pending&target_type=job&page=1&size=20
   GET /admin/audit/pending-count  (side bar badge)

2. 点击一条进入详情
   POST /admin/audit/job/123/lock        // 抢软锁
     - 40901 → 弹窗"{locked_by} 正在处理"，返回列表
   GET  /admin/audit/job/123              // 拉详情（含 version、risk_level、submitter_history）

3. 运营看内容，决定动作：

   3a. 通过
       POST /admin/audit/job/123/pass  { "version": 3 }
         - 40902 → 提示刷新，重新 GET 详情
         - 成功 → 显示"撤销"按钮（30 秒倒计时）
   3b. 驳回
       POST /admin/audit/job/123/reject
            { "version": 3, "reason": "薪资信息涉嫌虚假", "block_user": true }
   3c. 编辑修正后通过
       PUT /admin/audit/job/123/edit
           { "version": 3, "fields": { "salary_floor_monthly": 5500 } }
       → 然后 POST /pass { "version": 4 }（注意 edit 后 version+1）

4. 30 秒内撤销（可选）
   POST /admin/audit/job/123/undo

5. 离开详情页
   POST /admin/audit/job/123/unlock
```

### 5.3 厂家 Excel 批量导入

```
1. 下载模板（前端静态模板文件，列：role/display_name/company/contact_person/phone/can_search_jobs/can_search_workers/external_userid）

2. 用户上传 xlsx
   POST /admin/accounts/factories/import
   Content-Type: multipart/form-data
   field: file = <File>

3. 响应
   成功：{ success_count: 42, failed: [] }
   任意一行失败：{ success_count: 0, failed: [{row: 3, error: "phone 必填"}, ...] }
   ⚠️ 成功率不是 N/total：要么全成要么全失败（事务性全量回滚）

4. 前端 UI：
   - success_count > 0 → Message 成功 toast + 刷新列表
   - success_count == 0 → 弹窗展示 failed 表格，让用户修正 Excel 重传
```

### 5.4 岗位延期 / 下架 / 取消下架

```
列表 → 详情
GET /admin/jobs/123 → { version: 5, delist_reason: null, expires_at: "..." }

延期 15 天：
POST /admin/jobs/123/extend { "version": 5, "days": 15 }
→ { code: 0, data: { expires_at: "2026-05-02T..." } }

手动下架：
POST /admin/jobs/123/delist { "version": 6, "reason": "manual_delist" }

已下架 + 未过期 → 可取消下架：
POST /admin/jobs/123/restore { "version": 7 }
→ 过期的岗位返回 40904
```

### 5.5 系统配置变更 + 危险项二次确认

```
1. GET /admin/config → 返回按前缀分组的配置 { ttl: [...], rate_limit: [...], filter: [...] }
   每项含 { config_key, config_value, value_type, danger: bool }

2. 用户修改某项：
   2a. danger=false → 直接 PUT
   2b. danger=true → 前端先弹二次确认，确认后 PUT
       PUT /admin/config/filter.enable_gender
           { "config_value": "false" }
       → { code: 0, data: { changed: true, danger: true, notice: "..." } }

3. Phase 4 webhook 会在下次读取时直接命中 Redis 缓存失效，限流参数等立即生效
```

### 5.6 对话日志查询

```
GET /admin/logs/conversations
    ?userid=wx_xxx
    &start=2026-04-10T00:00:00+08:00
    &end=2026-04-17T00:00:00+08:00
    &direction=in         // 可选
    &intent=search_job    // 可选
    &page=1
    &size=50
```

- 必须带 `userid + start + end`
- `(end - start).days > 30` 返回 `40101`
- 导出 CSV：`/admin/logs/conversations/export` 同参数

### 5.7 数据看板

```
首屏：
GET /admin/reports/dashboard
  → { today: {...}, yesterday: {...}, trend_7d: [...] }
    注：audit_pending 只在 today 里（当前时刻值，历史日不返回此字段）

趋势：
GET /admin/reports/trends?range=30d
GET /admin/reports/trends?range=custom&from=2026-04-01&to=2026-04-17

TOP 榜：
GET /admin/reports/top?dim=city&limit=10
GET /admin/reports/top?dim=job_category&limit=10
GET /admin/reports/top?dim=role&limit=10

漏斗（固定 5 阶段 注册 → 首次发消息 → 首次有效检索 → 收到推荐 → 点详情）：
GET /admin/reports/funnel

导出（目前仅 daily metric）：
GET /admin/reports/export?metric=daily&from=2026-04-01&to=2026-04-17&format=csv
```

---

## 6. 模块路由清单

> 所有 `/admin/*` 接口均需 `Authorization: Bearer <token>`。Swagger UI 按 tag 分组列出完整参数；下表只列 method + path + 简述。

### 6.1 鉴权（tag: `admin-auth`，3）

| Method | Path | 说明 |
|---|---|---|
| POST | `/admin/login` | 登录颁发 JWT |
| GET | `/admin/me` | 当前管理员信息 |
| PUT | `/admin/me/password` | 修改密码 |

### 6.2 审核工作台（tag: `admin-audit`，9）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/audit/queue` | 待审队列（分页 + status/target_type 筛选） |
| GET | `/admin/audit/pending-count` | 待审数量（job/resume/total） |
| GET | `/admin/audit/{target_type}/{id}` | 审核详情（含 version / risk_level / submitter_history） |
| POST | `/admin/audit/{target_type}/{id}/lock` | 抢软锁（重入续 TTL） |
| POST | `/admin/audit/{target_type}/{id}/unlock` | 释放软锁（仅持有者） |
| POST | `/admin/audit/{target_type}/{id}/pass` | 通过（带 version） |
| POST | `/admin/audit/{target_type}/{id}/reject` | 驳回（带 version + reason [+ notify / block_user]） |
| PUT | `/admin/audit/{target_type}/{id}/edit` | 编辑修正字段 |
| POST | `/admin/audit/{target_type}/{id}/undo` | 30 秒内撤销最近动作 |

### 6.3 账号管理（tag: `admin-accounts`，15）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/accounts/factories` | 厂家列表 |
| POST | `/admin/accounts/factories` | 厂家预注册 |
| GET | `/admin/accounts/factories/{userid}` | 厂家详情 |
| PUT | `/admin/accounts/factories/{userid}` | 厂家编辑（不可改 external_userid） |
| POST | `/admin/accounts/factories/import` | 厂家 Excel 批量导入（全量回滚） |
| GET | `/admin/accounts/brokers` | 中介列表 |
| POST | `/admin/accounts/brokers` | 中介预注册 |
| GET | `/admin/accounts/brokers/{userid}` | 中介详情 |
| PUT | `/admin/accounts/brokers/{userid}` | 中介编辑 |
| POST | `/admin/accounts/brokers/import` | 中介 Excel 批量导入 |
| GET | `/admin/accounts/workers` | 工人列表（只读） |
| GET | `/admin/accounts/workers/{userid}` | 工人详情（只读） |
| GET | `/admin/accounts/blacklist` | 黑名单列表 |
| POST | `/admin/accounts/{userid}/block` | 封禁用户（必填 reason） |
| POST | `/admin/accounts/{userid}/unblock` | 解封用户（必填 reason） |

### 6.4 岗位管理（tag: `admin-jobs`，7）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/jobs` | 岗位列表（白名单筛选 + 排序 + 分页） |
| GET | `/admin/jobs/export` | 导出 CSV（含 BOM，≤ 10000 行） |
| GET | `/admin/jobs/{id}` | 岗位详情（含 version） |
| PUT | `/admin/jobs/{id}` | 编辑（`{version, fields}` + 白名单） |
| POST | `/admin/jobs/{id}/delist` | 下架（`{version, reason}`, reason ∈ manual_delist/filled） |
| POST | `/admin/jobs/{id}/extend` | 延期（`{version, days}`, days ∈ 15/30） |
| POST | `/admin/jobs/{id}/restore` | 取消下架（`{version}`，仅未过期可用） |

### 6.5 简历管理（tag: `admin-resumes`，6）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/resumes` | 简历列表 |
| GET | `/admin/resumes/export` | 导出 CSV |
| GET | `/admin/resumes/{id}` | 简历详情 |
| PUT | `/admin/resumes/{id}` | 编辑（`{version, fields}`） |
| POST | `/admin/resumes/{id}/delist` | 软下架 |
| POST | `/admin/resumes/{id}/extend` | 延期 |

### 6.6 字典管理（tag: `admin-dicts`，10）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/dicts/cities` | 城市字典（按省份分组，默认含禁用项） |
| PUT | `/admin/dicts/cities/{id}` | 编辑城市别名 aliases（仅 aliases 可改） |
| GET | `/admin/dicts/job-categories` | 工种列表 |
| POST | `/admin/dicts/job-categories` | 新增工种 |
| PUT | `/admin/dicts/job-categories/{id}` | 编辑工种 |
| DELETE | `/admin/dicts/job-categories/{id}` | 删除工种（引用中返回 40904） |
| GET | `/admin/dicts/sensitive-words` | 敏感词列表 |
| POST | `/admin/dicts/sensitive-words` | 新增敏感词 |
| DELETE | `/admin/dicts/sensitive-words/{id}` | 删除敏感词 |
| POST | `/admin/dicts/sensitive-words/batch` | 批量新增（返回 {added, duplicated}） |

### 6.7 系统配置（tag: `admin-config`，2）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/config` | 分组返回所有配置（每项含 `danger` 标志） |
| PUT | `/admin/config/{key}` | 单项更新；危险项 notice + audit_log |

### 6.8 数据看板（tag: `admin-reports`，5）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/reports/dashboard` | 概览（today / yesterday / trend_7d） |
| GET | `/admin/reports/trends` | 趋势（`range=7d\|30d\|custom`） |
| GET | `/admin/reports/top` | TOP（`dim=city\|job_category\|role`） |
| GET | `/admin/reports/funnel` | 转化漏斗（近 30 天，5 阶段） |
| GET | `/admin/reports/export` | 数据导出 CSV |

### 6.9 对话日志（tag: `admin-logs`，2）

| Method | Path | 说明 |
|---|---|---|
| GET | `/admin/logs/conversations` | 查询（必填 userid + start + end） |
| GET | `/admin/logs/conversations/export` | 导出 CSV |

### 6.10 事件回传（tag: `events`，1）

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/events/miniprogram_click` | 小程序点击回传（X-Event-Api-Key 鉴权，不走 JWT） |

---

## 7. 前端侧实现建议

### 7.1 Pinia auth store（token 管理）

```typescript
// src/stores/auth.ts
import { defineStore } from "pinia";

interface AuthState {
  token: string | null;
  expiresAt: string | null;
  user: AdminUserRead | null;
  passwordChanged: boolean;
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => ({
    token: localStorage.getItem("token"),
    expiresAt: localStorage.getItem("token_expires"),
    user: null,
    passwordChanged: false,
  }),
  actions: {
    setToken(token: string, expiresAt: string, passwordChanged: boolean) {
      this.token = token;
      this.expiresAt = expiresAt;
      this.passwordChanged = passwordChanged;
      localStorage.setItem("token", token);
      localStorage.setItem("token_expires", expiresAt);
    },
    logout() {
      this.token = null;
      this.expiresAt = null;
      this.user = null;
      this.passwordChanged = false;
      localStorage.removeItem("token");
      localStorage.removeItem("token_expires");
    },
    isExpired(): boolean {
      return !this.expiresAt || new Date(this.expiresAt) <= new Date();
    },
  },
});
```

### 7.2 路由守卫

```typescript
// src/router/index.ts
router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (to.meta.requiresAuth !== false) {
    if (!auth.token || auth.isExpired()) return { name: "login" };
    if (!auth.passwordChanged && to.name !== "change-password") {
      return { name: "change-password" };
    }
    if (!auth.user) {
      try {
        auth.user = await http.get("/admin/me");
      } catch {
        return { name: "login" };
      }
    }
  }
});
```

### 7.3 审核详情的锁与 Undo 钩子

```vue
<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

const lockRefreshTimer = ref<number | null>(null);

async function enterAudit() {
  try {
    await http.post(`/admin/audit/${targetType}/${id}/lock`);
  } catch (err: any) {
    if (err.code === 40901) {
      await ElMessageBox.alert(`此条目正在被 ${err.data.locked_by} 处理`);
      router.back();
      return;
    }
    throw err;
  }
  // 每 4 分钟续锁（TTL 300s）
  lockRefreshTimer.value = window.setInterval(() => {
    http.post(`/admin/audit/${targetType}/${id}/lock`).catch(() => {});
  }, 240_000);
}

async function leaveAudit() {
  if (lockRefreshTimer.value) clearInterval(lockRefreshTimer.value);
  await http.post(`/admin/audit/${targetType}/${id}/unlock`).catch(() => {});
}

onMounted(enterAudit);
onUnmounted(leaveAudit);
</script>
```

### 7.4 版本冲突 40902 的交互

```typescript
try {
  await http.put(`/admin/jobs/${id}`, { version: detail.version, fields });
} catch (err: any) {
  if (err.code === 40902) {
    await ElMessageBox.alert("此条目已被其他管理员修改，点击确认重新加载最新数据");
    await refresh();
    return;
  }
  throw err;
}
```

### 7.5 下载 CSV

见 §4.6 示例。

### 7.6 TypeScript 类型生成（可选）

后端 OpenAPI schema 可通过 `GET http://localhost:8000/openapi.json` 拿到。
前端可以用 [openapi-typescript](https://github.com/drwpow/openapi-typescript) 生成静态类型：

```bash
bun add -D openapi-typescript
bunx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts
```

推荐方案但非必需，Phase 6 MVP 阶段手写类型也可以。

---

## 8. 不在本期范围

前端设计时请不要依赖以下能力（v1 明确不做，Phase 7+ 再议）：

- **RBAC / 多管理员权限矩阵** ——一期单管理员，所有 admin 权限同等
- **找回密码 / 邮箱验证** ——一期管理员密码由运维直接改 DB
- **Refresh Token** ——过期直接重登
- **对话日志全文检索** ——只按 userid + 时间范围
- **告警规则配置 / 自定义看板** ——看板仅提供固定指标
- **审核员绩效统计** ——不做
- **Prompt 可视化编辑** ——LLM prompt 硬编码在后端
- **操作实时推送 / WebSocket** ——所有刷新都是拉取
- **图片 / 视频 / 二进制附件上传** ——一期仅 Excel 导入

---

## 9. 对接问题反馈

### 9.1 后端故障分类

| 现象 | 定位方法 | 责任归属 |
|---|---|---|
| HTTP 5xx | 查 backend 日志 | 后端 |
| HTTP 200 + `code=50001` | 查 backend 日志 message 字段 + `audit_log` | 后端 |
| HTTP 200 + `code=40101` | 检查请求参数，对照 Swagger schema | 前端 |
| HTTP 200 + `code=40902` | 正常业务事件（并发）| 前端交互处理 |
| CORS 报错 | 检查后端 `CORS_ORIGINS` | 运维 |

### 9.2 提 issue 模板

遇到疑似后端 bug 时请提供：

```
- 接口：GET /admin/xxx
- 请求参数：{...}
- Authorization: Bearer eyJ...(前缀即可)
- 响应：{code, message, data}
- 期望：...
- 发生时间：ISO 8601
```

### 9.3 联调约定

- 任何破坏性 API 变更 → 后端先提前同步，更新本文档 + Swagger + 回归
- 字段新增 → 后端直接加，本文档同步更新，前端向后兼容
- 字段删除 → 至少保留一个迭代，期间前端准备好 fallback
