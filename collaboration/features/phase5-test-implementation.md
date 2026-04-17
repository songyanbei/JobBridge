# Phase 5 测试实施文档

> 基于：`collaboration/features/phase5-main.md`
> 面向角色：测试
> 状态：`draft`
> 创建日期：2026-04-16

## 1. 测试目标

Phase 5 测试的重点，是验证运营后台后端的"鉴权 + 审核 + CRUD + 字典 + 配置 + 报表 + 事件回传"7 类接口在契约、业务规则、并发、安全、可观测性等方面是否符合规范，并为 Phase 6 前端联调和 Phase 7 端到端验收提供稳定底座。

测试应重点回答以下问题：

- 所有 `/admin/*` 接口是否都受 JWT 保护
- 审核工作台的软锁、乐观锁、Undo 是否如规范运行
- 写操作是否都写入了 `audit_log`
- 列表筛选、分页、排序、导出是否符合白名单约束
- 危险配置项变更是否落地、缓存是否被刷新、是否被 Phase 4 限流即时感知
- 事件回传 API 的 API Key 鉴权与 10 分钟幂等是否生效
- Excel 批量导入失败时是否能完整回滚并返回精准错误
- 报表接口在缓存命中与失效时表现是否一致

## 2. 当前现状与测试策略

当前基础现状决定了本阶段测试策略：

- Phase 1~4 测试已经覆盖：数据层、基础设施、业务 service、企微异步链路
- Phase 5 新增的主要是 HTTP 接口层与 admin 专用的 service / DTO / 异常处理 / DDL 变更
- 真实前端尚未存在（Phase 6 才做），Phase 5 测试以**接口契约 + 自动化测试 + Swagger UI 手测**为主，不要求 UI 联调

因此本阶段测试重点为：

1. **API 契约**：路径、方法、请求体、响应格式与方案 §13 / 架构 §7.4 / 本阶段 main.md 一致
2. **鉴权与权限**：JWT 校验、token 过期、enabled=0、event_api_key 校验
3. **业务规则**：审核三态机制、乐观锁、软锁、Undo、封禁/解封、延期上限、字典唯一性、配置类型校验
4. **可观测性**：所有写动作触发 `audit_log`
5. **并发/边界**：审核并发锁冲突、Excel 批量回滚、配置缓存清理
6. **联动**：限流配置改动后 Phase 4 行为变化

## 3. 本阶段测试范围

### 3.1 必测范围

1. **登录与鉴权**
   - 正常登录 / 错误密码 / 失败计数 / sleep 1s / enabled=0 / token 过期 / token 无效
   - GET /admin/me / 改密码各分支

2. **审核工作台**
   - 队列分页 / 筛选
   - 详情字段完整
   - 软锁正常 / 软锁冲突 40901
   - 乐观锁正常 / 冲突 40902
   - 通过 / 驳回 / 编辑 / Undo
   - Undo 30 秒边界 40903
   - 写 audit_log 验证（manual_pass / manual_reject / manual_edit / undo）

3. **账号管理**
   - 厂家 / 中介 / 工人列表 / 详情
   - 预注册（重复 external_userid）
   - Excel 批量导入（成功 / 部分失败 / 全部失败 / 公式注入防护）
   - 黑名单封禁 / 解封 / 重复封禁
   - 写 audit_log 验证

4. **岗位 / 简历管理**
   - 列表筛选 / 排序 / 分页
   - 详情字段完整
   - 编辑乐观锁
   - 下架 / 延期 / 取消下架边界
   - CSV 导出（10000 行边界）
   - 写 audit_log 验证

5. **字典管理**
   - 城市仅 alias 编辑
   - 工种 CRUD + 引用检查 + 排序
   - 敏感词 CRUD + 批量导入 + 去重
   - 字典变更触发 service 缓存清理

6. **系统配置**
   - 全部分组返回
   - 单项更新（int/bool/json/string）
   - 危险项触发 audit_log + 缓存清理
   - 不存在的 key 返回 40401
   - 类型不匹配返回 40101

7. **数据看板**
   - Dashboard 含 today / yesterday / trend_7d
   - 60 秒缓存命中 / 失效
   - trends / top / funnel
   - 导出 CSV

8. **对话日志**
   - 必传 userid + 时间范围
   - 范围 > 30 天返回 40101
   - 分页 / 排序
   - 导出 CSV

9. **事件回传 API**
   - API Key 校验（缺失 / 错误 / 正确）
   - 幂等 10 分钟
   - 写入 event_log
   - 写入失败降级（不阻塞回包）

10. **错误响应与全局异常**
    - 所有错误码在 §6.4 范围内
    - pydantic 校验错误返回 40101
    - 未知异常返回 50001

11. **回归**
    - Phase 1~4 自动化测试仍然通过
    - 健康检查 `/health` 仍正常
    - webhook / worker 在 Phase 5 修改 schema/seed 后仍正常

### 3.2 不测范围

- 不测前端页面（Phase 6 范围）
- 不测端到端 UI 联调
- 不测 nginx / TLS（Phase 7 范围）
- 不测定时任务（Phase 7 范围）
- 不测 Phase 3 service 内部业务规则（已覆盖）
- 不测 Phase 4 webhook 链路（已覆盖；本阶段只测限流配置联动 1 条用例）
- 不要求真实小程序客户端联调，事件回传用 curl / pytest 模拟

## 4. 测试环境要求

### 4.1 基础环境

- MySQL 8.0+：本阶段必须先执行 schema 升级（新增 event_log 表、audit_action 枚举扩展）
- Redis 7+
- Python 3.11+：运行测试与 FastAPI app
- 已导入 seed 数据（含 5 个新增 system_config）
- Phase 4 已完成（用于联动验证）

### 4.2 测试数据准备

- 默认管理员账号 `admin / admin123`
- 至少 2 个预注册厂家、2 个预注册中介
- 至少 5 个已注册工人 + 各类岗位/简历样本（覆盖 audit_status：pending / passed / rejected）
- 1 个被封禁用户用于解封测试
- 至少 10 条 conversation_log（含不同 intent）
- 至少 5 条 event_log

### 4.3 工具

- `pytest + httpx.AsyncClient`：API 集成测试
- `openpyxl`：内存生成 xlsx 测试 Excel 导入
- `redis-cli`：手测软锁 / Undo / 缓存
- Swagger UI `/docs`：人工探索式测试
- Postman / curl：复杂场景手测

## 5. 测试用例设计

### 5.1 登录与鉴权

#### TC-5.1.1 登录成功

- **操作**：POST `/admin/login` 用 `admin/admin123`
- **预期**：返回 200，`data.access_token` 非空，`password_changed=false`，`expires_at` 24h 后

#### TC-5.1.2 用户名错误

- **操作**：POST `/admin/login` 用错误账号
- **预期**：返回 `code=40001`

#### TC-5.1.3 密码错误（连续 3 次）

- **操作**：连续 3 次错误密码
- **预期**：每次返回 40001；第 4 次起服务端 sleep 1 秒（响应时间 ≥ 1s）

#### TC-5.1.4 enabled=0

- **前置**：`UPDATE admin_user SET enabled=0 WHERE username='admin'`
- **操作**：登录
- **预期**：返回 40301

#### TC-5.1.5 GET /admin/me 无 token

- **操作**：不带 Authorization header
- **预期**：返回 40003

#### TC-5.1.6 GET /admin/me token 过期

- **前置**：构造 `exp` 已过期的 JWT
- **操作**：访问
- **预期**：返回 40003

#### TC-5.1.7 GET /admin/me 正常

- **操作**：带正确 token
- **预期**：返回管理员资料，无 `password_hash` 字段

#### TC-5.1.8 改密码：旧密码错误

- **预期**：40001

#### TC-5.1.9 改密码：新密码长度 < 8

- **预期**：40101

#### TC-5.1.10 改密码：新旧相同

- **预期**：40101

#### TC-5.1.11 改密码：成功

- **预期**：`password_changed=1`，旧 token 仍可使用 GET /admin/me

### 5.2 审核工作台

#### TC-5.2.1 队列列表分页

- **操作**：GET `/admin/audit/queue?status=pending&page=1&size=10`
- **预期**：分页结构正常，items 含 `locked_by`

#### TC-5.2.2 队列筛选 target_type

- **操作**：`?target_type=job`
- **预期**：仅返回岗位类型

#### TC-5.2.3 待审计数

- **操作**：GET `/admin/audit/pending-count`
- **预期**：返回 `{job, resume, total}`

#### TC-5.2.4 详情字段完整

- **操作**：GET `/admin/audit/job/{id}`
- **预期**：含 `version / locked_by / risk_level / submitter_history / extracted_fields / field_confidence`

#### TC-5.2.5 软锁正常

- **操作**：A 调用 lock → 查询 Redis `audit_lock:job:{id}` 值 = A.username
- **预期**：返回 200

#### TC-5.2.6 软锁冲突

- **前置**：A 已锁
- **操作**：B 调用 lock
- **预期**：40901，`data.locked_by=A.username`

#### TC-5.2.7 软锁释放

- **操作**：A unlock → Redis key 消失
- **预期**：成功

#### TC-5.2.8 非持有者 unlock

- **操作**：B unlock A 的锁
- **预期**：返回 200 但 Redis key 仍存在（仅持有者可释放）

#### TC-5.2.9 通过：版本一致

- **操作**：POST `/pass` 带正确 version
- **预期**：`audit_status=passed`，`version+1`，audit_log 新增 `manual_pass`

#### TC-5.2.10 通过：版本不一致

- **操作**：POST `/pass` 带错误 version
- **预期**：40902，DB 不变

#### TC-5.2.11 驳回必须带 reason

- **操作**：POST `/reject` 不带 reason
- **预期**：40101

#### TC-5.2.12 驳回 + block_user

- **操作**：POST `/reject` 带 `block_user=true`
- **预期**：用户 `status=blocked`，audit_log 同时记录 reject 与 block

#### TC-5.2.13 编辑乐观锁

- **操作**：PUT `/edit` 带正确 / 错误 version
- **预期**：分别 200 / 40902

#### TC-5.2.14 Undo 30 秒内

- **操作**：通过后立即 undo
- **预期**：恢复 `audit_status=pending`，写 audit_log `undo`

#### TC-5.2.15 Undo 超时

- **操作**：Sleep 31 秒后 undo
- **预期**：40903

#### TC-5.2.16 Undo 后无法二次撤销

- **操作**：undo 成功后再次 undo
- **预期**：40903

### 5.3 账号管理

#### TC-5.3.1 厂家列表分页

- **操作**：GET `/admin/accounts/factories?page=1&size=20`
- **预期**：分页正常

#### TC-5.3.2 预注册成功

- **操作**：POST 厂家信息
- **预期**：写入 `user` 表，`role=factory`，audit_log 记录

#### TC-5.3.3 预注册重复 external_userid

- **操作**：再次提交相同 external_userid
- **预期**：40904

#### TC-5.3.4 Excel 导入成功

- **操作**：上传 5 行合法 xlsx
- **预期**：`success_count=5, failed=[]`，DB 新增 5 条

#### TC-5.3.5 Excel 导入部分失败 → 全部回滚

- **操作**：上传 5 行，第 3 行 phone 重复
- **预期**：返回 `failed=[{row:3,...}]`，DB 不新增任何

#### TC-5.3.6 Excel 公式注入

- **操作**：上传 phone=`=cmd|...`
- **预期**：服务端去除/转义后入库或拒绝

#### TC-5.3.7 工人列表只读

- **操作**：POST `/admin/accounts/workers`
- **预期**：404 或 405（路由不存在）

#### TC-5.3.8 黑名单封禁

- **操作**：POST `/admin/accounts/{userid}/block` 带 reason
- **预期**：user.status=blocked，audit_log 记录

#### TC-5.3.9 重复封禁

- **预期**：40904

#### TC-5.3.10 封禁不带 reason

- **预期**：40101

#### TC-5.3.11 解封成功

- **操作**：POST unblock 带 reason
- **预期**：user.status=active，audit_log 记录

### 5.4 岗位 / 简历管理

#### TC-5.4.1 列表筛选

- **操作**：GET `/admin/jobs?city=苏州市&job_category=电子厂&audit_status=pending`
- **预期**：筛选生效

#### TC-5.4.2 排序

- **操作**：`?sort=created_at:desc`
- **预期**：按时间倒序

#### TC-5.4.3 排序字段非白名单

- **操作**：`?sort=password:asc`
- **预期**：40101

#### TC-5.4.4 编辑乐观锁

- **同 TC-5.2.13**

#### TC-5.4.5 下架 manual_delist

- **操作**：POST `/delist` `{reason:"manual_delist"}`
- **预期**：`delist_reason=manual_delist`，audit_log 记录

#### TC-5.4.6 下架 filled

- **预期**：`delist_reason=filled`

#### TC-5.4.7 延期 15 天

- **操作**：POST `/extend` `{days:15}`
- **预期**：`expires_at += 15d`

#### TC-5.4.8 延期超出 TTL 上限

- **前置**：`expires_at` 已接近上限
- **操作**：再延期 30 天
- **预期**：仅延到上限或返回 40101（按实现选其一，需文档化）

#### TC-5.4.9 取消下架

- **前置**：`delist_reason=manual_delist` 且 `expires_at>now()`
- **预期**：`delist_reason=NULL`

#### TC-5.4.10 取消下架已过期岗位

- **预期**：40904

#### TC-5.4.11 CSV 导出

- **操作**：GET `/export?city=苏州市`
- **预期**：返回 CSV，含 BOM，文件名 `jobs_*.csv`

#### TC-5.4.12 CSV 导出超过 10000 行

- **预期**：40101

### 5.5 字典管理

#### TC-5.5.1 城市分组返回

- **操作**：GET `/admin/dicts/cities`
- **预期**：按省份分组

#### TC-5.5.2 城市编辑 alias

- **操作**：PUT 修改 aliases
- **预期**：成功

#### TC-5.5.3 城市修改其它字段

- **操作**：PUT 修改 name
- **预期**：被忽略或 40101（按实现选其一）

#### TC-5.5.4 工种新增重复 code

- **预期**：40904

#### TC-5.5.5 工种删除被引用

- **前置**：存在引用此工种的 job
- **预期**：40904

#### TC-5.5.6 敏感词批量导入

- **操作**：POST `/batch` 10 个词，其中 2 个已存在
- **预期**：`{added:8, duplicated:2}`

#### TC-5.5.7 字典变更后 audit_service 缓存被清

- **手段**：观察 `audit_service.invalidate_cache()` 被调用（mock 或日志）

### 5.6 系统配置

#### TC-5.6.1 全部分组返回

- **操作**：GET `/admin/config`
- **预期**：分组键 `ttl / match / filter / audit / llm / session / upload / report / rate_limit / event / account`

#### TC-5.6.2 单项更新 int

- **操作**：PUT `/admin/config/match.top_n` `{config_value:"5"}`
- **预期**：DB 更新，缓存清除

#### TC-5.6.3 类型不匹配

- **操作**：PUT `match.top_n` `{config_value:"abc"}`
- **预期**：40101

#### TC-5.6.4 不存在的 key

- **操作**：PUT `/admin/config/not.exists`
- **预期**：40401

#### TC-5.6.5 危险项变更

- **操作**：PUT `/admin/config/filter.enable_gender`
- **预期**：audit_log action=`manual_edit`，target_type=`system`，target_id=`filter.enable_gender`，snapshot 含 old/new

#### TC-5.6.6 限流配置变更联动 Phase 4

- **操作**：将 `rate_limit.max_count` 改为 1，发送 2 条消息
- **预期**：第 2 条触发限流（端到端验证）

### 5.7 数据看板

#### TC-5.7.1 Dashboard 正常返回

- **操作**：GET `/admin/reports/dashboard`
- **预期**：含 today / yesterday / trend_7d

#### TC-5.7.2 缓存命中

- **操作**：连续两次调用，第二次响应时间 < 50ms
- **预期**：Redis 中存在 `report_cache:dashboard`

#### TC-5.7.3 缓存过期

- **操作**：等待 60 秒后再次调用
- **预期**：触发新一次计算

#### TC-5.7.4 趋势 7d

- **操作**：GET `/trends?range=7d`
- **预期**：返回 7 个数据点

#### TC-5.7.5 自定义范围

- **操作**：`?range=custom&from=2026-04-01&to=2026-04-07`
- **预期**：返回对应数据

#### TC-5.7.6 漏斗

- **操作**：GET `/funnel`
- **预期**：5 个阶段、按数值递减

#### TC-5.7.7 导出

- **操作**：GET `/export?metric=dau&from=&to=`
- **预期**：CSV 文件

### 5.8 对话日志

#### TC-5.8.1 必传 userid

- **操作**：GET 不带 userid
- **预期**：40101

#### TC-5.8.2 时间范围 > 30 天

- **预期**：40101

#### TC-5.8.3 正常查询

- **操作**：GET 带 userid + start + end
- **预期**：返回该用户记录，倒序

#### TC-5.8.4 筛选 direction / intent

- **预期**：筛选生效

#### TC-5.8.5 导出 CSV

- **操作**：GET `/export`
- **预期**：CSV 文件

### 5.9 事件回传 API

#### TC-5.9.1 缺失 API Key

- **操作**：POST 不带 X-Event-Api-Key
- **预期**：40001

#### TC-5.9.2 错误 API Key

- **操作**：POST 带错误 key
- **预期**：40001

#### TC-5.9.3 正常上报

- **操作**：POST 带正确 key
- **预期**：写入 event_log，返回 `data.deduped=false`

#### TC-5.9.4 10 分钟内重复

- **操作**：再次提交相同 (userid, target_type, target_id)
- **预期**：返回 `code=0, data.deduped=true`，event_log 不新增

#### TC-5.9.5 写入 event_log 失败

- **手段**：mock event_log 写库失败
- **预期**：仍返回 200，audit_log 写入失败原因

#### TC-5.9.6 不同 target 不去重

- **操作**：不同 target_id
- **预期**：分别写入

### 5.10 全局异常与错误码

#### TC-5.10.1 pydantic 校验错误

- **操作**：缺必填字段
- **预期**：返回 40101，`data.fields` 含错误明细

#### TC-5.10.2 未捕获异常

- **手段**：mock service 抛 Exception
- **预期**：返回 50001

#### TC-5.10.3 错误码区段
   
- **手段**：所有响应错误码在 §6.4 范围内（统计测试期间所有失败响应的 code）

### 5.11 回归

- [ ] Phase 1~4 自动化测试仍然全部通过
- [ ] webhook 仍可接收消息
- [ ] worker 仍可消费
- [ ] schema 升级后旧 audit_log 记录仍可读

## 6. 验收确认项

以下为本阶段测试通过的最低标准：

- [ ] 50 个端点全部在 Swagger UI 可见且可调通
- [ ] 所有 `/admin/*` 接口都受 JWT 保护
- [ ] 登录失败计数 + sleep 防爆破生效
- [ ] 改密码三类校验生效
- [ ] 审核软锁、乐观锁、Undo 三层机制生效
- [ ] 通过/驳回/编辑/Undo 均写 audit_log
- [ ] 账号预注册、Excel 批量导入、封禁/解封正常
- [ ] Excel 导入失败 100% 回滚
- [ ] 岗位/简历列表筛选/排序/分页/导出正常
- [ ] 字典 CRUD + 唯一性 + 引用检查 + 批量去重正常
- [ ] 系统配置类型校验 + 危险项 audit_log + 缓存清理正常
- [ ] 限流配置变更联动 Phase 4 webhook
- [ ] Dashboard 缓存 60 秒命中 / 失效正确
- [ ] 报表 / 漏斗 / 趋势 / 导出可用
- [ ] 对话日志 30 天范围限制生效
- [ ] 事件回传 API Key + 幂等 + 写库失败降级正常
- [ ] 全部错误响应在 §6.4 错误码范围内
- [ ] Phase 1~4 测试全量回归通过

## 7. 测试工具与脚本

### 7.1 集成测试 fixture

```python
# conftest.py
@pytest.fixture
def admin_token(client):
    resp = client.post("/admin/login", json={"username": "admin", "password": "admin123"})
    return resp.json()["data"]["access_token"]

@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}

@pytest.fixture
def event_api_key_headers():
    return {"X-Event-Api-Key": settings.event_api_key}

@pytest.fixture
def make_xlsx():
    def _make(rows: list[dict]) -> bytes:
        from openpyxl import Workbook
        from io import BytesIO
        wb = Workbook()
        ws = wb.active
        cols = list(rows[0].keys())
        ws.append(cols)
        for row in rows:
            ws.append([row[c] for c in cols])
        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    return _make
```

### 7.2 Redis 状态检查

```bash
# 软锁
redis-cli KEYS "audit_lock:*"
redis-cli GET "audit_lock:job:1"

# Undo
redis-cli KEYS "undo_action:*"

# 配置缓存
redis-cli KEYS "config_cache:*"

# 报表缓存
redis-cli KEYS "report_cache:*"

# 事件幂等
redis-cli KEYS "event_idem:*"

# 登录失败计数
redis-cli KEYS "admin_login_fail:*"
```

### 7.3 DB 状态检查

```sql
-- 审核日志
SELECT id, target_type, target_id, action, operator, reason, created_at
FROM audit_log ORDER BY id DESC LIMIT 20;

-- 用户状态
SELECT external_userid, role, status, blocked_reason FROM user WHERE status='blocked';

-- 岗位下架
SELECT id, audit_status, delist_reason, expires_at FROM job WHERE delist_reason IS NOT NULL;

-- 系统配置
SELECT * FROM system_config WHERE config_key LIKE 'rate_limit%';

-- 事件
SELECT * FROM event_log ORDER BY id DESC LIMIT 10;
```

### 7.4 手测脚本：限流配置联动

```bash
# 1) 修改 rate_limit.max_count = 1
curl -X PUT http://localhost:8000/admin/config/rate_limit.max_count \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"config_value": "1"}'

# 2) 用 simulate_wecom.py 连发 2 条消息
python scripts/simulate_wecom.py --userid wkr_test_001 --content "苏州找电子厂"
python scripts/simulate_wecom.py --userid wkr_test_001 --content "苏州找电子厂2"

# 3) 验证第二条被限流（不写入 wecom_inbound_event）
mysql -e "SELECT COUNT(*) FROM wecom_inbound_event WHERE from_userid='wkr_test_001' AND created_at > NOW() - INTERVAL 1 MINUTE"
```

### 7.5 事件回传手测

```bash
curl -X POST http://localhost:8000/api/events/miniprogram_click \
  -H "X-Event-Api-Key: $EVENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"userid":"wkr_001","target_type":"job","target_id":42}'

# 重复一次验证 deduped=true
curl ... # 同上
```

## 8. 缺陷上报模板

每个缺陷至少包含：

- 测试用例编号
- 复现步骤
- 期望结果
- 实际结果
- 截图或日志
- 环境信息（commit hash、admin token 是否存在等）
- 影响范围（鉴权 / 审核 / 账号 / 字典 / 配置 / 报表 / 日志 / 事件）
