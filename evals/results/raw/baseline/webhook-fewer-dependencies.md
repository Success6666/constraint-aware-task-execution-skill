当前仓库为空，且没有指定语言或框架。下面给出技术栈无关的实施计划，默认使用现有 HTTP 框架、数据库及其驱动；签名校验直接使用语言标准加密库，不引入消息队列、Webhook SDK 或额外日志框架。

由于工作区为只读，无法按约定写入 `tmp-docs/webhook-receiver-implementation-plan.md`。

**目标架构**

```text
Webhook Provider
       |
       v
HTTP Receiver
  1. 限制请求大小
  2. 读取原始请求体
  3. 校验时间戳和签名
  4. 数据库唯一键去重
  5. 持久化事件和审计记录
       |
       +---- duplicate --> 200
       |
       +---- accepted  --> 202/200
                              |
                              v
                    Database-backed Worker
                     1. 原子领取任务
                     2. 执行业务处理
                     3. 成功 / 延迟重试
                     4. 超限进入 dead letter
```

HTTP 接收与业务处理解耦。只有事件已可靠写入数据库后才返回成功；业务失败由后台任务重试。整体提供“至少一次接收 + 幂等处理”，不宣称无法真正保证的端到端 exactly-once。

## 一、范围与假设

首版支持：

- `POST /webhooks/{provider}`。
- HMAC-SHA256 签名验证。
- 多密钥验证，支持无停机密钥轮换。
- 时间戳容差和重放攻击防护。
- 基于提供方事件 ID 的持久化幂等。
- 数据库驱动的指数退避重试。
- 死信状态和人工重放命令。
- 结构化、可检索的审计日志。
- 健康检查、基础指标和完整测试。

首版不包含：

- Kafka、RabbitMQ、Redis 等新基础设施。
- 通用可视化管理后台。
- 动态插件系统。
- 自研密码算法。
- Webhook 发送端能力。

开工前需要确定：

- 编程语言、HTTP 框架和数据库。
- 首批提供方及其签名协议。
- 成功响应要求是 `200` 还是 `202`。
- 事件保留期、审计保留期和敏感数据政策。
- 业务处理器是否能与 inbox 状态更新使用同一数据库事务。

## 二、模块划分

建议目录保持以下职责边界：

```text
src/
  webhook/
    endpoint
    raw_request
    signature_verifier
    provider_config
    event_parser
    repository
    retry_policy
    worker
    audit
    handlers/
  config/
  database/
  commands/
    replay_webhook
migrations/
tests/
  unit/
  integration/
  fixtures/
```

核心接口：

- `SignatureVerifier`：接收原始字节、签名头、时间戳和密钥集合。
- `WebhookRepository`：插入事件、领取任务、更新结果。
- `EventHandler`：按 `provider + event_type` 分发业务处理。
- `RetryPolicy`：计算下次执行时间并判定是否进入死信。
- `AuditSink`：记录安全和状态变化，不记录密钥。

提供方之间只隔离协议适配层，不复制存储、重试和审计逻辑。

## 三、数据模型

### `webhook_events`

关键字段：

- `id`：内部 ID。
- `provider`、`endpoint_id`。
- `external_event_id`：提供方稳定事件 ID。
- `event_type`。
- `status`：`pending | processing | succeeded | retry_wait | dead_letter`。
- `payload`：原始请求体或加密后的原始请求体。
- `payload_sha256`：审计与冲突检测。
- `signature_version`、`verified_key_id`。
- `received_at`、`processing_started_at`、`processed_at`。
- `attempt_count`、`next_attempt_at`。
- `last_error_code`、`last_error_summary`。
- `locked_by`、`lock_expires_at`。
- `created_at`、`updated_at`。

约束和索引：

- 唯一约束：`(provider, endpoint_id, external_event_id)`。
- 待处理索引：`(status, next_attempt_at)`。
- 查询索引：`(provider, received_at)`。
- 同一事件 ID 但 payload hash 不同，需要拒绝并产生高优先级审计事件。

### `webhook_audit_logs`

记录：

- `event_id`、`provider`。
- 动作：`received`、`signature_rejected`、`duplicate`、`processing_started`、`retry_scheduled`、`succeeded`、`dead_lettered`、`manually_replayed`。
- `actor_type`、`actor_id`。
- `request_id`、来源 IP。
- 前后状态、错误分类、时间戳。
- 可选 JSON 元数据，但必须经过字段白名单和脱敏。

审计日志采用追加写，不允许普通业务流程更新或删除。

## 四、接收流程

1. 只允许 `POST` 和预期 `Content-Type`。
2. 在读取正文前执行请求体大小限制，建议默认 `256 KiB`，按提供方调整。
3. 保留原始请求字节，禁止先解析 JSON 再重新序列化后验签。
4. 验证必需请求头、时间戳格式和事件 ID 格式。
5. 检查时间戳容差，默认正负 5 分钟，可按提供方配置。
6. 使用标准库计算 HMAC-SHA256，并用常量时间比较。
7. 支持同时验证当前密钥和上一把密钥，但审计中仅记录密钥标识。
8. 验签成功后解析 JSON，并验证最小事件结构。
9. 在数据库事务中插入事件和 `received` 审计记录。
10. 由数据库唯一约束解决并发重复请求，不能使用“先查询、后插入”去重。
11. 重复且 hash 相同：记录 `duplicate`，返回成功。
12. 持久化成功：立即返回 `200` 或 `202`。
13. 数据库不可用：返回 `503`，让提供方稍后重投。

建议状态码：

- `200/202`：首次接收或已安全处理的重复事件。
- `400`：请求头、JSON 或事件结构无效。
- `401`：签名无效。
- `413`：请求体过大。
- `415`：媒体类型不支持。
- `503`：无法持久化，提供方应重试。

签名、时间戳和原始正文绑定是成熟 webhook 协议的通行做法；Standard Webhooks 还定义了事件 ID、时间戳、多签名轮换和常量时间比较要求。[Standard Webhooks 规范](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)

## 五、幂等策略

幂等分两层：

- 接收幂等：数据库唯一约束保证同一外部事件只进入 inbox 一次。
- 业务幂等：处理器使用事件 ID 或业务唯一键，保证 worker 崩溃后重复执行不会重复扣款、发货或通知。

处理器与状态更新能共享数据库时，应放入同一事务：

```text
业务变更
+ 幂等处理记录
+ webhook_events.status = succeeded
```

无法共享事务时，处理器必须调用具有幂等键的下游接口，并保存下游操作标识。仅依靠 `status = processing` 不能避免进程崩溃造成的重复副作用。

## 六、重试与恢复

worker 使用数据库行锁或原子条件更新领取任务，并设置租约。进程退出后，过期租约能够重新领取。

建议策略：

```text
delay = random(0, min(base * 2^attempt, max_delay))
base = 5 秒
max_delay = 1 小时
max_attempts = 10
```

错误分类：

- 可重试：超时、连接失败、下游 `429`、大多数 `5xx`、临时锁冲突。
- 不可重试：永久业务校验错误、不支持的事件类型、确定不存在的资源。
- 未知错误：默认可重试，但错误摘要必须截断并脱敏。

超过次数或时限后进入 `dead_letter`。提供最小化 CLI：

```text
webhook replay --event-id <id>
webhook replay --provider <name> --since <time> --status dead_letter
```

人工重放不得创建新的外部事件记录；应重置执行状态、增加审计记录，并保留原失败历史。Stripe 同样建议已处理的重复事件返回成功，以终止提供方继续重试。[Stripe 重复事件处理](https://docs.stripe.com/webhooks/process-undelivered-events)

## 七、安全与审计要求

- 密钥只从环境变量或现有密钥管理服务读取。
- 日志严禁输出签名密钥、完整签名、授权头和完整敏感 payload。
- 错误响应使用固定信息，内部异常只进入受控日志。
- 对 endpoint、事件 ID、事件类型设置长度和字符集限制。
- JSON 解析后仍需执行 schema 或显式字段校验。
- 数据库查询全部参数化。
- 管理重放命令必须鉴权并记录操作者。
- 设置 payload 和审计日志保留策略，清理任务分批执行。
- 反向代理层启用 TLS、请求体限制、超时和基础速率限制。
- 来源 IP 白名单只能作为附加控制，不能替代签名验证。
- 不记录由请求头直接提供的“公钥”并将其视为可信密钥。

GitHub 官方同样要求在业务处理前验证共享密钥签名。[GitHub Webhook 验签指南](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

## 八、依赖控制

优先使用：

- 现有 HTTP 框架的原始 body API。
- 语言标准库的 HMAC、SHA-256、Base64、常量时间比较和随机数。
- 现有数据库驱动及迁移工具。
- 现有日志与配置机制。
- 数据库作为 inbox、任务队列和审计存储。

只有出现以下条件时才增加依赖：

- 提供方要求 Ed25519 等标准库不支持的算法：采用成熟加密库。
- 当前数据库无法安全实现并发任务领取：引入现有基础设施中的队列。
- payload schema 数量较多且手写校验不可维护：使用项目已有 schema 校验器。

不为简单 HMAC 校验引入完整 Webhook SDK。

## 九、实施顺序

1. 确定提供方协议、响应时限、数据库和保留策略。
2. 建立模块骨架、配置对象和数据库迁移。
3. 实现原始正文读取、大小限制、HMAC 验签和时间窗口检查。
4. 实现事务化 inbox 插入、唯一约束去重和审计追加。
5. 实现事件分发接口及一个端到端示例处理器。
6. 实现 worker 领取、租约、错误分类、退避重试和死信。
7. 实现人工重放 CLI、结构化日志和基础指标。
8. 完成安全测试、并发测试、故障恢复测试和运维文档。
9. 小流量启用首个提供方，验证重复投递、停机恢复和密钥轮换。
10. 稳定后再增加其他提供方适配器。

## 十、验收标准

- 正确签名的事件可在响应时限内持久化并返回成功。
- 对正文任意字节修改后，签名验证必定失败。
- 超出时间窗口的有效签名被拒绝。
- 当前密钥和轮换期旧密钥均可验证，废弃密钥不可验证。
- 100 个并发相同事件只产生一条 `webhook_events` 记录。
- 重复事件返回成功且不会重复产生业务副作用。
- 接收进程在插入前失败时返回非 `2xx`；插入后崩溃时事件仍可恢复处理。
- worker 在业务提交前后崩溃均不会造成不可控重复操作。
- 临时错误按退避策略重试，永久错误直接进入死信。
- worker 租约过期后任务可被其他实例重新领取。
- 人工重放有完整操作者、原因和状态变更审计。
- 日志中不存在密钥、完整签名或受保护 payload。
- 单元测试覆盖签名边界；集成测试覆盖数据库并发、事务和恢复路径。
- 依赖清单中除已有框架、数据库驱动外，没有因该功能新增的非必要运行时依赖。