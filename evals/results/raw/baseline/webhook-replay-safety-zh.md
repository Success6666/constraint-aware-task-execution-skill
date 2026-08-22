# 支付 Webhook 接收服务设计

## 1. 目标与处理原则

服务负责接收支付平台 Webhook，完成：

- 请求身份验证：签名、时间戳、nonce。
- 防重放：时间窗口、nonce 一次性使用、事件幂等。
- 可靠处理：先持久化接收记录，再异步执行业务。
- 审计追踪：记录原始请求、验证结果、处理状态和错误原因。
- 明确拒绝策略：签名无效、时间偏差超限、nonce 重复或事件已被处理时，拒绝业务处理。

Webhook 入口必须是 HTTPS，并将请求体按原始字节读取。签名校验前禁止 JSON 重新序列化、字段排序或字符串 trim。

## 2. API 契约

### Endpoint

```http
POST /api/v1/webhooks/{provider}
Content-Type: application/json
X-Webhook-Id: evt_123
X-Webhook-Timestamp: 1710000000
X-Webhook-Nonce: 8c5e...
X-Webhook-Signature: v1=base64-signature
```

建议同时支持平台原生 Header，但在内部统一映射为标准字段：

- `provider`
- `event_id`
- `timestamp`
- `nonce`
- `signature`
- `raw_body`

### 成功响应

请求已经通过验证并成功登记后立即返回：

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{"accepted":true}
```

如果平台要求同步确认业务结果，也应只确认“事件已可靠接收”，不要在 HTTP 请求中执行长时间业务逻辑。

### 拒绝响应

```json
{"accepted":false,"code":"invalid_signature"}
```

建议状态码：

| 情况 | HTTP 状态码 | code |
|---|---:|---|
| 缺少必要 Header | 400 | `invalid_request` |
| JSON 格式非法 | 400 | `invalid_request` |
| 签名无效 | 401 | `invalid_signature` |
| 时间偏差超限 | 401 | `timestamp_out_of_range` |
| nonce 已使用 | 409 | `replayed_request` |
| event_id 已成功登记 | 200 或 202 | `already_accepted` |
| 服务临时不可用 | 503 | `temporarily_unavailable` |

对外响应不要暴露密钥、详细验签差异或内部数据库错误。日志中同样不得记录完整签名密钥和支付敏感数据。

## 3. 签名方案

推荐使用 HMAC-SHA256，并将签名版本化。签名输入使用明确格式，例如：

```text
v1:{timestamp}.{nonce}.{raw_body}
```

签名计算：

```text
expected = Base64(HMAC-SHA256(secret, signing_string))
```

验证规则：

1. 读取原始请求体字节 `raw_body`。
2. 校验 Header 是否存在、格式是否正确、长度是否在限制内。
3. 解析时间戳，但不接受非数字、浮点或超长值。
4. 构造签名字符串时使用原始 Header 值和原始 Body。
5. 计算期望签名。
6. 使用常量时间比较函数比较签名，禁止普通字符串比较。
7. 支持密钥轮换时，先尝试当前密钥，再尝试上一把仍在有效期内的密钥；验证结果应记录使用的密钥版本。
8. 未通过签名校验时立即结束，不写入可触发业务的事件队列。

如果供应商使用 RSA/ECDSA，则使用供应商规定的公钥、签名编码和 canonicalization 规则，但仍需保留原始消息字节，并拒绝不符合算法或密钥版本的请求。

## 4. 时间戳校验

使用服务端 UTC 当前时间：

```text
abs(server_now - request_timestamp) <= allowed_skew
```

建议默认 `allowed_skew = 300` 秒，并配置化。校验应在验签前完成，以降低无效请求消耗，但最终审计记录仍需保留拒绝原因。

注意事项：

- 服务器必须使用 NTP 或云平台时间同步。
- 时间戳单位必须固定为秒或毫秒，不能根据数值大小猜测。
- 允许的最大窗口不应超过业务安全要求。
- 时间窗口只解决过期问题，不能替代 nonce 和 event_id 幂等。

## 5. nonce 防重放

nonce 存储应使用 Redis 或等价的原子 KV 存储：

```text
SET webhook:nonce:{provider}:{nonce} 1 NX EX 600
```

只有 `SET NX` 成功时才允许继续处理。已存在则返回 `replayed_request`。

推荐流程：

1. 先校验签名和时间戳。
2. 使用 `provider + nonce` 作为唯一键执行原子占用。
3. nonce TTL 至少覆盖时间窗口，并额外留出传播和重试余量，例如时间窗口 300 秒、TTL 600 秒。
4. Redis 不可用时默认拒绝请求并返回 503，不能在无法保证防重放时继续执行业务。
5. nonce 应限制长度和字符集，防止异常键和资源滥用。

如果平台保证 `event_id` 全局唯一，仍建议保留 nonce 校验，因为同一事件可能被重新签名或通过不同请求参数重复发送。

## 6. 幂等设计

核心幂等键建议为：

```text
(provider, event_id)
```

数据库建立唯一约束：

```sql
UNIQUE (provider, event_id)
```

事件表至少包含：

```text
webhook_events
- id
- provider
- event_id
- event_type
- received_at
- request_timestamp
- nonce_hash
- signature_version
- key_version
- raw_body_encrypted_or_redacted
- body_sha256
- validation_status
- processing_status
- attempt_count
- last_error_code
- processed_at
- created_at
- updated_at
```

登记和入队必须保证一致性。推荐 Transactional Outbox：

1. Webhook 请求通过全部安全校验。
2. 在一个数据库事务中插入 `webhook_events` 和 `outbox_messages`。
3. 利用唯一约束处理并发重复事件。
4. 提交成功后返回 202。
5. Outbox worker 发布到内部队列，业务消费者处理事件。
6. 消费者以 `event_id` 或业务对象版本号再次执行幂等控制。

重复请求处理规则：

- 已存在且状态为 `accepted`、`processing` 或 `processed`：不重复触发业务，返回 200/202。
- 已存在且状态为 `rejected`：按拒绝原因返回，不允许直接覆盖原记录。
- 首次请求插入冲突时，重新读取事件状态并返回既定结果。
- 不能仅依赖内存锁、消费者单线程或消息队列“恰好一次”。

## 7. 推荐处理流水线

```text
请求进入
  -> 限制 Body 大小、Header 大小和请求超时
  -> 读取原始 Body
  -> 解析并校验必要 Header
  -> 校验时间戳
  -> 校验签名
  -> 原子占用 nonce
  -> 校验 JSON schema 和事件字段
  -> 事务写入 webhook_events + outbox_messages
  -> 返回 202
  -> 异步消费、执行业务幂等处理
  -> 更新处理状态并写审计日志
```

安全校验失败的请求不得写入业务队列。可以写入独立的安全审计流，但必须防止攻击者通过大量无效请求耗尽数据库。

## 8. 审计与日志

审计记录应不可随意修改，并至少包括：

- 请求唯一追踪 ID。
- provider、event_id、event_type。
- 接收时间、请求时间、时间偏差。
- nonce 的哈希值，不建议保存明文 nonce。
- Body SHA-256。
- 签名版本、密钥版本。
- 验证结果及拒绝原因。
- 来源 IP、User-Agent、服务实例。
- 处理状态、重试次数、最后错误、完成时间。
- 关联订单、支付意图、退款等业务 ID。

原始 Body 是否保存取决于合规要求：

- 含个人或支付敏感信息时加密存储，并设置严格保留期限。
- 不需要重放调试时只保存摘要和脱敏字段。
- 日志中对卡号、银行账户、身份证件、Token 等字段做彻底脱敏。
- 审计数据应具备访问控制、访问日志和保留策略。

建议提供内部查询接口，支持按 `provider/event_id/body_sha256/status/time range` 查询，但不得允许通过接口重新执行未经授权的事件。

## 9. 可靠性与安全控制

- 接入网关限流，按 provider、IP、租户分别限制。
- 限制请求体大小，例如 1 MB，并设置读取和处理超时。
- 对 JSON 做 schema 校验，拒绝未知危险结构和过深嵌套。
- 密钥放入 Secret Manager，禁止写入代码、配置仓库和日志。
- 支持密钥轮换、密钥版本和紧急吊销。
- 仅允许 TLS 1.2+，验证反向代理传递的真实 IP 时使用可信代理列表。
- 队列采用死信队列和指数退避；永久业务错误不能无限重试。
- 监控接收量、验签失败率、时间戳失败率、nonce 重放率、入队延迟、处理延迟、失败重试数和死信数量。
- 为异常失败设置告警，但对攻击流量做聚合，避免日志告警风暴。

## 10. 状态机

```text
received -> accepted -> processing -> processed
                         |              |
                         v              v
                       retrying       ignored_duplicate
                         |
                         v
                    dead_letter
```

安全验证失败单独记录为 `rejected`，不能进入 `accepted` 状态。状态迁移应带版本号或使用条件更新，防止并发消费者回写旧状态。

## 11. 测试方案

### 单元测试

覆盖：

- 正确签名通过。
- Body 仅改变一个字节时签名失败。
- Header 缺失、格式错误、签名版本不支持时拒绝。
- 常量时间比较路径被调用。
- 时间戳在边界内通过，刚好超出边界拒绝。
- 秒和毫秒单位按约定处理，错误单位拒绝。
- nonce 首次占用成功，第二次占用失败。
- nonce 的 provider 隔离。
- 密钥轮换时旧有效密钥通过，吊销密钥失败。
- 非法 JSON、超大 Body、超长 Header 被拒绝。

### 集成测试

- 验签成功后能在同一事务中写入事件和 outbox。
- 数据库提交失败时不返回 202。
- outbox 发布失败时事件可被扫描重发。
- 相同 `provider/event_id` 并发请求只有一条业务事件。
- Redis 不可用时请求返回 503 且不执行业务。
- 消费者重复收到消息时业务结果不重复。
- 失败重试、死信和状态迁移正确。

### 安全测试

- 重放完全相同的请求被 nonce 或 event_id 拦截。
- 使用旧时间戳的有效签名被拒绝。
- 修改 timestamp、nonce 或 Body 后验签失败。
- 尝试通过 JSON 空白、字段顺序、Unicode 转义绕过校验失败。
- 伪造代理 Header、异常 IP、超长输入和高并发请求不会绕过校验或耗尽资源。
- 日志和审计中没有密钥及未脱敏敏感数据。

### 端到端测试

使用支付平台沙箱发送真实 Webhook，验证：

1. 事件可靠接收并返回正确状态码。
2. 网络重试不会重复扣款、入账或更新订单。
3. 业务处理失败后能恢复重试。
4. 审计记录能够串起请求、队列消息和业务结果。

## 12. 验收标准

- 任意签名无效请求都不会进入业务处理。
- 任意超出时间窗口的请求都不会进入业务处理。
- 相同 nonce 的重放请求不会被再次接受。
- 相同 `provider + event_id` 的并发或重复请求最多产生一次业务效果。
- 所有接受的事件都有持久化记录和可恢复的异步处理路径。
- 所有拒绝请求都有可检索的原因，但不泄露敏感信息。
- 数据库、Redis、队列任一关键依赖故障时不会错误返回成功。
- 密钥轮换、重试、死信、恢复和审计查询均有自动化测试覆盖。
