# Task: 自建企微 Demo / 联调环境建设

> 状态：`deferred`（延后到 Phase 7 末或客户交付前）
> 创建日期：2026-04-18
> 最近更新：2026-04-18
> 任务性质：**独立于 Phase 4 正式验收的并行任务**
> 关联文档：
> - `collaboration/features/phase4-main.md`（Phase 4 主链路需求）
> - `方案设计_v0.1.md` §12（企微接入）、§17.2（指令清单）、§17.3（外部依赖确认单）
> - `docs/implementation-plan.md` §4.5

---

## 🚧 当前状态速读（2026-04-18 更新）

**本任务已暂缓，等待以下条件之一：**

1. **客户方提供企微企业 + 已备案域名 + 回调 URL 权限** → 直接切到客户方企微对接（代码 0~3 小时改动）
2. **或** Phase 7 末期内部自建环境 → 需要自备**已备案的自有域名**

### 暂缓原因

2026-04-18 尝试自建 Demo 企微时，企微后台要求 **回调 URL 的主域名必须已完成 ICP 备案，且备案主体不能是第三方服务商（cpolar / ngrok / natapp 等一律被拒）**。

当时方案评估结论：
- 自购域名 + 备案 20~30 天 → 时间来得及但产生额外成本
- Demo 演示可以**全程用 Mock**，不影响业务逻辑展示
- 真机联调推迟到客户交付前的 Onboarding 阶段由客户配合

### 已完成的脚手架（保留待用）

| 产物 | 路径 | 定位 |
|---|---|---|
| `.env` 企微 5 项配置 | `backend/.env` `WECOM_*` | 自建测试企业的配置，**有效可用**，切换客户环境时改 5 行 |
| Demo 数据 seed 模板 | `backend/sql/seed_demo.sql.template` | 占位符形式，userid 就绪后 `sed` 替换 |
| 启停脚本 | `scripts/demo_env_start.sh` / `_stop.sh` / `_seed.sh` / `_cleanup.sh` | 未真机前不使用 |
| Phase 5 假数据 | `scripts/seed_dev_data.sh` | **已可用** — 为 Phase 5/6 开发期间的列表/筛选/分页提供数据 |

### 目前 Phase 5/6 开发如何应对"没有真机数据"

使用 `scripts/seed_dev_data.sh` 造的假数据：
- 8 个 user（factory/broker/worker 各种状态）
- 20 个 job（pending / passed / rejected / filled / manual_delist 全状态）
- 8 个 resume
- 30 条 conversation_log（in/out direction，wecom_msg_id 规范）
- 15 条 wecom_inbound_event（received/processing/done/failed/dead_letter 全状态）
- 10 条 audit_log

清理：`bash scripts/seed_dev_data.sh clean`

### Phase 7 末重新启用本文档时

参考原有 §5 实施 Checklist 执行，但 **§5.2 公网回调通道** 章节需要改为"使用已备案自有域名方案"（原 cpolar 共享域名方案**不可用**）。

另需参考新文档（待创建）：`docs/wecom-onboarding-checklist.md` — 交付时给客户的企微接入指引。

---

---

## 1. 任务定位与决策背景

### 1.1 为什么这是独立任务

Phase 4 正式验收标准（`phase4-main.md` §7、§8）明确允许"Mock 出站降级验收"：

> 如无法确认客户企微认证级别，Phase 4 代码按方案 A 实现，但 E2E 验收降级为 Mock 出站；
> 企微真实联调推迟到权限就绪后。

本任务**不纳入 Phase 4 验收**，原因：
- 若纳入，则 Phase 4 受"客户企微认证级别"这一外部依赖制约（方案 §17.3 `待确认`）
- 本任务独立推进可避免互相阻塞
- 产出物服务于 **Phase 5/6/7 全生命周期的真机联调**，不只是一次性 Demo

### 1.2 为什么要现在做，而不是等所有阶段完成后一起做

| 项 | 现在做（Phase 4 收尾并行） | 等所有阶段完成后一起做 |
|---|---|---|
| 初始投入 | 1 人日 | 0（推迟）|
| 维护成本 | ~10 元/月（固定域名）+ 5 分钟/月 | 0 |
| Bug 修复期望 | 低（Phase 4 代码在"热状态"中修） | 中~高（跨 3 个 phase 修，牵动其他模块冻结边界）|
| Phase 5/6 开发期"活体数据流" | ✅ 有 | ❌ 没有 |
| Phase 7 最终联调工期 | ~1 人日（复用已有环境） | 3~5 人日（首次联调 + 级联 bug 排查）|
| **TCO** | **~2 人日 + 50 元** | **~4~6 人日** |

核心依据：**Phase 4 是企微链路的代码所在地**。真机 bug（中文/emoji 编码、AES 填充、时间戳时差、5 秒重试、URL 校验编码）99% 出在 Phase 4 代码里，越早在真机暴露越好。

### 1.3 与客户企微的关系

- 本环境使用**自注册企微 + 自建应用 API**（`cgi-bin/message/send`）
- 不依赖客户的"客户联系"API 权限（`externalcontact/message/send`）
- 当前 `WeComClient.send_text()` 实现已经对应自建应用路径，代码无需改动
- 未来切换到客户企微的成本见 §9

---

## 2. 任务目标

产出一套**持续可用**的 Demo / 联调环境，满足：

1. 企微回调回 webhook 链路打通（验签、解密、URL 校验均在真机走通）
2. 三类角色（工人 / 厂家 / 中介）均可通过企微 App 真实操作
3. Phase 4 的 10 条命令、5.x 节 E2E 场景均可现场演示
4. 大屏同步展示 `inbound_event` 状态、Redis 队列长度、Worker 日志
5. 客户 Demo 当天演示者可在 30 分钟内完整展示 Phase 4 能力

---

## 3. 范围

### 3.1 本任务必须完成

- 自注册企业微信（不付费认证）
- 创建自建应用"招工助手"，拿到 CorpID / AgentID / Secret / Token / EncodingAESKey
- 公网 HTTPS 回调通道（cpolar 付费版 / frp + 轻量 VPS / 花生壳 三选一）
- `.env` 企微相关字段对齐企微后台
- 回调 URL 通过企微"保存"校验（真机走一次 GET `/webhook/wecom`）
- 企业成员至少 5 人（2 工人 + 2 厂家 + 1 中介）
- MySQL seed：预注册厂家/中介用户、≥ 20 条在线岗位、≥ 5 份简历
- 演示脚本（逐分钟）+ 彩排视频 / 截图备份
- 故障应急手册（§7）

### 3.2 本任务明确不做

- 不做客户认证级别的企微接入（交给客户权限就绪后切换）
- 不做生产级高可用（cpolar 免费版或付费版即可，不上 K8s）
- 不做企微客户联系 API 的代码适配（留给后续）
- 不做 Demo 内容以外的业务扩展

---

## 4. 前置条件

| 条件 | 状态 | 不满足的应对 |
|---|---|---|
| Phase 4 代码已合并（webhook / worker / message_router / command_service） | ✅ 已满足 | —— |
| Phase 4 单测 + 集成测通过 | ✅ 已满足 | —— |
| `wecom_inbound_event` DB 迁移已执行（media_id 列 + 扩展枚举）| ✅ 本地已迁移 | Demo 机器上手工执行 `ALTER TABLE`（见 §5.3）|
| LLM_API_KEY 可用 | ⚠️ 待确认 | Demo 前一天用简单 prompt ping 一次验证 |
| 可联网的 WSL2 / Linux 机器 | ⚠️ 待确认 | Demo 用一台长期稳定的机器 |
| 二级域名或固定公网地址 | ⚠️ 待确认 | 选 cpolar 付费版（最省事）|

---

## 5. 实施 Checklist

### 5.1 企微注册与应用创建（~30 分钟）

- [ ] 打开 https://work.weixin.qq.com/ → 注册企业
- [ ] 企业名填"JobBridge Demo"（或复用已有小微/个体企业）
- [ ] 个体工商户 / 小微企业均可，**不需付费认证**
- [ ] 管理后台 → 应用管理 → 创建应用 → 自建应用"招工助手"
- [ ] 可见范围：暂时先加自己，后续加演示成员后再扩
- [ ] 记录：
  - `CorpID`（"我的企业" → "企业信息"）
  - `AgentId`（应用详情页）
  - `Secret`（应用详情 → "Secret" → 点"查看"，发到企业微信 App）
- [ ] 应用详情 → "功能" → "接收消息" → 配置 API 接收消息（暂时先不填 URL）
- [ ] "接收消息" → 生成并记录 `Token`（自己设定，如 `jobbridge_demo_token`）
- [ ] "接收消息" → 点"随机生成" `EncodingAESKey`（43 位）

### 5.2 公网回调通道（~30 分钟 ~ 1 小时）

三选一，推荐 **A（cpolar 付费版）**：

#### 方案 A：cpolar 付费版（推荐）

- [ ] 官网 https://www.cpolar.com/ 注册 → 升级至"基础版"（~9.9 元/月）
- [ ] 拿到固定二级域名，如 `xxx.cpolar.cn`
- [ ] WSL2 内：
  ```bash
  curl -L https://www.cpolar.com/static/downloads/install-release-cpolar.sh | sudo bash
  cpolar authtoken <token>
  cpolar http --region cn_top --subdomain <your-subdomain> 8000
  ```
- [ ] 验证浏览器可访问 `https://xxx.cpolar.cn/health`

#### 方案 B：frp + 阿里云轻量服务器

- [ ] 买一台阿里云轻量（~9 元/月，可选）
- [ ] VPS 装 frps，WSL2 装 frpc
- [ ] 绑定自有域名（如 `demo.yourcompany.com`）+ Let's Encrypt 免费证书
- [ ] 全程自己维护，长期稳定

#### 方案 C：花生壳（免费版）

- [ ] 二级域名免费但有限流
- [ ] 只适合演示当天临时使用

### 5.3 DB 迁移（仅 Demo 机器首次需要）

```sql
-- 连接 Demo 机器的 MySQL
ALTER TABLE wecom_inbound_event
  MODIFY COLUMN msg_type
    ENUM('text','image','voice','video','file','link','location','event','other') NOT NULL,
  ADD COLUMN media_id VARCHAR(128) DEFAULT NULL
    COMMENT '媒体消息的 media_id' AFTER msg_type;
```

- [ ] 执行上述 DDL
- [ ] `DESCRIBE wecom_inbound_event` 验证 `media_id` 列存在 + `msg_type` 枚举有 9 个值

### 5.4 配置 `.env` 与启动服务（~15 分钟）

- [ ] `backend/.env` 填入：
  ```
  WECOM_CORP_ID=<5.1 记录的 CorpID>
  WECOM_AGENT_ID=<5.1 记录的 AgentID>
  WECOM_SECRET=<5.1 记录的 Secret>
  WECOM_TOKEN=<5.1 设定的 Token>
  WECOM_AES_KEY=<5.1 生成的 EncodingAESKey>
  ```
- [ ] 回企微后台 → 应用 → "接收消息"：
  - URL：`https://xxx.cpolar.cn/webhook/wecom`
  - Token / EncodingAESKey：与 `.env` 一致
  - 点击"保存" → 若出现"保存成功"代表 **GET URL 校验通过**（verify_signature + decrypt_message 真机走通 ✅）
- [ ] 启动三个进程（建议 tmux / screen 分屏）：
  ```bash
  # 屏 1：cpolar
  cpolar http --region cn_top --subdomain <name> 8000

  # 屏 2：FastAPI
  cd backend && source .venv-wsl/bin/activate
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info

  # 屏 3：Worker
  cd backend && source .venv-wsl/bin/activate
  python -m app.services.worker
  ```
- [ ] 用自己的企业微信 App 给"招工助手"应用发 `"你好"` → 观察到：
  - webhook 日志：`webhook: accepted msg_id=... elapsed_ms=...`
  - Worker 日志：`worker: processed msg_id=... replies=1`
  - 手机收到欢迎语

### 5.5 企业成员 + 测试数据 seed（~1~2 小时）

#### 5.5.1 添加企业成员

在企微后台 → 通讯录 → 添加成员：

| 姓名 | 账号（userid）| 角色扮演 |
|---|---|---|
| 演示工人-小明 | `demo_worker_xm` | 工人 |
| 演示工人-小王 | `demo_worker_xw` | 工人 |
| 演示厂家-张总 | `demo_factory_zz` | 厂家 |
| 演示厂家-李总 | `demo_factory_lz` | 厂家 |
| 演示中介-李姐 | `demo_broker_lj` | 中介 |

- [ ] 5 个成员全部添加完成
- [ ] 让每个成员扫码加入企业（邀请链接在通讯录里）
- [ ] 让每个成员在自己的企业微信 App 里关注"招工助手"应用
- [ ] 确认每个成员都能收到自建应用消息（发一条测试消息验证）

#### 5.5.2 MySQL 预注册数据

- [ ] 创建 `backend/sql/seed_demo.sql`（不纳入版本控制），内容示例：

```sql
-- 预注册厂家 / 中介用户（工人由系统自动注册）
INSERT INTO user (external_userid, role, status, display_name, company,
                  contact_person, phone, can_search_jobs, can_search_workers)
VALUES
  ('demo_factory_zz', 'factory', 'active', '张总', '苏州电子厂',
   '张总', '13800000001', 0, 1),
  ('demo_factory_lz', 'factory', 'active', '李总', '昆山服装厂',
   '李总', '13800000002', 0, 1),
  ('demo_broker_lj',  'broker',  'active', '李姐', NULL,
   NULL,    '13800000003', 1, 1);

-- seed 20 条岗位（覆盖苏州/昆山/无锡 × 电子/服装/普工）
-- 略：按 phase3 的 seed 风格补齐，city / job_category / salary_floor_monthly
-- pay_type / headcount / expires_at 必填；审核状态全部 passed
```

- [ ] 执行 seed：`mysql -u jobbridge -pjobbridge jobbridge < sql/seed_demo.sql`
- [ ] 验证：`SELECT role, COUNT(*) FROM user WHERE external_userid LIKE 'demo_%' GROUP BY role`

#### 5.5.3 业务链路端到端自测

- [ ] 演示工人-小明发"你好" → 收到欢迎语
- [ ] 演示工人-小明发"苏州找电子厂，5000 以上" → 收到推荐 3 条
- [ ] 演示工人-小明发"更多" → 收到下一批
- [ ] 演示工人-小明发"/重新找" → 清空确认
- [ ] 演示厂家-张总发"/我的状态" → 收到状态摘要
- [ ] 演示中介-李姐发"/找岗位" → 切换确认，再发"苏州电子厂" → 收到岗位结果

### 5.6 演示脚本 + 彩排（~1 小时）

见 §6。

- [ ] 按演示脚本完整跑一遍
- [ ] 录制视频备份（演示当天若真机挂了，视频兜底）
- [ ] 关键截图：每个场景最终状态 1 张图

---

## 6. 演示脚本（30 分钟版本）

### 6.1 开场（2 分钟）

- 主讲人一句话介绍："JobBridge 一期核心是把招工撮合从线下搬到企微，通过 LLM 做意图理解 + 结构化抽取 + 匹配推荐。今天我们演示完整的企微消息主链路。"
- 打开三块屏：
  - 屏 A：演示者手机企业微信 App（投屏到大屏）
  - 屏 B：Worker / webhook 日志滚动
  - 屏 C：Redis CLI + MySQL 查询终端

### 6.2 场景 1：新工人完整流程（6 分钟）

| 动作 | 预期 | 观察点 |
|---|---|---|
| 工人-小明发"你好" | 自动注册 → 欢迎语 | DB：`user` 表多一行 `role=worker`；日志：`auto-registered worker` |
| 工人-小明发"苏州找电子厂，5000以上，包吃住" | 推荐 Top 3（编号+核心字段+小程序链接）| 日志：`search_jobs top_n=3`；手机：工人侧不展示电话 |
| 工人-小明发"工资再高点" | follow_up → 更新 criteria → 重新推荐 | 日志：`merge_criteria_patch digest changed` |
| 工人-小明发"更多" | show_more → 从快照取下一批 | DB：`conversation_log` 的 `criteria_snapshot` 含 prompt 版本 |
| 工人-小明发"/重新找" | 清空确认 | Redis：`session:demo_worker_xm` 的 `search_criteria` 清空 |

### 6.3 场景 2：厂家发布岗位（3 分钟）

| 动作 | 预期 | 观察点 |
|---|---|---|
| 厂家-张总发"苏州电子厂招普工 30 人，5500 包吃住，三班倒，18-40 岁" | 入库 + 审核通过 + 确认 | DB：`job` 表多一行 `audit_status=passed` |
| 厂家-张总发"/我的状态" | 状态摘要（含最近岗位） | 手机：显示"最近岗位 #xx 审核状态 已通过" |

### 6.4 场景 3：中介双向切换（3 分钟）

| 动作 | 预期 |
|---|---|
| 中介-李姐发"/找岗位" | 切换确认 |
| 中介-李姐发"苏州电子厂" | 返回岗位列表 |
| 中介-李姐发"/找工人" | 切换确认 |
| 中介-李姐发"找苏州普工 30 岁以内" | 返回工人列表（含电话）|

### 6.5 场景 4：系统能力展示（5 分钟）

| 动作 | 预期 | 大屏展示 |
|---|---|---|
| 工人-小王发语音 | "暂不支持语音" | 屏 C：`SELECT status, COUNT(*) FROM wecom_inbound_event GROUP BY status` |
| 工人-小王发图片 | "图片已收到" | Worker 日志：`image download/save` |
| 工人-小王 6 秒内发 6 条消息 | 第 6 条被限流，不入队 | 屏 C：`LLEN queue:rate_limit_notify = 1` |

### 6.6 场景 5：Worker 高可用（3 分钟）

| 动作 | 预期 | 观察点 |
|---|---|---|
| `kill` Worker 进程 | webhook 仍接收消息入队 | 屏 C：`LLEN queue:incoming` 随发消息递增 |
| 工人-小明发 2 条消息 | 暂无回复（Worker 下线） | —— |
| 重启 Worker（`python -m app.services.worker`）| 启动自检扫 `processing` → 消息被消费 | 日志：`startup recovery requeue msg_id=...` |
| 工人-小明手机上 | 收到消息回复 | —— |

### 6.7 场景 6：命令矩阵（5 分钟）

| 命令 | 演示者 | 预期 |
|---|---|---|
| `/帮助` | 工人-小明 | HELP_TEXT |
| `/续期 15` | 厂家-张总 | 最近岗位 +15 天 |
| `/续期 30` | 厂家-李总 | 最近岗位 +30 天 |
| `/下架` | 厂家-张总 | `delist_reason=manual_delist` |
| `/招满了` | 厂家-李总 | `delist_reason=filled` |
| `/人工客服` | 工人-小王 | 返回联系方式文案 |

### 6.8 收尾（3 分钟）

- Q&A 5 分钟
- 如客户问到"权限时的实际差异"：回答"代码按方案 A 实现，切换到贵方企微后只需更新 .env 配置；当前 Demo 展示的全部业务逻辑和交互体验与生产一致"

---

## 7. 风险与应急

| 风险 | 概率 | 触发迹象 | 应急方案 |
|---|---|---|---|
| cpolar / 内网穿透掉线 | 中 | 手机发消息 Worker 日志无反应 | 备份 ngrok 或临时切换为 frp；或改用录制视频演示 |
| 企微 5 秒重试风暴 | 低 | webhook 日志大量 duplicate msg_id | 正常现象，幂等已防住；向客户说明"这正是我们设计的幂等防线起作用" |
| LLM 超时或 429 | 中 | 意图分类慢，回复延迟 > 10s | 演示用工人账号提前 5 分钟发一条相同话题预热；备份回答截图 |
| Worker 卡死 | 低 | `inbound_event` 长时间 `processing` 状态 | 按场景 5 演示"Worker 高可用"化险为夷 |
| 企微 token 过期 | 低 | errcode=42001 | 代码已有 `invalidate_token()` 自动恢复；演示前 10 分钟手工 curl 刷新一次 |
| WSL2 机器突然睡眠 | 中 | 整条链路无响应 | 演示机器关闭电源休眠策略；插电源；有线网 |
| 中文 emoji 编码乱码 | 低 | 回复出现 `??` 或 `\uXXXX` | 检查 `json.dumps(..., ensure_ascii=False)`；确认 MySQL 连接串含 `charset=utf8mb4` |

**统一兜底**：演示前一天完整彩排一次 + 录屏，真机挂了切录屏继续讲。

---

## 8. 文件 / 配置变更清单

| 操作 | 路径 | 说明 |
|---|---|---|
| 修改 | `backend/.env` | 填入企微真实配置（**不提交版本控制**）|
| 新建 | `backend/sql/seed_demo.sql` | Demo 数据 seed 脚本（**不提交版本控制**，保留样例在本文档中）|
| 新建 | `scripts/demo_env_start.sh`（可选）| 一键启动 cpolar + uvicorn + worker 的 tmux session |
| 新建 | `scripts/demo_env_stop.sh`（可选）| 一键收尾 |
| 新建 | `docs/demo_rehearsal.md`（可选）| 演示脚本 + 彩排记录归档 |

**不改动**：
- 应用代码（`backend/app/` 下全部）不改
- 数据库 schema：仅跑已有的 Phase 4 迁移（§5.3）

---

## 9. 未来迁移到客户企微的路径

### 9.1 代码改动评估

| 场景 | 代码改动 |
|---|---|
| 客户也用"自建应用" API（不开客户联系）| **0 改动**，只改 `.env` |
| 客户走"客户联系"API（externalcontact）| `WeComClient` 加一个 `send_text_external()` 方法；message_router 按用户来源（企业成员 vs 外部联系人）分流 — **约 2~3 人小时** |

### 9.2 切换 Checklist

- [ ] 拿到客户方 CorpID / AgentID / Secret / Token / EncodingAESKey
- [ ] 改客户生产环境 `.env`
- [ ] 客户企微后台配置回调 URL（改成客户域名 + HTTPS）
- [ ] 客户 IP 白名单加生产服务器出口 IP
- [ ] 清空 Demo 数据：`DELETE FROM user WHERE external_userid LIKE 'demo_%'` 等
- [ ] 按客户真实业务数据重新 seed（城市字典、工种字典、敏感词等保留）
- [ ] 客户侧扫码绑定真实用户，全链路回归一次

### 9.3 什么时候做切换

客户满足以下全部时：
- 企微认证级别确认（方案 §17.3 `企微认证级别` 从 `待确认` 变 `已确认`）
- 客户联系 API 权限开通（如需要）
- 生产回调域名 + HTTPS 就绪

建议放在 **Phase 7 末尾 / 上线前一周**，给回归测试留 2 人日。

---

## 10. 验收标准

- [ ] 企业微信"招工助手"应用已创建，URL 校验通过
- [ ] cpolar / frp / 花生壳之一长期稳定运行
- [ ] 5 个企业成员齐备，各自能收发消息
- [ ] seed 数据完整（≥ 20 岗位、≥ 5 简历、3 个预注册用户）
- [ ] 演示脚本 §6 的 6 个场景全部在真机跑通
- [ ] 录屏备份已完成
- [ ] `.env` 和 seed SQL 不在版本控制中
- [ ] 故障应急手册 §7 已交底给演示者

---

## 11. 投入与时间线

| 阶段 | 时长 | 负责角色 |
|---|---|---|
| 企微注册 + 应用创建 | 30 分钟 | 技术 |
| 公网回调通道 | 30 分钟 ~ 1 小时 | 技术 |
| DB 迁移 + `.env` 配置 + 联通测试 | 30 分钟 | 技术 |
| 企业成员 + seed 数据 | 1 ~ 2 小时 | 技术 + 业务 |
| 演示脚本彩排 + 录屏 | 1 小时 | 技术 + 主讲人 |
| **合计** | **~1 人日** | —— |

## 12. 备注

- 本任务不产生生产代码变更，因此不需要 PR review 流程
- `.env` 和 `seed_demo.sql` 含敏感配置，**严禁**提交到 git；已在 `.gitignore` 中
- cpolar 付费费用可作为项目临时支出报销（~50 元覆盖 5 个月）
- Demo 之后该环境保留，服务 Phase 5/6/7 的联调
- 如 Demo 前发现本文档 §5 任何步骤有阻塞，立即升级到项目例会
