# 支付 Webhook 接收服务设计

## 1. 接口

```http
POST /api/v1/payment/webhooks/{provider}
Content-Type: application/json
X-Webhook-Key-Id: key_2025_01
X-Webhook-Timestamp: 1735689600
X-Webhook-Nonce: 01J...
X-Webhook-Signature: v1=base64(hmac_sha256(...))
X-Request-Id: optional-client-id
```

请求体必须使用原始字节参与签名，不能先 JSON 解析再重新序列化。

推荐响应：

| 场景 | HTTP | 说明 |
|---|---:|---|
| 接收成功并进入处理队列 | 202 | 已持久化、尚未完成业务处理 |
| 已处理过的相同业务事件 | 200 | 不重复执行业务，可停止上游重试 |
| 签名无效 | 401 | 必须拒绝处理 |
| 时间戳超限 | 401 | 必须拒绝处理 |
| nonce 重放 | 409 | 必须拒绝处理 |
| 请求格式错误 | 400 | 必须拒绝处理 |
| 系统暂时不可用 | 503 | 未确认接收，允许上游重试 |

---

## 2. 签名协议

### 2.1 待签名字符串

```text
v1:{timestamp}:{nonce}:{sha256_hex(raw_body)}
```

例如：

```text
v1:1735689600:01JABCDEF:7f83b1657ff1fc53b92dc18148a1d65dfa135e...
```

签名计算：

```text
signature = Base64(
    HMAC-SHA256(
        secret,
        signing_string
    )
)
```

请求头格式：

```text
X-Webhook-Signature: v1=<base64-signature>
```

### 2.2 验证规则

1. 读取原始请求体。
2. 校验必需请求头存在、长度合理、字符集合法。
3. 根据 `X-Webhook-Key-Id` 查找当前或仍在轮换期内的密钥。
4. 计算请求体 SHA-256。
5. 构造待签名字符串。
6. 使用常量时间比较比较签名，禁止普通字符串比较。
7. 签名验证失败时：
   - 不进入业务处理；
   - 不写入幂等成功记录；
   - 记录安全审计事件；
   - 返回 `401`。

密钥不能出现在日志、响应、审计明文或异常堆栈中。

### 2.3 密钥轮换

保存：

```text
provider
key_id
secret_ciphertext
status: active | verifying_only | revoked
valid_from
valid_to
```

验证时允许当前密钥和轮换期内的旧密钥，生成签名时只使用当前密钥。密钥应存放在 KMS 或 Secret Manager 中。

---

## 3. 时间戳防护

服务端记录接收时间：

```text
now = server_utc_unix_seconds()
delta = abs(now - timestamp)
```

默认允许偏差：

```text
delta <= 300 seconds
```

超出范围时：

- 不验证 nonce；
- 不进入业务处理；
- 记录 `TIMESTAMP_OUT_OF_RANGE`；
- 返回 `401`。

所有服务节点必须使用同步的 UTC 时间，部署 NTP 或等效时间同步机制。

时间窗口应配置化，例如：

```yaml
webhook:
  max_clock_skew_seconds: 300
```

---

## 4. nonce 防重放

### 4.1 存储

使用 Redis 或具备原子唯一约束的高速存储：

```text
key: webhook:nonce:{provider}:{key_id}:{nonce}
value: request_hash
ttl: 600 seconds
```

TTL 应大于允许时间偏差，例如允许偏差 300 秒时设置为 600 秒。

### 4.2 原子校验

签名和时间戳验证成功后执行：

```text
SET webhook:nonce:{provider}:{key_id}:{nonce} request_hash NX EX 600
```

结果：

- `OK`：首次出现，继续处理；
- `NOT-OK`：nonce 已存在，视为重放，返回 `409`；
- Redis 不可用：不能降级放行，返回 `503`，否则无法保证重放防护。

nonce 必须限制长度和字符集，例如最多 128 个 ASCII 字符。

### 4.3 并发行为

两个相同 nonce 的并发请求中，只允许一个请求成功执行 `SET NX`。另一个请求必须被拒绝，不能依赖应用层先查后写。

---

## 5. 业务幂等

nonce 防止同一请求被重复发送；业务幂等防止同一支付事件因不同 nonce、网络重试或上游重新签名而重复执行业务。

要求上游提供：

```json
{
  "event_id": "evt_123456",
  "event_type": "payment.succeeded",
  "occurred_at": "2025-01-01T00:00:00Z",
  "data": {
    "payment_id": "pay_123",
    "amount": 1000,
    "currency": "CNY"
  }
}
```

### 5.1 Inbox 表

```sql
CREATE TABLE webhook_inbox (
    id              BIGINT PRIMARY KEY,
    provider        VARCHAR(64) NOT NULL,
    event_id        VARCHAR(128) NOT NULL,
    event_type      VARCHAR(128) NOT NULL,
    payload_hash    CHAR(64) NOT NULL,
    raw_payload     JSONB NOT NULL,
    status          VARCHAR(32) NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    processed_at    TIMESTAMP NULL,
    last_error      VARCHAR(512) NULL,
    UNIQUE (provider, event_id)
);
```

`status`：

```text
RECEIVED
PROCESSING
SUCCEEDED
FAILED_RETRYABLE
FAILED_FINAL
```

### 5.2 幂等规则

在数据库事务中插入 inbox：

```sql
INSERT INTO webhook_inbox (...)
VALUES (...)
ON CONFLICT (provider, event_id) DO NOTHING;
```

- 首次插入：写入 `RECEIVED`，发布处理任务；
- 已存在且 `payload_hash` 相同：
  - 不重复执行业务；
  - 已成功则返回 `200`；
  - 处理中或可重试失败则返回 `202`；
- 已存在但 `payload_hash` 不同：
  - 记录 `EVENT_ID_PAYLOAD_CONFLICT`；
  - 返回 `409`；
  - 不得覆盖原始事件。

业务处理必须在同一事务中完成状态变更和业务幂等控制，或使用可靠 Outbox 保证消息不会丢失。

---

## 6. 请求处理流程

```text
接收原始 HTTP 请求
        |
        v
限制请求大小、校验请求头和 Content-Type
        |
        v
解析 timestamp / nonce / signature
        |
        v
校验 timestamp 是否在允许窗口内
        | 失败 -> 审计 + 401
        v
根据 key_id 获取密钥
        |
        v
计算 raw_body hash 和 HMAC
        | 失败 -> 审计 + 401
        v
原子写入 nonce
        | 已存在 -> 审计 + 409
        v
解析 JSON 和 event_id
        | 失败 -> 审计 + 400
        v
数据库插入 webhook_inbox
        |
        +-- 已有相同 event_id -> 按幂等规则返回
        |
        v
提交事务并投递处理任务
        |
        v
返回 202
```

签名验证必须基于原始请求体；JSON 解析只用于业务字段读取，不能影响签名验证结果。

---

## 7. 审计设计

建立独立的不可变审计记录：

```sql
CREATE TABLE webhook_audit (
    id                  BIGINT PRIMARY KEY,
    request_id          VARCHAR(128) NOT NULL,
    provider            VARCHAR(64) NOT NULL,
    key_id              VARCHAR(128),
    event_id            VARCHAR(128),
    nonce_hash          CHAR(64),
    payload_hash        CHAR(64),
    received_at         TIMESTAMP NOT NULL,
    timestamp_value     BIGINT,
    clock_skew_seconds  BIGINT,
    result              VARCHAR(32) NOT NULL,
    reason              VARCHAR(64) NOT NULL,
    source_ip_hash      CHAR(64),
    user_agent           VARCHAR(512),
    created_at          TIMESTAMP NOT NULL
);
```

`result` 示例：

```text
ACCEPTED
REJECTED
DUPLICATE
ERROR
```

`reason` 示例：

```text
SIGNATURE_INVALID
KEY_NOT_FOUND
TIMESTAMP_OUT_OF_RANGE
NONCE_REPLAY
MALFORMED_REQUEST
EVENT_ID_DUPLICATE
EVENT_ID_PAYLOAD_CONFLICT
ACCEPTED_FOR_PROCESSING
```

审计要求：

- 所有拒绝请求都必须记录；
- 记录哈希而非完整签名、密钥和敏感支付数据；
- 原始 payload 仅在受控的 inbox 表保存，并按数据保留策略清理；
- 审计记录不可被普通业务更新或删除；
- 记录 `request_id`，若客户端未提供则服务端生成；
- 监控签名失败、重放、时间偏差、队列积压和处理失败数量。

---

## 8. 业务处理器要求

消费者处理 `webhook_inbox`：

1. 使用数据库锁或状态条件更新抢占任务：

```sql
UPDATE webhook_inbox
SET status = 'PROCESSING'
WHERE id = ?
  AND status IN ('RECEIVED', 'FAILED_RETRYABLE');
```

2. 业务更新必须带业务幂等条件，例如：

```sql
INSERT INTO payment_event_effects (provider, event_id)
VALUES (?, ?)
ON CONFLICT DO NOTHING;
```

3. 只有成功完成业务事务后才设置 `SUCCEEDED`。
4. 可重试异常设置 `FAILED_RETRYABLE`。
5. 数据不合法、金额不匹配、状态非法等设置 `FAILED_FINAL` 并告警。
6. 不因重复投递再次发货、记账、退款或发送通知。

---

## 9. 伪代码

```python
def receive_webhook(request):
    request_id = request.header("X-Request-Id") or uuid4()
    raw_body = request.raw_body

    if len(raw_body) > MAX_BODY_SIZE:
        audit(request, request_id, "REJECTED", "BODY_TOO_LARGE")
        return 400

    headers = parse_headers(request)

    timestamp = parse_integer(headers.timestamp)
    if timestamp is None:
        audit(request, request_id, "REJECTED", "INVALID_TIMESTAMP")
        return 401

    skew = abs(current_unix_seconds() - timestamp)
    if skew > MAX_CLOCK_SKEW:
        audit(request, request_id, "REJECTED",
              "TIMESTAMP_OUT_OF_RANGE", skew=skew)
        return 401

    secret = secret_store.get(headers.key_id)
    if secret is None:
        audit(request, request_id, "REJECTED", "KEY_NOT_FOUND")
        return 401

    body_hash = sha256_hex(raw_body)
    signing_string = f"v1:{timestamp}:{headers.nonce}:{body_hash}"
    expected = base64(hmac_sha256(secret, signing_string))

    if not constant_time_equal(headers.signature, "v1=" + expected):
        audit(request, request_id, "REJECTED", "SIGNATURE_INVALID")
        return 401

    if not redis.set(
        nonce_key(provider, headers.key_id, headers.nonce),
        body_hash,
        nx=True,
        ex=NONCE_TTL
    ):
        audit(request, request_id, "REJECTED", "NONCE_REPLAY")
        return 409

    payload = parse_json(raw_body)
    event_id = validate_event(payload)

    result = inbox.insert_idempotently(
        provider=provider,
        event_id=event_id,
        event_type=payload["event_type"],
        payload_hash=body_hash,
        raw_payload=payload
    )

    if result.conflict:
        audit(request, request_id, "REJECTED",
              "EVENT_ID_PAYLOAD_CONFLICT")
        return 409

    if result.duplicate:
        audit(request, request_id, "DUPLICATE",
              "EVENT_ID_DUPLICATE")
        return 200

    queue.publish(result.inbox_id)

    audit(request, request_id, "ACCEPTED",
          "ACCEPTED_FOR_PROCESSING")
    return 202
```

---

## 10. 必须覆盖的测试

### 签名

- 正确签名通过；
- 修改一个 JSON 字节后签名失败；
- 修改 timestamp、nonce 或 key ID 后失败；
- 使用错误密钥失败；
- 缺少签名头失败；
- 验证使用原始 body，而不是重新序列化后的 JSON；
- 比较使用常量时间函数。

### 时间戳

- 当前时间通过；
- 正好在允许边界通过；
- 超出边界被拒绝；
- 未来时间超限被拒绝；
- 非数字、负数、过长值被拒绝。

### nonce 重放

- 同一 nonce 的第一次请求通过；
- 第二次请求被拒绝；
- 并发发送相同 nonce 时仅一个通过；
- nonce TTL 到期后可按协议再次使用；
- Redis 不可用时返回 `503`，不能放行。

### 幂等

- 相同 `event_id`、相同 payload 不重复执行业务；
- 相同 `event_id`、不同 payload 被拒绝；
- 不同 nonce 的同一事件仍只产生一次业务效果；
- 消费者重复执行不会重复扣款、退款或发货；
- 消费者失败后可安全重试。

### 审计和安全

- 所有拒绝原因均有审计记录；
- 审计不包含密钥、完整签名和敏感支付字段；
- 请求体超限、JSON 非法、未知事件类型均被正确拒绝；
- 日志和审计中的 nonce、IP 等按规定脱敏或哈希；
- 密钥轮换期间新旧密钥行为符合预期。

### 集成与并发

- 多实例部署下 nonce 和幂等仍然有效；
- 数据库提交成功但消息投递失败时，Outbox 或补偿任务可以恢复；
- 消息重复投递不会产生重复业务效果；
- 队列积压和处理失败能被监控和告警。
