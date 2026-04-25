# Mock 企业微信测试台（Mock WeCom Testbed）

> **⚠️ 仅用于演示 / 开发联调，禁止用于生产环境。演示结束后整包删除。**

## 定位

本目录是一个**单仓库内的独立沙箱**，因企业专属域名 ICP 备案未到位、无法申请企业微信回调域而建。

- 提供一个"模拟企业微信"的外壳 UI（求职者 + 招聘者双视角 + SSE 流式回复）
- **下游的 MySQL / Redis / Worker / message_router / LLM 全部走主后端真实代码链路**
- 即模拟"消息进/出"两头，中间 100% 真实

## 和主项目的关系

| 项 | 解耦情况 |
|---|---|
| `mock-testbed/frontend/` | **100% 独立** —— 独立 Vite 子项目、独立 `node_modules`、独立端口 5174 |
| `mock-testbed/backend/` | **99% 独立** —— 独立 FastAPI 服务、独立 venv、独立端口 8001 |
| 主仓库 `frontend/` | **0 改动** |
| 主仓库 `backend/app/wecom/client.py` | **唯一切面** —— 2 段 `[MOCK-WEWORK] BEGIN/END` 标记的 env-based `if` 分支 |
| 主仓库其他文件（`main.py / config.py / api/ / services/ / models.py / ...`） | **0 改动** |

## 启停

### 前置条件

- 主后端运行中（本地或 docker compose），MySQL/Redis 可连
- 主后端启动前必须 `export MOCK_WEWORK_OUTBOUND=true`（否则出站拦截不生效，演示看不到 bot 回复）
- 可选：`export MOCK_WEWORK_REDIS_URL=redis://localhost:6379/0`（默认值就是这个）
- 主后端若设了 `APP_ENV=production`，`[MOCK-WEWORK]` 分支会直接 `RuntimeError` 拒绝启动（防误启兜底）

**网络绑定**：沙箱后端默认只绑 `127.0.0.1:8001`（仅本机）。需要给同网段设备演示时，
在启动前 `export MOCK_HOST=0.0.0.0`。⚠️ 绑 `0.0.0.0` 等于把 `/mock/wework/inbound`
暴露给整条网络，注意别在不可信网络开启。

### 一键启动

```bash
cd mock-testbed

# 首次运行前灌 seed 数据
./scripts/seed.sh

# 启动前后端（后端 8001 + 前端 5174）
./scripts/start.sh

# 冒烟验证
./scripts/smoke.sh

# 浏览器打开
# http://localhost:5174
```

### 停止

```bash
./scripts/stop.sh
# 记得 unset MOCK_WEWORK_OUTBOUND（主后端）
```

## 演示流程（典型）

1. 启动主后端（带 `MOCK_WEWORK_OUTBOUND=true`）
2. 启动本沙箱：`cd mock-testbed && ./scripts/start.sh`
3. 浏览器打开 `http://localhost:5174` → 双栏布局 + 红色横幅
4. **招聘者视角（左栏）**：选 `wm_mock_factory_001`，发消息"发布岗位 深圳 打包工 5000"
5. **求职者视角（右栏）**：选 `wm_mock_worker_001`，发消息"我想找深圳打包工"
6. 观察：3 秒内 SSE 气泡弹出 bot 回复（基于主后端真实意图识别 + 检索）

### 多窗口演示（C 模式）

不想用双栏，想模拟不同人在不同设备上：

```
http://localhost:5174/single?external_userid=wm_mock_worker_001&role=worker
http://localhost:5174/single?external_userid=wm_mock_factory_001&role=factory
```

开 N 个浏览器窗口各扮演一个角色，支持书签、分享链接。

## 字段契约

本沙箱的所有接口字段名、结构、大小写**严格对齐企业微信官方契约**（未来接入真企微时主后端业务代码 0 改动）：

- 身份：`external_userid`（`wm_` 前缀，本沙箱用 `wm_mock_` 子前缀）
- 入站消息字段：`ToUserName / FromUserName / CreateTime / MsgType / Content / MsgId / AgentID`（对应企微 XML 标签）
- 出站消息字段：`touser / msgtype / agentid / text.content`（对应 `/cgi-bin/message/send` 请求体）
- 返回体顶层恒有 `errcode / errmsg`

## 目录结构

```
mock-testbed/
├── README.md              # 本文件
├── .gitignore
├── backend/               # 独立 FastAPI 服务（端口 8001）
│   ├── main.py            # uvicorn 入口
│   ├── routes.py          # 5 个 /mock/wework/* 路由
│   ├── outbound_bus.py    # Redis pub/sub 封装
│   ├── models.py          # 轻量 SQLAlchemy 模型（只读 user 表 3 个字段）
│   ├── db.py              # 引擎 + SessionLocal
│   ├── config.py          # 本沙箱配置（Pydantic BaseSettings）
│   ├── requirements.txt   # 独立依赖
│   ├── .env.example
│   └── tests/             # 单测 + 契约测试
├── frontend/              # 独立 Vite 子项目（端口 5174）
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.js / App.vue / router.js / api.js
│       ├── views/ (MockEntryView / MockSplitView / MockSingleView)
│       └── components/ (MockBanner / MockIdentityPicker / MockChatPanel)
├── sql/
│   └── seed_mock_users.sql  # 幂等注入 4 个 wm_mock_* 用户
├── scripts/
│   ├── start.sh   stop.sh   seed.sh   smoke.sh
└── logs/          # 运行时日志 + pid 文件（git 忽略）
```

## 删除指南（接入真企微时执行）

```bash
# 1. 停沙箱（若在运行）
cd mock-testbed && ./scripts/stop.sh

# 2. 主后端取消环境变量
unset MOCK_WEWORK_OUTBOUND
unset MOCK_WEWORK_REDIS_URL

# 3. 主后端删切面（唯一改动点）
grep -rn "MOCK-WEWORK" backend/app/
# 预期只命中 backend/app/wecom/client.py 的 2 处 [MOCK-WEWORK] BEGIN/END 标记块
# 手工删除这 2 段 BEGIN/END 之间的内容（含 BEGIN/END 注释本身）

# 4. 删 mock 相关测试文件
rm backend/tests/unit/test_wecom_mock_outbound.py

# 5. 整包删除沙箱
cd .. && rm -rf mock-testbed/

# 6. 验证
pytest backend/tests/       # 应全绿
git diff --stat              # 应只显示上述 3 处删除

# 7. 可选：清理 mock 测试用户
mysql ... -e "DELETE FROM user WHERE external_userid LIKE 'wm_mock_%';"

# 8. commit 清理
git commit -am "chore: remove mock-wework testbed (real WeCom integrated)"
```

**总计 7 步命令 + 手工删 2 段带 `[MOCK-WEWORK]` 标记的 if 块**。主前端 `frontend/` 一行不碰，主后端只剩 1 个文件（`wecom/client.py`）的改动。

## 已知限制 / 非目标

- ❌ 不模拟交互卡片（`template_card`）、审批流（`sys_approval_change`）、通讯录同步
- ❌ 不模拟富媒体（image/voice/file/video 的 `media_id`）
- ❌ 不模拟消息加解密（AES-CBC / SHA1 验签）—— 沙箱明文传输
- ❌ 不模拟 JS-SDK `wx.config / wx.agentConfig`
- ❌ SSE 延迟不作为真企微 P95 性能参考（Redis pub/sub ≠ HTTPS 外网）
- ❌ 不进 docker-compose 生产编排（沙箱目标就是独立启停、不进生产）

## 相关文档

- `collaboration/features/phase7-mock-wework-testbed.md` 主设计文档
- `collaboration/features/phase7-mock-wework-testbed-dev-implementation.md` 开发实施
- `collaboration/features/phase7-mock-wework-testbed-test-implementation.md` 测试设计
- 企业微信官方接口文档：https://developer.work.weixin.qq.com/document/
