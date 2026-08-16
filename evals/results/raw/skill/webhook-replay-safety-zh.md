## 方案概述

设计一个“先验签、再去重、后入账”的支付 Webhook 接收服务：

```text
请求
  -> 解析并限制请求体
  -> 校验必要请求头
  -> 校验时间戳窗口
  -> 校验 HMAC 签名
  -> 原子占用 nonce
  -> 幂等检查 event_id
  -> 在事务中写入事件、业务数据和审计记录
  -> 返回结果
```

签名无效、时间偏差超限、nonce 已使用或事件已处理时，均不得执行支付业务处理。

## 请求协议

请求头：

```http
X-Pay-Timestamp: 1710000000
X-Pay-Nonce: 8f3d...32-byte-random
X-Pay-Key-Id: key_2026_01
X-Pay-Signature: v1=base64(hmac_sha256(secret, canonical_request))
X-Pay-Event-Id: evt_01HR...
Content-Type: application/json
```

签名原文使用明确的规范化格式，避免 JSON 字段顺序、空格或编码差异：

```text
v1
{timestamp}
{nonce}
{event_id}
{sha256(raw_request_body)}
```

其中：

- `timestamp` 使用 Unix 秒。
- `nonce` 至少 128 bit 随机数，建议 256 bit。
- `event_id` 是支付方生成的全局事件 ID。
- Body 必须使用原始字节计算 SHA-256，不能对解析后 JSON 再序列化。
- 签名比较使用常量时间比较。
- `key_id` 用于密钥轮换和审计定位。

算法可采用 HMAC-SHA256；若支付方已有 RSA/ECDSA 协议，则保留相同的规范化原文和校验流程。

## 校验规则

### 1. 基础请求校验

拒绝以下请求：

- 缺少必需请求头。
- Body 超过限制，例如 1 MB。
- `Content-Type` 不符合约定。
- `timestamp`、nonce、event_id、signature 格式非法。
- JSON 无法解析或事件类型不支持。

对外只返回统一错误，避免泄漏具体校验原因；详细原因写入内部审计日志。

### 2. 时间戳校验

设：

```text
skew = abs(server_now - request_timestamp)
```

当 `skew > 300 秒` 时拒绝请求。生产环境应使用同步时钟，并监控 NTP 偏差。

时间戳校验应在签名校验前进行，以减少无效请求造成的计算消耗；但审计中应记录失败阶段。

### 3. 签名校验

服务端根据 `key_id` 取得密钥，重新计算签名：

```text
expected = HMAC-SHA256(secret, canonical_request)
```

使用常量时间比较 `expected` 与请求签名。以下情况直接拒绝：

- `key_id` 不存在、已撤销或不在有效期内。
- 签名版本不支持。
- 签名格式非法。
- 签名不匹配。

密钥只保存在密钥管理系统中，应用不记录明文密钥和完整签名原文。

### 4. nonce 防重放

使用 Redis 或数据库建立带 TTL 的 nonce 记录，必须原子执行：

```text
SET webhook:nonce:{key_id}:{nonce} {event_id} NX EX 600
```

如果返回失败，表示 nonce 已出现过，拒绝处理。

TTL 应覆盖最大允许时间偏差并留出网络重试余量，例如时间窗口 5 分钟时设置 10 分钟。nonce 命名空间应包含租户或 key_id，防止不同支付方相互冲突。

### 5. 幂等处理

以 `event_id` 建立唯一约束：

```sql
CREATE UNIQUE INDEX ux_webhook_event_id
ON webhook_events(provider, event_id);
```

处理逻辑：

1. 事务内插入 `webhook_events`。
2. 若唯一键冲突：
   - 已成功处理：返回成功，避免支付方无限重试。
   - 正在处理：返回可重试状态，或由队列接管。
   - 上次失败：根据状态和重试策略重新处理。
3. 只有首次成功占用事件的请求可以执行业务变更。

幂等键应绑定 `provider + event_id`，不能只依赖客户端传入的订单号。订单号、金额、币种、支付状态等字段也应在重复请求时进行一致性校验；同一 `event_id` 内容不一致应标记为篡改并拒绝。

## 数据模型

### `webhook_events`

| 字段 | 说明 |
|---|---|
| `id` | 内部主键 |
| `provider` | 支付提供方 |
| `event_id` | 外部事件 ID |
| `event_type` | 事件类型 |
| `payload_hash` | 原始 Body SHA-256 |
| `timestamp` | 请求时间戳 |
| `nonce_hash` | nonce 哈希，不保存原 nonce 也可 |
| `key_id` | 签名密钥标识 |
| `status` | `RECEIVED/PROCESSING/SUCCEEDED/FAILED/REJECTED` |
| `failure_code` | 内部失败原因 |
| `received_at` | 接收时间 |
| `processed_at` | 完成时间 |
| `attempt_count` | 处理次数 |

### `webhook_audit_logs`

记录每次接收和处理结果：

- request ID、provider、event ID、key ID
- 客户端 IP、User-Agent
- 时间戳偏差
- payload hash
- 校验阶段和结果
- nonce 是否重复
- 幂等命中情况
- 业务处理结果
- 错误分类和耗时

不得记录完整支付敏感数据、密钥、Authorization、完整卡号或未经脱敏的 Body。审计日志应追加写入、限制权限并设置保留周期。

## 接口行为

建议统一响应：

```http
HTTP 200
{"accepted": true}
```

表示事件已成功处理或此前已经成功处理，支付方无需重试。

```http
HTTP 400
{"accepted": false, "code": "invalid_request"}
```

表示格式错误、时间过期、签名错误、nonce 重放等永久失败。

```http
HTTP 500
{"accepted": false, "code": "temporary_failure"}
```

仅用于服务暂时不可用、数据库故障或异步队列不可用，允许支付方重试。

如果签名有效但业务处理暂时失败，应保留事件记录和失败原因，利用队列或后台重试；不能简单丢弃请求。

## 处理伪代码

```pseudo
receive(request):
    request_id = newRequestId()
    raw_body = request.rawBytes

    if bodyTooLarge(raw_body):
        audit(REJECTED, "body_too_large")
        return 400

    headers = parseHeaders(request)
    if missingRequiredHeaders(headers):
        audit(REJECTED, "missing_header")
        return 400

    if abs(now() - headers.timestamp) > 300:
        audit(REJECTED, "timestamp_out_of_range")
        return 400

    key = keyStore.get(headers.key_id)
    canonical = buildCanonical(
        headers.timestamp,
        headers.nonce,
        headers.event_id,
        sha256(raw_body)
    )

    if !constantTimeEqual(
        hmacSha256(key.secret, canonical),
        decodeSignature(headers.signature)
    ):
        audit(REJECTED, "invalid_signature")
        return 400

    if !redis.setNX(
        "webhook:nonce:" + provider + ":" + headers.nonce,
        headers.event_id,
        ttl=600
    ):
        audit(REJECTED, "replay_nonce")
        return 400

    event = parseAndValidate(raw_body)
    if event.id != headers.event_id:
        audit(REJECTED, "event_id_mismatch")
        return 400

    transaction:
        existing = insertWebhookEventUnique(event, payloadHash, PROCESSING)

        if existing.status == SUCCEEDED:
            audit("IDEMPOTENT_HIT")
            return 200

        applyPaymentStateChange(event)
        markWebhookEventSucceeded(event.id)
        audit("SUCCEEDED")

    return 200
```

实际实现中，解析后的事件校验应在签名通过后进行；业务状态更新和事件状态更新必须处于同一数据库事务，或使用可靠的事务消息/outbox 机制。

## 测试设计

### 单元测试

- 正确签名通过。
- 修改 Body、timestamp、nonce、event_id 后签名失败。
- 签名比较为常量时间比较。
- 缺少 header、格式错误、Body 超限被拒绝。
- 时间戳刚好在窗口边界时的行为明确。
- 时间偏差超过窗口被拒绝。
- 不存在、撤销、过期的 key_id 被拒绝。
- canonicalization 对字段顺序和空白不敏感，因为它只使用原始 Body hash。

### 重放与幂等测试

- 相同 nonce 的第二次请求必拒绝。
- 不同 nonce、相同 event_id 不重复执行支付变更。
- 相同 event_id 但不同 Body hash 被拒绝并告警。
- 并发发送相同 event_id，只有一个请求能进入业务处理。
- Redis 超时或不可用时按明确策略失败关闭，不能绕过 nonce 校验。
- 处理成功后支付方重试返回 200，不产生第二笔支付。

### 集成和安全测试

- 数据库唯一约束在并发下仍生效。
- 业务事务回滚后事件可重试且不会形成错误状态。
- 密钥轮换期间旧、新 key 均按有效期正确处理。
- 审计记录覆盖接受、拒绝、重放、幂等命中和业务失败。
- 日志中无密钥、完整签名、卡号等敏感信息。
- 使用固定时间、随机 nonce、时钟漂移和网络重试进行端到端测试。
- 对接口执行速率限制和请求体限制，防止签名验证和存储被滥用。

关键验收标准是：任何未通过签名、时间窗口或 nonce 检查的请求都不会触发支付状态变更；任何已成功处理的 `event_id` 都不会再次产生业务副作用。