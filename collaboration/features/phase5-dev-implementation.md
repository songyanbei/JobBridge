# Phase 5 开发实施文档

> 基于：`collaboration/features/phase5-main.md`
> 面向角色：后端开发
> 状态：`draft`
> 创建日期：2026-04-16

## 1. 开发目标

本阶段开发目标，是把整套运营后台需要的"鉴权 + CRUD + 审核 + 报表 + 事件回传"接口做出来，为 Phase 6 的前端联调提供稳定契约。

开发时请始终记住：

- Phase 3 的 7 个 service（user / intent / conversation / audit / search / upload / permission）已完成业务核心能力，Phase 5 的 admin 接口**只做编排和 DTO 转换**，不重写业务规则
- Phase 4 的 webhook / worker / message_router 已经在跑，Phase 5 的限流配置变更必须能立即影响 Phase 4 的行为（缓存清理）
- 所有 `/admin/*` 接口必须经过统一鉴权依赖；不允许任何匿名端点
- 所有写操作必须写 `audit_log`；`operator` 字段必须等于当前管理员 username
- 所有列表必须支持分页；分页字段固定 `page / size`
- 所有 ORM 对象必须经 schema DTO 转换后再返回
- 不允许 admin 接口直接拼裸 SQL，必须经 service 层

## 2. 当前代码现状

可直接复用：

- `app/models.py` 11 张表
- `app/schemas/admin.py` 已有 `AdminLogin / AdminToken / AdminUserRead / SystemConfigRead / SystemConfigUpdate / AuditLogRead`
- `app/services/`：Phase 3+4 的 9 个 service
- `app/core/redis_client.py`：session、锁、限流、幂等、队列、配置缓存基础
- `app/core/pagination.py`、`app/core/exceptions.py`
- `app/api/webhook.py`（Phase 4）
- `app/main.py` 已注册 webhook
- `app/config.py` 已含 `admin_jwt_secret / admin_jwt_expires_hours`

需新建：

- `app/api/deps.py`、`app/core/security.py`、`app/core/responses.py`
- `app/api/admin/{auth,audit,accounts,jobs,resumes,dicts,config,reports,logs}.py`
- `app/api/events.py`
- `app/services/{admin_user_service,audit_workbench_service,account_service,dict_service,system_config_service,report_service,event_service}.py`
- `app/schemas/{audit,account,dict,report,event}.py`
- `models.py` 新增 `EventLog` 类，扩展 `audit_action` 枚举（`manual_edit / undo`）
- `sql/schema.sql` 同步表与枚举变更
- `sql/seed.sql` 新增 5 个 system_config key
- `requirements.txt` 补 jose / passlib / openpyxl

## 3. 开发原则

### 3.1 依赖边界

- `api/admin/*` → `services/*_service` → `models.py + core/*`
- `api/admin/*` 严禁 `from sqlalchemy import` 写裸 SQL；统一通过 service
- `api/admin/*` 严禁直接 `import redis`；统一通过 `core/redis_client`
- `api/events.py` 同样走 service
- `services/*_service` 不 import `api/*`、不 import `wecom/*`、不 import `llm/providers/*`
- DTO 转换仅在 api 层完成；service 层返回 ORM 或 dataclass

### 3.2 鉴权边界

- 所有 `/admin/*` 路由通过 `Depends(require_admin)` 注入 `current_admin: AdminUser`
- 所有写动作的 service 调用必须显式传 `operator: str` 参数
- 写 `audit_log` 时 `operator` 必须填，**不能 None**
- 事件回传走独立的 `Depends(require_event_api_key)`，与 JWT 完全分离

### 3.3 异常与响应

- 所有业务异常继承 `core/exceptions.py:BusinessException`，构造时带 `code` 和 `message`
- 路由层不写 try/except；统一在 `main.py` 全局异常处理器封装
- 校验错误（pydantic）由 FastAPI 的 `RequestValidationError` 统一转 `40101`

### 3.4 性能与安全

- Dashboard / 报表必须缓存 60 秒
- 列表接口必须分页，无分页等价于禁止
- 导出接口默认上限 10000 行
- Excel 导入禁止公式注入：去除 `= + - @` 开头字段
- 危险配置项变更必须二次确认（提示文案 + audit_log）

## 4. 逐模块开发指引

### 4.1 模块 A：基础设施与共享依赖

#### 4.1.1 `core/security.py`

```python
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import jwt, JWTError
from app.config import settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return _pwd.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)

def create_admin_token(admin_id: int, username: str) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.admin_jwt_expires_hours)
    payload = {"sub": str(admin_id), "username": username, "exp": expires_at}
    token = jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")
    return token, expires_at

def decode_admin_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.admin_jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise BusinessException(code=40003, message="Token 无效") from exc
```

#### 4.1.2 `api/deps.py`

```python
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login", auto_error=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_redis():
    return redis_client.get_redis()

def require_admin(token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> AdminUser:
    if not token:
        raise BusinessException(40003, "Token 无效")
    claims = decode_admin_token(token)
    admin = db.query(AdminUser).get(int(claims["sub"]))
    if not admin or not admin.enabled:
        raise BusinessException(40003, "Token 无效")
    return admin

def require_event_api_key(x_event_api_key: str | None = Header(None)) -> None:
    if x_event_api_key != settings.event_api_key:
        raise BusinessException(40001, "Invalid API Key")
```

#### 4.1.3 `core/responses.py`

```python
def ok(data=None) -> dict:
    return {"code": 0, "message": "ok", "data": data}

def fail(code: int, message: str, data=None) -> dict:
    return {"code": code, "message": message, "data": data}

def paged(items, total, page, size) -> dict:
    pages = (total + size - 1) // size if size else 0
    return ok({"items": items, "total": total, "page": page, "size": size, "pages": pages})
```

#### 4.1.4 `main.py` 异常处理器

```python
@app.exception_handler(BusinessException)
async def business_exc(request, exc):
    return JSONResponse(status_code=200, content=fail(exc.code, exc.message))

@app.exception_handler(RequestValidationError)
async def validation_exc(request, exc):
    return JSONResponse(status_code=200, content=fail(40101, "参数错误", {"fields": exc.errors()}))

@app.exception_handler(HTTPException)
async def http_exc(request, exc):
    return JSONResponse(status_code=exc.status_code, content=fail(exc.status_code * 100, str(exc.detail)))
```

### 4.2 模块 B：登录与鉴权

#### 4.2.1 `api/admin/auth.py`

```python
@router.post("/admin/login")
def login(req: AdminLogin, db=Depends(get_db), redis=Depends(get_redis)):
    # 1. 查 admin_user
    admin = admin_user_service.get_by_username(db, req.username)

    # 2. 检查失败次数
    fail_key = f"admin_login_fail:{req.username}"
    fail_count = int(redis.get(fail_key) or 0)
    if fail_count >= 3:
        time.sleep(1)  # 简单延迟防爆破

    # 3. 校验密码
    if not admin or not verify_password(req.password, admin.password_hash):
        redis.incr(fail_key)
        redis.expire(fail_key, 60)
        raise BusinessException(40001, "用户名或密码错误")

    # 4. 校验 enabled
    if not admin.enabled:
        raise BusinessException(40301, "账号已禁用")

    # 5. 颁发 token
    token, expires_at = create_admin_token(admin.id, admin.username)

    # 6. 更新 last_login_at
    admin.last_login_at = datetime.now()
    db.commit()

    redis.delete(fail_key)
    return ok({
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "password_changed": bool(admin.password_changed),
    })

@router.get("/admin/me")
def me(current=Depends(require_admin)):
    return ok(AdminUserRead.model_validate(current).model_dump())

@router.put("/admin/me/password")
def change_password(req: ChangePasswordRequest, current=Depends(require_admin), db=Depends(get_db)):
    if not verify_password(req.old_password, current.password_hash):
        raise BusinessException(40001, "原密码错误")
    if len(req.new_password) < 8:
        raise BusinessException(40101, "新密码长度至少 8 位")
    if req.old_password == req.new_password:
        raise BusinessException(40101, "新密码不能与旧密码相同")
    current.password_hash = hash_password(req.new_password)
    current.password_changed = 1
    db.commit()
    return ok()
```

#### 4.2.2 `services/admin_user_service.py`

```python
def get_by_username(db, username: str) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.username == username).first()
```

### 4.3 模块 C：审核工作台

#### 4.3.1 软锁与 Undo 工具（在 `core/redis_client.py` 中）

```python
AUDIT_LOCK_PREFIX = "audit_lock:"
AUDIT_LOCK_TTL = 300
UNDO_PREFIX = "undo_action:"
UNDO_TTL = 30

def acquire_audit_lock(target_type: str, target_id: int, operator: str) -> bool:
    r = get_redis()
    return r.set(f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}", operator, nx=True, ex=AUDIT_LOCK_TTL) is not None

def get_audit_lock_holder(target_type: str, target_id: int) -> str | None:
    r = get_redis()
    return r.get(f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}")

def release_audit_lock(target_type: str, target_id: int, operator: str) -> bool:
    r = get_redis()
    key = f"{AUDIT_LOCK_PREFIX}{target_type}:{target_id}"
    if r.get(key) == operator:
        r.delete(key)
        return True
    return False

def save_undo(target_type: str, target_id: int, action_payload: dict) -> None:
    r = get_redis()
    r.setex(f"{UNDO_PREFIX}{target_type}:{target_id}", UNDO_TTL, json.dumps(action_payload, ensure_ascii=False))

def pop_undo(target_type: str, target_id: int) -> dict | None:
    r = get_redis()
    key = f"{UNDO_PREFIX}{target_type}:{target_id}"
    data = r.get(key)
    if not data:
        return None
    r.delete(key)
    return json.loads(data)
```

#### 4.3.2 `services/audit_workbench_service.py`

提供以下方法（部分签名）：

```python
def list_queue(db, status, target_type, page, size, filters) -> tuple[list, int]: ...
def get_pending_count(db) -> dict: ...
def get_detail(db, target_type, target_id, current_admin) -> dict:
    """返回前端所需详情，含 version / locked_by / risk_level / submitter_history / extracted_fields / field_confidence。"""

def lock(target_type, target_id, operator) -> None:
    holder = get_audit_lock_holder(target_type, target_id)
    if holder and holder != operator:
        raise BusinessException(40901, "条目正在被其他审核员处理", {"locked_by": holder})
    acquire_audit_lock(target_type, target_id, operator)

def unlock(target_type, target_id, operator) -> None:
    release_audit_lock(target_type, target_id, operator)

def pass_action(db, target_type, target_id, version, operator) -> None:
    obj = _load_with_lock(db, target_type, target_id)
    _check_version(obj, version)
    before = _snapshot(obj)
    obj.audit_status = "passed"
    obj.audited_by = operator
    obj.audited_at = datetime.now()
    obj.version += 1
    _write_audit_log(db, target_type, target_id, "manual_pass", operator, before, _snapshot(obj))
    db.commit()
    save_undo(target_type, target_id, {"action": "pass", "before": before, "operator": operator, "ts": time.time()})

def reject_action(db, target_type, target_id, version, reason, notify, block_user, operator) -> None: ...
def edit_action(db, target_type, target_id, version, payload, operator) -> None: ...
def undo(db, target_type, target_id, operator) -> None: ...
```

实现细节：

- `_check_version(obj, version)`：对比 `obj.version`，不一致抛 40902
- 编辑接口仅允许更新预定义字段白名单
- 通过 / 驳回 / 编辑后必须写 `audit_log`，`snapshot` 含 `{before, after, version_before, version_after}`
- Undo：从 Redis 取出快照 → 反向恢复 → 删除 Redis key → 写 `audit_log` `action="undo"`

#### 4.3.3 `api/admin/audit.py`

```python
@router.get("/admin/audit/queue")
def queue(status="pending", target_type=None, page=1, size=20, db=Depends(get_db), current=Depends(require_admin)):
    items, total = audit_workbench_service.list_queue(db, status, target_type, page, size, {})
    return paged([_to_dto(x) for x in items], total, page, size)

@router.get("/admin/audit/pending-count")
def pending(db=Depends(get_db), current=Depends(require_admin)):
    return ok(audit_workbench_service.get_pending_count(db))

@router.get("/admin/audit/{target_type}/{target_id}")
def detail(target_type, target_id, db=Depends(get_db), current=Depends(require_admin)):
    return ok(audit_workbench_service.get_detail(db, target_type, target_id, current))

@router.post("/admin/audit/{target_type}/{target_id}/lock")
def lock(target_type, target_id, current=Depends(require_admin)):
    audit_workbench_service.lock(target_type, target_id, current.username)
    return ok()

# unlock / pass / reject / edit / undo 依此类推
```

### 4.4 模块 D：账号管理

#### 4.4.1 `services/account_service.py`

```python
def list_factories(db, page, size, filters): ...
def list_brokers(db, page, size, filters): ...
def list_workers(db, page, size, filters): ...
def list_blacklist(db, page, size): ...

def pre_register(db, role, payload, operator) -> User:
    if role not in ("factory", "broker"):
        raise BusinessException(40101, "仅厂家/中介可预注册")
    external = payload.external_userid or _gen_external_userid(role)
    if db.query(User).get(external):
        raise BusinessException(40904, "external_userid 已存在")
    user = User(external_userid=external, role=role, ...)
    db.add(user)
    _write_audit_log(db, "user", external, "manual_pass", operator, None, _snap(user), reason="pre_register")
    db.commit()
    return user

def import_excel(db, role, file_bytes, operator) -> dict:
    rows = _parse_xlsx(file_bytes, max_rows=cfg("account.import_max_rows"))
    success, failed = [], []
    try:
        for idx, row in enumerate(rows, 2):  # 第 2 行起为数据
            row = _sanitize(row)  # CSV 注入防护
            try:
                pre_register(db, role, _row_to_payload(row), operator)
                success.append(idx)
            except BusinessException as e:
                failed.append({"row": idx, "error": e.message})
        if failed:
            db.rollback()  # 任何一行失败 → 全部回滚
            return {"success_count": 0, "failed": failed}
        db.commit()
        return {"success_count": len(success), "failed": []}
    except Exception:
        db.rollback()
        raise

def block_user(db, userid, reason, operator):
    user = db.query(User).get(userid)
    if not user:
        raise BusinessException(40401, "用户不存在")
    if user.status == "blocked":
        raise BusinessException(40904, "用户已被封禁")
    before = _snap(user)
    user.status = "blocked"
    user.blocked_reason = reason
    _write_audit_log(db, "user", userid, "manual_reject", operator, before, _snap(user), reason=reason)
    db.commit()

def unblock_user(db, userid, reason, operator): ...
```

#### 4.4.2 `api/admin/accounts.py`

按"列表 / 详情 / 编辑 / 预注册 / 导入 / 封禁 / 解封"依次定义路由；中介路由结构与厂家一致；工人 / 黑名单仅暴露查询。

`POST /admin/accounts/factories/import`：

- `Content-Type: multipart/form-data`，字段 `file`
- 限制文件大小 ≤ 2MB；扩展名 `.xlsx`
- 调用 `account_service.import_excel(...)`

### 4.5 模块 E：岗位 / 简历管理

`api/admin/jobs.py` 与 `api/admin/resumes.py` 结构一致。

筛选白名单参数转 dict 后调 service：

```python
@router.get("/admin/jobs")
def list_jobs(
    city: str | None = None,
    job_category: str | None = None,
    audit_status: str | None = None,
    delist_reason: str | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = 1,
    size: int = 20,
    sort: str = "created_at:desc",
    db=Depends(get_db),
    current=Depends(require_admin),
):
    items, total = job_admin_service.list_jobs(db, locals())
    return paged([JobAdminListOut.model_validate(x).model_dump() for x in items], total, page, size)
```

下架接口：

```python
@router.post("/admin/jobs/{job_id}/delist")
def delist(job_id, payload: JobDelistRequest, db=Depends(get_db), current=Depends(require_admin)):
    job_admin_service.delist(db, job_id, payload.reason, current.username)
    return ok()
```

延期接口：`payload.days ∈ {15, 30}`，超出抛 40101。

导出接口：返回 `StreamingResponse(media_type="text/csv")`，文件名带时间戳，写入 BOM `\ufeff`。

### 4.6 模块 F：字典管理

`services/dict_service.py`：

```python
def list_cities(db, province=None, keyword=None, page=1, size=50): ...
def update_city_aliases(db, city_id, aliases, operator): ...

def list_job_categories(db): ...
def create_job_category(db, payload, operator): ...
def update_job_category(db, cat_id, payload, operator): ...
def delete_job_category(db, cat_id, operator):
    # 删除前检查是否被引用
    if db.query(Job).filter(Job.job_category == name).first():
        raise BusinessException(40904, "工种正在被使用，无法删除")

def list_sensitive_words(db, level=None, keyword=None, page=1, size=50): ...
def add_sensitive_word(db, word, level, category, operator): ...
def delete_sensitive_word(db, word_id, operator): ...
def batch_add_sensitive_words(db, words, level, category, operator) -> dict:
    added, duplicated = [], []
    for w in set(words):
        if db.query(DictSensitiveWord).filter_by(word=w).first():
            duplicated.append(w)
        else:
            db.add(DictSensitiveWord(word=w, level=level, category=category, enabled=1))
            added.append(w)
    db.commit()
    return {"added": len(added), "duplicated": len(duplicated)}
```

字典变更后必须清除业务侧缓存（如 `audit_service` 的敏感词缓存，由 `audit_service.invalidate_cache()` 提供）。

### 4.7 模块 G：系统配置

`services/system_config_service.py`：

```python
def list_grouped(db) -> dict[str, list]:
    items = db.query(SystemConfig).order_by(SystemConfig.config_key).all()
    grouped = defaultdict(list)
    for it in items:
        prefix = it.config_key.split(".")[0]
        grouped[prefix].append(SystemConfigRead.model_validate(it).model_dump())
    return dict(grouped)

DANGER_KEYS = {"filter.enable_gender", "filter.enable_age", "filter.enable_ethnicity", "llm.provider"}

def update(db, redis, key, new_value, value_type_override, operator) -> dict:
    item = db.query(SystemConfig).get(key)
    if not item:
        raise BusinessException(40401, f"配置项 {key} 不存在")
    _validate_value(value_type_override or item.value_type, new_value)
    before = item.config_value
    item.config_value = new_value
    if value_type_override:
        item.value_type = value_type_override
    item.updated_by = operator
    if key in DANGER_KEYS:
        _write_audit_log(db, "system", key, "manual_edit", operator,
                         {"old": before}, {"new": new_value}, reason="danger_config_change")
    db.commit()
    # 清缓存
    redis.delete(f"config_cache:{key}")
    redis.delete("config_cache:all")
    return {"changed": before != new_value, "danger": key in DANGER_KEYS}
```

`api/admin/config.py`：

```python
@router.get("/admin/config")
def list_config(db=Depends(get_db), current=Depends(require_admin)):
    return ok(system_config_service.list_grouped(db))

@router.put("/admin/config/{key}")
def update_config(key, payload: SystemConfigUpdate, db=Depends(get_db), redis=Depends(get_redis), current=Depends(require_admin)):
    result = system_config_service.update(db, redis, key, payload.config_value, payload.value_type, current.username)
    return ok(result)
```

### 4.8 模块 H：数据看板

`services/report_service.py`：

```python
DASHBOARD_CACHE = "report_cache:dashboard"

def get_dashboard(db, redis, force=False) -> dict:
    cache = redis.get(DASHBOARD_CACHE) if not force else None
    if cache:
        return json.loads(cache)
    today = date.today()
    yesterday = today - timedelta(days=1)
    data = {
        "today": _calc_day(db, today),
        "yesterday": _calc_day(db, yesterday),
        "trend_7d": [_calc_day(db, today - timedelta(days=i)) for i in range(6, -1, -1)],
    }
    redis.setex(DASHBOARD_CACHE, _cfg("report.cache_ttl_seconds", 60), json.dumps(data, default=str))
    return data

def _calc_day(db, day) -> dict:
    """聚合当日 DAU、上传数、检索次数、命中率、空召回率、待审数。"""
    ...

def get_trends(db, range_str, frm, to): ...
def get_top(db, dim, limit): ...
def get_funnel(db, frm, to): ...
def export(db, metric, frm, to, fmt): ...
```

数据来源：

- DAU：`user.last_active_at`
- 上传数：`job.created_at` / `resume.created_at`
- 检索次数：`conversation_log` where `intent in ('search_job', 'search_worker', 'show_more', 'follow_up')`
- 命中率：从 conversation_log 中 `direction='out'` 含推荐结果的占比（用 `criteria_snapshot.has_recommend` 字段判定）
- 空召回率：`criteria_snapshot.recommend_count == 0` 占比
- 待审数：`job + resume` 的 `audit_status='pending'` 计数
- 详情点击率：`event_log` 与 `推荐次数` 的比

具体 SQL 与 join 由开发实现，保持单次接口耗时 < 500ms（缓存外）。

### 4.9 模块 I：对话日志

`api/admin/logs.py`：

```python
@router.get("/admin/logs/conversations")
def list_conversations(
    userid: str,
    start: datetime,
    end: datetime,
    direction: str | None = None,
    intent: str | None = None,
    page: int = 1,
    size: int = 50,
    db=Depends(get_db),
    current=Depends(require_admin),
):
    if (end - start).days > 30:
        raise BusinessException(40101, "时间范围最大 30 天")
    items, total = log_service.list_conversations(db, userid, start, end, direction, intent, page, size)
    return paged([ConversationLogOut.model_validate(x).model_dump() for x in items], total, page, size)

@router.get("/admin/logs/conversations/export")
def export_conversations(...):
    return StreamingResponse(_csv_iter(...), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="conversations_{ts}.csv"'
    })
```

### 4.10 模块 J：事件回传 API

`api/events.py`：

```python
router = APIRouter(prefix="/api/events", tags=["events"])

@router.post("/miniprogram_click")
def miniprogram_click(
    payload: MiniProgramClickRequest,
    db=Depends(get_db),
    redis=Depends(get_redis),
    _=Depends(require_event_api_key),
):
    deduped = event_service.record_click(db, redis, payload)
    return ok({"deduped": deduped})
```

`services/event_service.py`：

```python
WINDOW_DEFAULT = 600

def record_click(db, redis, payload) -> bool:
    key = f"event_idem:{payload.userid}:{payload.target_type}:{payload.target_id}"
    set_ok = redis.set(key, "1", nx=True, ex=_cfg_int("event.dedupe_window_seconds", WINDOW_DEFAULT))
    if set_ok is None:
        return True  # 已去重
    try:
        db.add(EventLog(
            event_type="miniprogram_click",
            userid=payload.userid,
            target_type=payload.target_type,
            target_id=payload.target_id,
            occurred_at=datetime.fromtimestamp(payload.timestamp) if payload.timestamp else datetime.now(),
        ))
        db.commit()
    except Exception as exc:
        logger.error(f"event_log write failed: {exc}")
        _write_audit_log(db, "user", payload.userid, "auto_reject", "system", None, None, reason=f"event_log write failed: {exc}")
        db.commit()
    return False
```

### 4.11 模块 K：基础设施补强

#### 4.11.1 `models.py` 新增

```python
class EventLog(Base):
    __tablename__ = "event_log"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    event_type = sa.Column(sa.Enum("miniprogram_click", name="event_type"), nullable=False)
    userid = sa.Column(sa.String(64), nullable=False)
    target_type = sa.Column(sa.Enum("job", "resume", name="event_target_type"), nullable=False)
    target_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    occurred_at = sa.Column(sa.DateTime, nullable=False)
    extra = sa.Column(sa.JSON, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    __table_args__ = (
        sa.Index("idx_target", "target_type", "target_id", "occurred_at"),
        sa.Index("idx_user_time", "userid", "occurred_at"),
    )
```

#### 4.11.2 `audit_action` 枚举扩展

`models.py`：

```python
action = sa.Column(
    sa.Enum(
        "auto_pass", "auto_reject",
        "manual_pass", "manual_reject",
        "manual_edit", "undo",
        "appeal", "reinstate",
        name="audit_action"
    ),
    nullable=False,
)
```

`schema.sql` 同步：

```sql
ALTER TABLE audit_log MODIFY COLUMN action
ENUM('auto_pass','auto_reject','manual_pass','manual_reject','manual_edit','undo','appeal','reinstate') NOT NULL;
```

#### 4.11.3 `seed.sql` 新增配置项

```sql
INSERT INTO `system_config` (`config_key`, `config_value`, `value_type`, `description`) VALUES
('event.dedupe_window_seconds',  '600', 'int', '事件回传去重窗口（秒）'),
('audit.lock_ttl_seconds',        '300','int', '审核工作台软锁 TTL'),
('audit.undo_window_seconds',     '30', 'int', 'Undo 撤销窗口 TTL'),
('report.cache_ttl_seconds',      '60', 'int', '看板缓存 TTL'),
('account.import_max_rows',       '500','int', '批量导入单次最大行数');
```

#### 4.11.4 `requirements.txt`

```
python-jose[cryptography]>=3.3
passlib[bcrypt]>=1.7
openpyxl>=3.1
```

#### 4.11.5 `main.py` 注册

```python
from app.api.admin import router as admin_router
from app.api.events import router as events_router
from app.api.webhook import router as webhook_router

app.include_router(webhook_router)
app.include_router(admin_router)
app.include_router(events_router)
```

`api/admin/__init__.py`：

```python
from fastapi import APIRouter
from . import auth, audit, accounts, jobs, resumes, dicts, config, reports, logs

router = APIRouter()
router.include_router(auth.router)
router.include_router(audit.router)
router.include_router(accounts.router)
router.include_router(jobs.router)
router.include_router(resumes.router)
router.include_router(dicts.router)
router.include_router(config.router)
router.include_router(reports.router)
router.include_router(logs.router)
```

## 5. 开发顺序建议

建议按以下顺序开发和联调：

1. **基础设施**（模块 A + L）：security / deps / responses / 异常处理器 → 跑通最简单端点
2. **schema.sql + models 扩展**（模块 K）：先建 `event_log` 表 + audit_action 枚举扩展，确保 ORM 加载无误
3. **登录与改密**（模块 B）：依赖前两步，形成最小可用闭环
4. **审核工作台**（模块 C）：核心模块，建议优先；含软锁、乐观锁、Undo
5. **账号管理**（模块 D）：含 Excel 导入
6. **岗位 / 简历管理**（模块 E）
7. **字典管理**（模块 F）
8. **系统配置**（模块 G）：注意配置缓存清理与 Phase 4 限流端到端验证
9. **数据看板**（模块 H）
10. **对话日志**（模块 I）
11. **事件回传 API**（模块 J）
12. **依赖补充与 Swagger 验收**：Swagger UI `/docs` 中通查接口

## 6. 测试辅助

- 使用 `pytest + httpx.AsyncClient` 写 API 层集成测试，复用 Phase 3 已有 `conftest.py`
- 鉴权场景可写 fixture：`admin_token` / `event_api_key_header`
- Excel 导入测试用 `openpyxl` 在内存生成 xlsx 文件
- 审核工作台并发测试：`threading` 模拟两个管理员同时 lock；乐观锁用预先修改 `version` 模拟
- Undo 测试通过 monkey patch `time.time` 模拟过期

## 7. 注意事项

1. **不要让 admin 接口直接读 ORM 字段返回前端**：必须经 schema DTO，避免泄漏内部字段
2. **不要省略 `operator`**：所有写操作必须带 `current.username` 进入 service
3. **不要直接修改密码哈希**：必须经过 `hash_password()`
4. **不要在 service 里返回 dict 然后期望 FastAPI 自动序列化日期**：`datetime` 必须在 schema 层 `model_dump(mode="json")` 或 `default=str`
5. **不要把 `external_userid` 当成可信输入**：所有写操作前先存在性校验
6. **不要让 Excel 导入吞掉异常**：失败必须返回行号
7. **不要忽略危险配置项的 audit_log**：所有 filter.* / llm.provider 变更必须留痕
8. **不要直接在 admin 端修改岗位 audit_status，绕过审核 service**：审核动作必须通过审核工作台接口走
9. **不要在事件回传里阻塞业务**：`event_log` 写库失败必须降级
10. **不要忘记在 main.py 注册全局异常处理器**：否则错误码会从 422/500 漏出去
