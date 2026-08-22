建议采用“认证防重放层 + Webhook 收件箱 + 幂等业务处理器 + 审计日志”的分层设计。关键点是把传输层重放和业务事件重复明确区分：

- 相同 `nonce` 再次出现：视为请求重放，拒绝，不进入业务处理。
- 不同 `nonce` 但相同 `event_id`：通过认证后命中业务幂等，只返回既有处理结果，不重复执行支付业务。

## 接口协议

```http
POST /v1/webhooks/payments/{provider}

Content-Type: application/json
X-Webhook-Key-Id: key_2026_01
X-Webhook-Timestamp: 1786857600
X-Webhook-Nonce: 018f7b6c-...
X-Webhook-Event-Id: evt_123456
X-Webhook-Signature: v1=<hex-hmac-sha256>
```

签名原文使用固定、无歧义的格式：

```text
v1
{timestamp}
{nonce}
{event_id}
{HTTP_METHOD}
{request_path}
{sha256(raw_request_body)}
```

计算方式：

```text
signature = HMAC-SHA256(secret, signing_payload)
```

必须使用收到的原始请求字节计算正文摘要，不能先解析 JSON 再序列化。签名比较使用常量时间比较。

如果支付平台有自己的签名协议，应严格采用平台协议，不自行修改字段顺序或编码规则。

## 请求处理顺序

1. 限制请求方法、正文大小和内容类型。
2. 读取原始正文，同时计算正文 SHA-256。
3. 校验签名头、时间戳、`nonce`、`event_id` 的格式和长度。
4. 根据 `key_id` 查找当前或轮换期内的密钥。
5. 验证签名。
6. 验证时间戳在允许窗口内，例如服务器时间前后 5 分钟。
7. 原子写入 `nonce` 防重放记录；已存在则拒绝。
8. 解析并验证事件结构。
9. 原子创建或读取以 `(provider, event_id)` 唯一标识的收件箱记录。
10. 新事件进入业务处理；重复事件返回原有结果。
11. 业务状态变更和收件箱完成状态在同一数据库事务中提交。
12. 全程写入不可变审计事件。

校验顺序应避免泄露过多信息。对外可统一返回一般性错误，对内审计中保存具体拒绝原因。

## 返回语义

| 场景 | HTTP 状态 | 是否处理业务 |
|---|---:|---|
| 签名无效 | `401` | 否 |
| 未知或已停用的 `key_id` | `401` | 否 |
| 时间戳超限 | `401` | 否 |
| `nonce` 已使用 | `409` | 否 |
| 请求格式错误 | `400` | 否 |
| 首次收到有效事件 | `200` 或 `202` | 是 |
| 新 nonce、相同 event_id、此前成功 | `200` | 否，返回既有结果 |
| 相同 event_id、不同正文 | `409` | 否，标记安全告警 |
| 内部暂时故障 | `500`/`503` | 否或等待重试 |

不要在签名、时间戳或 nonce 校验失败时返回 `200`，否则上游可能错误地认为请求已被成功接收。

## 数据模型

```sql
CREATE TABLE webhook_nonces (
    provider       VARCHAR(64) NOT NULL,
    key_id         VARCHAR(128) NOT NULL,
    nonce          VARCHAR(128) NOT NULL,
    received_at    TIMESTAMP NOT NULL,
    request_id     UUID NOT NULL,
    PRIMARY KEY (provider, key_id, nonce)
);

CREATE TABLE webhook_inbox (
    provider       VARCHAR(64) NOT NULL,
    event_id       VARCHAR(128) NOT NULL,
    event_type     VARCHAR(128) NOT NULL,
    body_hash      CHAR(64) NOT NULL,
    status         VARCHAR(32) NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    result_code    VARCHAR(64),
    first_seen_at  TIMESTAMP NOT NULL,
    completed_at   TIMESTAMP,
    last_error     VARCHAR(512),
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE webhook_audit (
    audit_id       UUID PRIMARY KEY,
    request_id     UUID NOT NULL,
    provider       VARCHAR(64),
    event_id       VARCHAR(128),
    key_id         VARCHAR(128),
    nonce_hash     CHAR(64),
    body_hash      CHAR(64),
    decision       VARCHAR(32) NOT NULL,
    reason_code    VARCHAR(64),
    source_ip      VARCHAR(64),
    received_at    TIMESTAMP NOT NULL,
    metadata       JSON
);
```

`webhook_nonces` 的保留时间必须至少覆盖签名时间窗口，并留出时钟和队列延迟余量。例如时间窗口为 5 分钟，可保留 15 至 60 分钟。若需调查攻击行为，可只长期保存 nonce 的哈希。

`webhook_inbox` 不应过早删除，应至少覆盖支付平台可能重试和业务对账的周期。

## 原子防重放

不要采用“先查询 nonce，再插入”的实现，这会产生并发竞态。应依赖数据库唯一约束或支持原子 `SET NX` 的共享存储。

数据库示例：

```sql
INSERT INTO webhook_nonces (
    provider, key_id, nonce, received_at, request_id
) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
ON CONFLICT DO NOTHING;
```

影响行数为 `0` 时立即判定为重放。

如果使用 Redis：

```text
SET webhook:nonce:{provider}:{key_id}:{nonce} request_id NX EX 1800
```

Redis 必须是所有服务实例共享的，且其不可用时应默认拒绝请求或返回 `503`，不能绕过防重放检查继续处理。

## 核心伪代码

```text
handle(request):
    request_id = new_uuid()
    raw_body = read_raw_body_with_limit(request)

    headers = validate_required_headers(request.headers)
    if invalid:
        audit(REJECTED, MALFORMED_HEADERS)
        return 400

    timestamp = parse_integer(headers.timestamp)
    secret = key_store.find(provider, headers.key_id)

    if secret is absent or not active:
        audit(REJECTED, UNKNOWN_KEY)
        return 401

    payload = canonicalize(
        timestamp,
        nonce,
        event_id,
        request.method,
        request.path,
        sha256(raw_body)
    )

    if not constant_time_equal(
        decode_signature(headers.signature),
        hmac_sha256(secret, payload)
    ):
        audit(REJECTED, INVALID_SIGNATURE)
        return 401

    if abs(trusted_clock.now() - timestamp) > 5 minutes:
        audit(REJECTED, TIMESTAMP_OUT_OF_RANGE)
        return 401

    if not nonce_store.insert_if_absent(provider, key_id, nonce):
        audit(REJECTED, NONCE_REPLAY)
        return 409

    event = parse_and_validate(raw_body)
    if event.id != headers.event_id:
        audit(REJECTED, EVENT_ID_MISMATCH)
        return 400

    transaction:
        existing = inbox.lock_or_insert(
            provider,
            event.id,
            sha256(raw_body)
        )

        if existing.body_hash != sha256(raw_body):
            audit(REJECTED, EVENT_BODY_CONFLICT)
            return 409

        if existing.status == COMPLETED:
            audit(IDEMPOTENT_HIT, EVENT_ALREADY_PROCESSED)
            return 200 existing.result

        apply_payment_state_transition(event)
        mark_inbox_completed(existing)
        append_outbox_messages_if_needed()

    audit(ACCEPTED, PROCESSED)
    return 200
```

对于异步处理，可在验证签名、时间戳和 nonce 后，将事件写入持久化 inbox，并返回 `202`。消息队列发布应使用 transactional outbox，避免数据库已写入但消息未发布，或消息已发布但数据库回滚。

## 支付状态保护

幂等键只能防止重复调用，不能替代状态机校验。支付状态转换应有明确约束，例如：

```text
PENDING -> PAID
PENDING -> FAILED
PAID -> REFUNDED
```

禁止将已退款订单因迟到的 `payment.succeeded` 事件重新改为已支付。建议同时检查：

- 商户订单号和支付平台账户是否匹配
- 币种和金额是否匹配
- 事件类型是否允许当前状态转换
- 平台事件创建时间或序列号是否比已处理事件更新
- 平台支付对象 ID 是否与订单绑定记录一致

## 密钥管理

- 密钥存放在 KMS 或 Secret Manager，不写入代码、配置仓库或日志。
- 每个支付平台、环境和商户账户使用独立密钥。
- 支持 `key_id` 和双密钥轮换期。
- 密钥撤销应快速生效并产生审计记录。
- 日志中禁止记录密钥、完整签名、银行卡信息或完整请求正文。
- 服务器时间必须通过可靠的时间同步服务维护；时间异常时告警并默认停止接收。

## 审计要求

审计记录至少包含：

- 内部 `request_id`
- 平台、`key_id`、`event_id`
- nonce 哈希和正文哈希
- 接收时间和验证时间
- 接收、拒绝、幂等命中或处理失败的决策
- 标准化原因码
- 来源 IP、User-Agent、追踪 ID
- 处理耗时和最终业务结果

审计日志应追加写、限制访问、加密存储，并配置防篡改归档。正文只在确有合规需求时加密保存，并设置最短必要保留期。

## 测试矩阵

必须覆盖以下自动化测试：

- 正确签名、合法时间戳、新 nonce、新 event_id：只处理一次。
- 修改正文但沿用旧签名：`401`。
- 修改路径、event_id、timestamp 或 nonce：`401`。
- 签名长度错误、非十六进制、缺少头字段：拒绝且不抛出异常。
- 时间戳刚好位于窗口边界：行为明确。
- 时间戳超过窗口一秒：`401`。
- 未来时间超过窗口：`401`。
- 相同 nonce 串行重放：第二次 `409`。
- 相同 nonce 高并发请求：只有一个请求可通过 nonce 插入。
- 新 nonce、相同 event_id：业务只执行一次。
- 相同 event_id、不同正文哈希：`409` 并告警。
- 多实例并发接收：仍只处理一次。
- 业务事务回滚：支付状态和 inbox 状态保持一致。
- 服务在提交前、提交后分别崩溃：恢复后不产生重复付款或重复退款。
- Redis、数据库、密钥服务不可用：默认不继续处理。
- 密钥轮换：新旧密钥在过渡期有效，撤销后旧密钥立即失败。
- 原始 JSON 中字段顺序、空格和 Unicode 表达不同：按原始字节正确验签。
- 日志检查：不包含密钥、完整签名和敏感支付数据。

验收标准应包括：在并发和故障注入测试下，每个 `(provider, event_id)` 的支付副作用最多发生一次；任何签名无效、时间超限或 nonce 重放请求产生零业务副作用，并且存在可追溯的拒绝审计记录。