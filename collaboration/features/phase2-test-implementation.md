# Phase 2 测试实施文档

> 基于：`collaboration/features/phase2-main.md`
> 面向角色：测试
> 状态：`in-progress`
> 创建日期：2026-04-12

## 1. 测试目标

Phase 2 测试的核心不是看业务效果，而是确认基础设施契约是否稳定、可被后续复用、可被 mock、可被自动化测试约束住。

测试应重点回答以下问题：

- LLM provider 是否真的通过统一工厂切换
- Prompt 是否已经形成统一入口、token 预算和严格 JSON 契约
- Storage 是否具备稳定的本地文件行为
- WeCom 回调和客户端封装是否形成统一输入输出
- Phase 4 会直接依赖的消息基础契约是否已被固定
- 这些能力是否已具备自动化测试，不依赖真实外部服务

## 2. 当前现状与测试重点

当前代码现状决定了本阶段测试重点：

- 抽象接口已存在，但 provider 和封装大概率是新增实现
- 真实外部服务不可作为测试依赖
- 本阶段大部分测试应该以单元测试和 mock 集成为主

因此建议的测试主轴是：

1. 契约正确
2. 异常可控
3. 配置驱动
4. 可 mock
5. 不越界

另外，Phase 2 还需要确认一类“文档契约”已经固定，而不是等到 Phase 4 再临时决定：

- Redis 限流 / 幂等 / 入队 / 死信调用顺序
- `wecom_inbound_event` 状态流转
- 死信转移条件

## 3. 本阶段测试范围

### 3.1 必测范围

1. LLM
- provider 工厂选择
- provider 请求/响应适配
- 非 JSON fallback
- 超时和重试
- prompt 常量、版本、token 预算、严格 JSON 契约

2. Storage
- 本地文件保存
- URL 获取
- 删除
- 存在性检查
- key 路径规则

3. WeCom
- 验签
- 解密
- XML 解析
- 统一消息对象字段
- 客户端发送/下载/查联系人接口封装

4. 消息基础契约
- `check_rate_limit()`、`check_msg_duplicate()`、`enqueue_message()`、`dequeue_message()` 的调用约定
- `queue:incoming`、`queue:dead_letter` 的命名约定
- `wecom_inbound_event` 的状态流转规则
- 重试上限与死信转移条件

### 3.2 不测范围

- 业务 service 行为
- webhook 路由
- worker 消费
- 多轮会话逻辑
- `criteria_patch` merge
- 上传/检索/权限/审核业务结果
- 真实企微联调
- 真实 LLM 精度和效果

## 4. 测试方法建议

### 4.1 LLM 测试方法

LLM 部分以 mock HTTP 为主，不依赖真实 API key。

建议覆盖：

- 工厂根据 provider 字符串返回正确实现
- 未知 provider 返回明确错误
- provider 收到合法响应时能生成统一 DTO
- provider 收到非法 JSON 字符串时走 fallback
- provider 超时时触发一次重试
- 重试后仍失败时走统一失败路径
- provider fallback 不负责最终用户中文回复文案

兜底边界判定：

- provider 层只验证“返回统一 fallback 结果或抛统一异常”
- “没太理解您的意思”“系统繁忙”等最终文案属于 Phase 3，不在 Phase 2 断言其文本内容

### 4.2 Prompt 测试方法

Prompt 不测“效果”，只测“形状和约束”。

建议覆盖：

- prompt 常量存在
- 含版本标记
- 含 token 预算注释
- 含严格 JSON 文案
- 输出字段要求和 DTO 契约对齐
- 不允许 markdown code block 输出

### 4.3 Storage 测试方法

Storage 测试优先使用临时目录，不污染真实上传目录。

建议覆盖：

- `save()` 后文件真实存在
- `get_url()` 返回值稳定
- `exists()` 与文件状态一致
- `delete()` 后文件移除
- 删除不存在文件时行为符合实现约定
- key 包含多级目录时仍可工作
- key 命名符合统一模板 `{entity_type}/{record_id}/{uuid}.{ext}`

### 4.4 WeCom 测试方法

WeCom 分两层测试：

1. 纯算法/解析层
- 验签
- 解密
- XML 解析

2. HTTP 封装层
- `send_text()`
- `download_media()`
- `get_external_contact()`

建议全部通过 fixture + mock HTTP 响应完成，不依赖真实企微后台。

`get_external_contact()` 需要额外验证：

- 返回 `None` 只表示用户不存在或已删除
- 网络错误、鉴权失败、接口异常应抛异常

### 4.5 消息契约检查方法

这部分不是跑真实 webhook / worker，而是确认契约已经被清晰固定，避免 Phase 4 重新拍脑袋。

建议覆盖：

- 文档中已明确 webhook 先限流、再幂等、再入队
- 文档中已明确 worker 消费和死信转移顺序
- 文档中已明确 `wecom_inbound_event` 的 5 个状态值和流转
- 文档中已明确最大重试次数为 2

## 5. 缺陷判定口径

出现以下任一情况，应判定 Phase 2 不通过：

- service 层仍需直接 import 具体 provider
- provider 工厂不能稳定切换
- prompt 没有统一入口或缺少严格 JSON 约束
- prompt 缺少 token 预算约束
- LLM provider 对非法 JSON 没有可验证 fallback
- 超时没有重试或行为不可预测
- LocalStorage 无法稳定保存/删除文件
- WeCom XML 不能稳定转成统一消息对象
- `get_external_contact()` 的 `None` 语义不清
- Phase 4 依赖的消息基础契约未被文档化
- 基础设施模块无法通过 mock 测试
- 为了实现 Phase 2 提前写入 Phase 3/4 业务逻辑

## 6. 推荐测试顺序

1. 先跑纯单元测试
- prompt
- provider 工厂
- storage 路径逻辑
- callback XML 解析

2. 再跑 mock HTTP 测试
- LLM provider
- WeCom client

3. 最后跑轻量集成测试
- LocalStorage 临时目录读写删
- 配置驱动的 provider 切换

4. 最后做契约复核
- 队列 key
- 限流 / 幂等 / 入队顺序
- `wecom_inbound_event` 状态机
- 死信规则

## 7. 推荐运行方式

如果本阶段新增自动化测试需要区分 unit / integration，文档示例必须同时覆盖 Linux/macOS、PowerShell、CMD。

示例写法：

- Linux/macOS：`RUN_INTEGRATION=1 pytest tests/integration/... -v`
- PowerShell：`$env:RUN_INTEGRATION='1'; pytest tests/integration/... -v`
- CMD：`set RUN_INTEGRATION=1 && pytest tests/integration/... -v`

## 8. 测试输出要求

测试结束后，请至少输出：

- 通过 / 不通过结论
- 不通过项清单
- 复现步骤
- 测试环境说明
- 自动化测试执行结果
- 是否可进入 Phase 3

## 9. 对开发的交付要求

测试开始前，应确认开发已交付：

- 新增代码文件
- 对应自动化测试
- 如新增配置项则同步 `.env.example`
- 必要的 README 或执行说明

如果缺这些，测试可直接退回，不进入功能验证。

关于 `doubao.py`：

- Phase 2 以“结构骨架 + mock 测试通过”为验收口径
- 如果没有真实 API Key，不应据此阻塞整个 Phase 2
