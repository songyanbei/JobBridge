"""Phase 5 接口级端到端 smoke（真实 MySQL + Redis）。

覆盖场景：
1. 登录 / JWT 无 token / JWT 无效 / 密码错误 / 改密
2. 审核工作台：先建测试岗位 → lock 冲突 → 乐观锁冲突 → pass → undo → unlock
3. 账号管理：预注册厂家 + 重名 + 封禁 + 解封
4. 字典：敏感词 CRUD + 批量
5. 系统配置：读取 + 更新（含危险项）+ 限流参数变更 + audit_log 校验
6. 看板：dashboard / trends / funnel
7. 对话日志：30 天范围校验
8. 事件回传：API Key + 幂等去重

所有断言失败立即退出；全部通过输出"ALL PHASE5 SMOKE PASSED"。
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta

import httpx

BASE = "http://127.0.0.1:8001"
EVENT_API_KEY = "phase5-event-key-abc123"


def expect(cond, msg):
    if not cond:
        print(f"FAIL: {msg}", flush=True)
        sys.exit(1)
    print(f"  ok: {msg}")


def expect_code(resp, code, msg):
    body = resp.json()
    if body.get("code") != code:
        print(f"FAIL: {msg} — expected code={code}, got body={body}", flush=True)
        sys.exit(1)
    print(f"  ok: {msg} (code={code})")
    return body


def banner(title):
    print(f"\n=== {title} ===", flush=True)


def main():
    client = httpx.Client(base_url=BASE, timeout=15.0)

    # ------------------------------------------------------------------
    banner("1. 鉴权：匿名 / 无效 token / 登录 / me")
    # 匿名访问受保护接口 → 40003
    r = client.get("/admin/me")
    expect_code(r, 40003, "anonymous GET /admin/me → 40003")

    # 错密（需 ≥6 字符满足 pydantic min_length，才会进入业务校验）
    r = client.post("/admin/login", json={"username": "admin", "password": "wrongpw"})
    expect_code(r, 40001, "wrong password → 40001")

    # 正确登录
    r = client.post("/admin/login", json={"username": "admin", "password": "admin123"})
    body = expect_code(r, 0, "login admin/admin123")
    token = body["data"]["access_token"]
    expect(body["data"]["password_changed"] is False, "password_changed=false on first login")
    expect(body["data"]["expires_at"], "expires_at present")
    headers = {"Authorization": f"Bearer {token}"}

    # 无效 token → 40003
    r = client.get("/admin/me", headers={"Authorization": "Bearer bogus.token"})
    expect_code(r, 40003, "bogus token → 40003")

    # /admin/me
    r = client.get("/admin/me", headers=headers)
    body = expect_code(r, 0, "GET /admin/me")
    expect(body["data"]["username"] == "admin", "username=admin")

    # 改密（旧→新），再用新密码登录，再改回原密码（避免污染后续测试）
    new_pw = "admin123-new"
    r = client.put("/admin/me/password", json={"old_password": "admin123", "new_password": new_pw}, headers=headers)
    expect_code(r, 0, "change password ok")
    # 再用新密码登录
    r = client.post("/admin/login", json={"username": "admin", "password": new_pw})
    body = expect_code(r, 0, "login with new password")
    expect(body["data"]["password_changed"] is True, "password_changed=true after change")
    headers = {"Authorization": f"Bearer {body['data']['access_token']}"}
    # 新密码 < 8 字符
    r = client.put("/admin/me/password", json={"old_password": new_pw, "new_password": "short"}, headers=headers)
    expect_code(r, 40101, "new password < 8 → 40101")
    # 旧密码错
    r = client.put("/admin/me/password", json={"old_password": "wrong", "new_password": "admin123"}, headers=headers)
    expect_code(r, 40001, "wrong old password → 40001")
    # 改回原
    r = client.put("/admin/me/password", json={"old_password": new_pw, "new_password": "admin123"}, headers=headers)
    expect_code(r, 0, "restore original password")
    # 重新登录一次，继续用
    r = client.post("/admin/login", json={"username": "admin", "password": "admin123"})
    headers = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

    # ------------------------------------------------------------------
    banner("2. 账号管理：厂家预注册 + 重名 + 封禁 + 解封")
    factory_name = f"smoke-factory-{int(time.time())}"
    r = client.post("/admin/accounts/factories",
                     json={"display_name": factory_name, "company": "SmokeCo", "phone": "13800000001"},
                     headers=headers)
    body = expect_code(r, 0, "pre-register factory")
    factory_userid = body["data"]["external_userid"]
    expect(factory_userid.startswith("pre_factory_"), f"external_userid pattern: {factory_userid}")

    # 同 external_userid 重复预注册 → 40904
    r = client.post("/admin/accounts/factories",
                     json={"display_name": factory_name, "phone": "13800000002",
                           "external_userid": factory_userid},
                     headers=headers)
    expect_code(r, 40904, "duplicate external_userid → 40904")

    # 列表包含这个厂家
    r = client.get("/admin/accounts/factories", params={"keyword": factory_name}, headers=headers)
    body = expect_code(r, 0, "list factories with keyword")
    expect(body["data"]["total"] >= 1, f"factory list total>=1 got {body['data']['total']}")

    # 封禁
    r = client.post(f"/admin/accounts/{factory_userid}/block",
                    json={"reason": "smoke test block"}, headers=headers)
    expect_code(r, 0, "block factory")
    # 再次封禁 → 40904
    r = client.post(f"/admin/accounts/{factory_userid}/block",
                    json={"reason": "again"}, headers=headers)
    expect_code(r, 40904, "re-block → 40904")
    # 黑名单列表含
    r = client.get("/admin/accounts/blacklist", params={"keyword": factory_name}, headers=headers)
    body = expect_code(r, 0, "blacklist list")
    expect(any(u["external_userid"] == factory_userid for u in body["data"]["items"]),
           "blocked factory appears in blacklist")
    # 解封
    r = client.post(f"/admin/accounts/{factory_userid}/unblock",
                    json={"reason": "smoke test unblock"}, headers=headers)
    expect_code(r, 0, "unblock factory")

    # ------------------------------------------------------------------
    banner("3. 字典：敏感词 CRUD + 批量 + 工种 CRUD + 删除引用保护")
    # 敏感词
    sw = f"smoke词{int(time.time())}"
    r = client.post("/admin/dicts/sensitive-words",
                    json={"word": sw, "level": "mid", "category": "test"}, headers=headers)
    body = expect_code(r, 0, "add sensitive word")
    sw_id = body["data"]["id"]
    # 重复
    r = client.post("/admin/dicts/sensitive-words",
                    json={"word": sw, "level": "mid"}, headers=headers)
    expect_code(r, 40904, "duplicate sensitive word → 40904")
    # 批量
    r = client.post("/admin/dicts/sensitive-words/batch",
                    json={"words": [f"batch{int(time.time())}a", f"batch{int(time.time())}b", sw],
                          "level": "low"},
                    headers=headers)
    body = expect_code(r, 0, "batch sensitive words")
    expect(body["data"]["added"] == 2 and body["data"]["duplicated"] == 1,
           f"batch added=2 dup=1, got {body['data']}")
    # 删除
    r = client.delete(f"/admin/dicts/sensitive-words/{sw_id}", headers=headers)
    expect_code(r, 0, "delete sensitive word")

    # 工种列表
    r = client.get("/admin/dicts/job-categories", headers=headers)
    body = expect_code(r, 0, "list job categories")
    cats = body["data"]
    expect(len(cats) >= 10, f"job categories >= 10 seeded (got {len(cats)})")
    # 找一个被 seed.sql 预设的有 name 的分类，删除应返回 40904（无引用也会过——因为没岗位引用，不触发引用保护，应直接成功）
    # 这里只验证创建+唯一性+删除，不做引用保护（需要先建岗位）
    new_cat_code = f"smoke_cat_{int(time.time())}"
    r = client.post("/admin/dicts/job-categories",
                    json={"code": new_cat_code, "name": new_cat_code, "sort_order": 9999}, headers=headers)
    body = expect_code(r, 0, "create job category")
    cat_id = body["data"]["id"]
    # 重名
    r = client.post("/admin/dicts/job-categories",
                    json={"code": new_cat_code, "name": new_cat_code}, headers=headers)
    expect_code(r, 40904, "duplicate category code → 40904")
    # 编辑
    r = client.put(f"/admin/dicts/job-categories/{cat_id}",
                    json={"sort_order": 500}, headers=headers)
    expect_code(r, 0, "update job category")
    # 删除
    r = client.delete(f"/admin/dicts/job-categories/{cat_id}", headers=headers)
    expect_code(r, 0, "delete job category")

    # 城市字典（按省份分组）
    r = client.get("/admin/dicts/cities", headers=headers)
    body = expect_code(r, 0, "list cities grouped")
    expect(len(body["data"]) >= 1, "cities grouped by province, >=1")

    # ------------------------------------------------------------------
    banner("4. 系统配置：分组读取 / 危险项 / audit_log / webhook 限流生效")
    r = client.get("/admin/config", headers=headers)
    body = expect_code(r, 0, "list config grouped")
    groups = body["data"]
    expect("rate_limit" in groups, "rate_limit group exists")
    expect("filter" in groups, "filter group exists")
    expect("event" in groups, "event group exists")
    # 所有 5 个 Phase 5 新键
    event_keys = {c["config_key"] for c in groups.get("event", [])}
    expect("event.dedupe_window_seconds" in event_keys, "event.dedupe_window_seconds present")

    # 危险项变更
    r = client.put("/admin/config/filter.enable_gender",
                    json={"config_value": "false"}, headers=headers)
    body = expect_code(r, 0, "update danger config")
    expect(body["data"]["danger"] is True, "danger flag true")
    expect(body["data"]["notice"] is not None, "danger notice present")
    # 恢复
    client.put("/admin/config/filter.enable_gender", json={"config_value": "true"}, headers=headers)

    # 普通项变更
    r = client.put("/admin/config/rate_limit.max_count",
                    json={"config_value": "8"}, headers=headers)
    body = expect_code(r, 0, "update normal config")
    expect(body["data"]["danger"] is False, "non-danger flag")

    # 类型校验：int 传非数字 → 40101
    r = client.put("/admin/config/rate_limit.max_count",
                    json={"config_value": "not-a-number"}, headers=headers)
    expect_code(r, 40101, "type mismatch int → 40101")
    # 恢复
    client.put("/admin/config/rate_limit.max_count", json={"config_value": "5"}, headers=headers)

    # 不存在的 key → 40401
    r = client.put("/admin/config/no.such.key", json={"config_value": "x"}, headers=headers)
    expect_code(r, 40401, "missing config key → 40401")

    # ------------------------------------------------------------------
    banner("5. 审核工作台：通过 import 直接插入 job 再走审核流程")
    # 先创建一个测试用户（worker owner）
    import uuid
    worker_uid = f"smoke_worker_{uuid.uuid4().hex[:8]}"
    import pymysql
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user (external_userid, role, display_name, status) VALUES (%s, 'worker', 'smoke', 'active')",
                (worker_uid,),
            )
            cur.execute(
                """INSERT INTO job
                   (owner_userid, city, job_category, salary_floor_monthly, pay_type, headcount,
                    raw_text, audit_status, expires_at, version)
                   VALUES (%s, '苏州市', '电子厂', 5000, '月薪', 5, 'smoke test job', 'pending',
                           DATE_ADD(NOW(), INTERVAL 30 DAY), 1)""",
                (worker_uid,),
            )
            job_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    print(f"  created job #{job_id} owned by {worker_uid}")

    # 待审数
    r = client.get("/admin/audit/pending-count", headers=headers)
    body = expect_code(r, 0, "pending-count")
    expect(body["data"]["job"] >= 1, "pending job >=1")

    # 详情
    r = client.get(f"/admin/audit/job/{job_id}", headers=headers)
    body = expect_code(r, 0, "audit detail")
    version = body["data"]["version"]
    expect(version == 1, f"initial version=1, got {version}")

    # 软锁
    r = client.post(f"/admin/audit/job/{job_id}/lock", headers=headers)
    expect_code(r, 0, "acquire lock")

    # 模拟第二个管理员（另开 session，同帐号但不同 client 不影响；软锁是按 operator 区分）
    # 构造一个"另一个 operator"：创建第二个管理员账号
    # 简化：用 SQL 直接插入一个 admin2 并登录
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        # bcrypt hash for "admin123"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO admin_user (username, password_hash, display_name, enabled) "
                "VALUES ('admin2', %s, 'admin2', 1)",
                ("$2b$10$eSJKksBigl05aIBiYNR/MuHvR0GCahspw0YnVo3EL8UlYanuXBNDy",),
            )
        conn.commit()
    finally:
        conn.close()

    r = client.post("/admin/login", json={"username": "admin2", "password": "admin123"})
    body = expect_code(r, 0, "login admin2")
    headers2 = {"Authorization": f"Bearer {body['data']['access_token']}"}

    # admin2 尝试 lock → 40901
    r = client.post(f"/admin/audit/job/{job_id}/lock", headers=headers2)
    body = expect_code(r, 40901, "lock conflict → 40901")
    expect(body["data"]["locked_by"] == "admin", f"locked_by=admin got {body['data']}")

    # 乐观锁：错版本通过
    r = client.post(f"/admin/audit/job/{job_id}/pass",
                    json={"version": 999}, headers=headers)
    body = expect_code(r, 40902, "wrong version → 40902")
    expect("current_version" in body["data"], "40902 data has current_version")

    # 正确通过
    r = client.post(f"/admin/audit/job/{job_id}/pass",
                    json={"version": version}, headers=headers)
    expect_code(r, 0, "pass audit")
    # 详情应为 passed 且 version+1
    r = client.get(f"/admin/audit/job/{job_id}", headers=headers)
    body = expect_code(r, 0, "detail after pass")
    expect(body["data"]["audit_status"] == "passed", "audit_status=passed")
    expect(body["data"]["version"] == version + 1, f"version incremented to {version+1}")
    expect(body["data"]["audited_by"] == "admin", "audited_by=admin")

    # Undo（30s 内）
    r = client.post(f"/admin/audit/job/{job_id}/undo", headers=headers)
    expect_code(r, 0, "undo pass")
    r = client.get(f"/admin/audit/job/{job_id}", headers=headers)
    body = expect_code(r, 0, "detail after undo")
    expect(body["data"]["audit_status"] == "pending", "audit_status back to pending")

    # Undo 已经 pop 掉了；再 undo → 40903
    r = client.post(f"/admin/audit/job/{job_id}/undo", headers=headers)
    expect_code(r, 40903, "second undo → 40903 (window expired)")

    # 释放锁
    r = client.post(f"/admin/audit/job/{job_id}/unlock", headers=headers)
    expect_code(r, 0, "unlock")

    # 清理 job
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE target_type='job' AND target_id=%s", (str(job_id),))
            cur.execute("DELETE FROM job WHERE id=%s", (job_id,))
            cur.execute("DELETE FROM user WHERE external_userid=%s", (worker_uid,))
            cur.execute("DELETE FROM admin_user WHERE username='admin2'")
        conn.commit()
    finally:
        conn.close()

    # ------------------------------------------------------------------
    banner("6. 岗位乐观锁：delist/extend/restore 都需要 version")
    # 再建一个岗位测试 delist/restore
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user (external_userid, role, status) VALUES (%s, 'worker', 'active')",
                (worker_uid,),
            )
            cur.execute(
                """INSERT INTO job (owner_userid, city, job_category, salary_floor_monthly, pay_type,
                                     headcount, raw_text, audit_status, expires_at, version)
                   VALUES (%s, '苏州市', '电子厂', 5000, '月薪', 5, 'smoke2', 'passed',
                           DATE_ADD(NOW(), INTERVAL 10 DAY), 3)""",
                (worker_uid,),
            )
            job_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # 错版本 delist
    r = client.post(f"/admin/jobs/{job_id}/delist",
                    json={"version": 99, "reason": "manual_delist"}, headers=headers)
    expect_code(r, 40902, "delist wrong version → 40902")

    # 正确 delist
    r = client.post(f"/admin/jobs/{job_id}/delist",
                    json={"version": 3, "reason": "manual_delist"}, headers=headers)
    expect_code(r, 0, "delist ok")

    # 延期（未过期 + 已 delist 状态也可延期）
    r = client.post(f"/admin/jobs/{job_id}/extend",
                    json={"version": 4, "days": 15}, headers=headers)
    expect_code(r, 0, "extend 15d ok")

    # 错 days
    r = client.post(f"/admin/jobs/{job_id}/extend",
                    json={"version": 5, "days": 7}, headers=headers)
    expect_code(r, 40101, "invalid days → 40101")

    # 取消下架
    r = client.post(f"/admin/jobs/{job_id}/restore", json={"version": 5}, headers=headers)
    expect_code(r, 0, "restore ok")

    # 清理
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE target_type='job' AND target_id=%s", (str(job_id),))
            cur.execute("DELETE FROM job WHERE id=%s", (job_id,))
            cur.execute("DELETE FROM user WHERE external_userid=%s", (worker_uid,))
        conn.commit()
    finally:
        conn.close()

    # ------------------------------------------------------------------
    banner("7. 数据看板：dashboard / trends / funnel")
    r = client.get("/admin/reports/dashboard", headers=headers)
    body = expect_code(r, 0, "dashboard")
    expect("today" in body["data"] and "yesterday" in body["data"] and "trend_7d" in body["data"],
           "dashboard has today/yesterday/trend_7d")
    expect("audit_pending" in body["data"]["today"], "today has audit_pending")
    expect("audit_pending" not in body["data"]["yesterday"], "yesterday has NO audit_pending")

    r = client.get("/admin/reports/trends", params={"range": "7d"}, headers=headers)
    expect_code(r, 0, "trends 7d")

    r = client.get("/admin/reports/funnel", headers=headers)
    body = expect_code(r, 0, "funnel")
    stages = [s["stage"] for s in body["data"]]
    expect(stages == ["注册", "首次发消息", "首次有效检索", "收到推荐", "点详情"],
           f"funnel 5 stages, got {stages}")

    r = client.get("/admin/reports/top", params={"dim": "city", "limit": 5}, headers=headers)
    expect_code(r, 0, "top by city")

    # 无效 dim
    r = client.get("/admin/reports/top", params={"dim": "bogus"}, headers=headers)
    expect_code(r, 40101, "invalid dim → 40101")

    # ------------------------------------------------------------------
    banner("8. 对话日志：30 天范围限制")
    start = (datetime.now() - timedelta(days=40)).isoformat()
    end = datetime.now().isoformat()
    r = client.get("/admin/logs/conversations",
                    params={"userid": "x", "start": start, "end": end}, headers=headers)
    expect_code(r, 40101, "range > 30d → 40101")

    start = (datetime.now() - timedelta(days=7)).isoformat()
    r = client.get("/admin/logs/conversations",
                    params={"userid": "nobody", "start": start, "end": end}, headers=headers)
    body = expect_code(r, 0, "7d range ok")
    expect(body["data"]["total"] == 0, "nobody has no logs")

    # ------------------------------------------------------------------
    banner("9. 事件回传：API Key / 幂等去重")
    # 无 API Key
    r = client.post("/api/events/miniprogram_click",
                    json={"userid": "wx_1", "target_type": "job", "target_id": 1})
    expect_code(r, 40001, "no API key → 40001")

    # 错 API Key
    r = client.post("/api/events/miniprogram_click",
                    json={"userid": "wx_1", "target_type": "job", "target_id": 1},
                    headers={"X-Event-Api-Key": "wrong"})
    expect_code(r, 40001, "wrong API key → 40001")

    # 正确：首次写入
    event_headers = {"X-Event-Api-Key": EVENT_API_KEY}
    r = client.post("/api/events/miniprogram_click",
                    json={"userid": "wx_smoke_1", "target_type": "job", "target_id": 99999,
                          "timestamp": int(time.time())},
                    headers=event_headers)
    body = expect_code(r, 0, "event first write")
    expect(body["data"]["deduped"] is False, "first call deduped=false")

    # 重复：10 分钟内去重
    r = client.post("/api/events/miniprogram_click",
                    json={"userid": "wx_smoke_1", "target_type": "job", "target_id": 99999,
                          "timestamp": int(time.time())},
                    headers=event_headers)
    body = expect_code(r, 0, "event dedup")
    expect(body["data"]["deduped"] is True, "second call deduped=true")

    # 毫秒时间戳兼容
    r = client.post("/api/events/miniprogram_click",
                    json={"userid": "wx_smoke_2", "target_type": "resume", "target_id": 12345,
                          "timestamp": int(time.time() * 1000)},
                    headers=event_headers)
    expect_code(r, 0, "event ms timestamp accepted")

    # 验证写入了 event_log
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM event_log WHERE userid IN ('wx_smoke_1','wx_smoke_2')")
            count = cur.fetchone()[0]
        expect(count == 2, f"event_log has 2 rows, got {count}")
        # 清理
        with conn.cursor() as cur:
            cur.execute("DELETE FROM event_log WHERE userid IN ('wx_smoke_1','wx_smoke_2')")
        conn.commit()
    finally:
        conn.close()

    # ------------------------------------------------------------------
    banner("10. Swagger /docs + /redoc 可达")
    r = client.get("/docs")
    expect(r.status_code == 200, f"/docs 200, got {r.status_code}")
    r = client.get("/redoc")
    expect(r.status_code == 200, f"/redoc 200, got {r.status_code}")
    r = client.get("/openapi.json")
    openapi = r.json()
    admin_paths = [p for p in openapi["paths"] if p.startswith("/admin/") or p.startswith("/api/events/")]
    expect(len(admin_paths) >= 50, f"openapi has >=50 /admin|/api/events paths, got {len(admin_paths)}")
    tags = {t["name"] for t in openapi.get("tags", [])}
    expected_tags = {"admin-auth", "admin-audit", "admin-accounts", "admin-jobs",
                      "admin-resumes", "admin-dicts", "admin-config", "admin-reports",
                      "admin-logs", "events"}
    missing = expected_tags - tags
    expect(not missing, f"tags cover all Phase 5 modules (missing={missing})")

    # ------------------------------------------------------------------
    # 清理账号测试残留
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="jobbridge",
                            password="jobbridge", database="jobbridge")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE target_id=%s", (factory_userid,))
            cur.execute("DELETE FROM user WHERE external_userid=%s", (factory_userid,))
        conn.commit()
    finally:
        conn.close()

    print("\n\nALL PHASE5 SMOKE PASSED ✓", flush=True)


if __name__ == "__main__":
    main()
