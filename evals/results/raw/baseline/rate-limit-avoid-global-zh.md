# API 限流模块设计

## 1. 目标与边界

支持：

- 多实例部署下的全局限流
- 按租户、用户、API、IP、路由等维度配置
- 多级策略叠加
- QPS、突发流量、并发数限制
- 配置热更新
- 并发安全
- 指标、日志、响应头
- Redis 不可用时可配置降级行为
- 不依赖全局可变状态

核心原则：

- 限流状态放在共享存储中，实例本地只保存不可变配置和临时对象。
- 所有状态变更必须是原子操作。
- 配置、时钟、存储、指标、日志均通过依赖注入提供。
- 默认使用 Redis + Lua 保证多实例原子性。

---

## 2. 核心接口

```text
Limiter
  check(ctx, RequestContext) -> Decision
```

```text
RequestContext {
  tenantId: string
  userId: string?
  clientIp: string?
  method: string
  route: string
  apiName: string
  cost: integer = 1
  metadata: map
}
```

```text
Decision {
  allowed: boolean
  reason: Allowed | RateExceeded | ConcurrencyExceeded | InvalidRequest
  retryAfter: duration?
  limit: integer?
  remaining: integer?
  resetAfter: duration?
}
```

```text
PolicyProvider
  current() -> PolicySnapshot

StateStore
  eval(script, keys, args) -> StoreResult

MetricsSink
  increment(name, value, labels)
  observe(name, value, labels)

Clock
  now() -> timestamp
```

`Limiter` 实例通过构造函数接收这些依赖，不使用包级单例或全局可变变量。

---

## 3. 策略模型

```yaml
policies:
  - name: tenant-default
    priority: 100
    match:
      tenant: "*"
      route: "*"
    key:
      - tenant
      - route
    algorithm: token_bucket
    rate: 100
    period: 1s
    burst: 200
    cost: request

  - name: user-export
    priority: 200
    match:
      route: "POST /exports"
    key:
      - tenant
      - user
      - route
    algorithm: token_bucket
    rate: 2
    period: 1s
    burst: 5
    max_concurrency: 2
    on_store_error: reject
```

### 策略字段

- `name`：唯一名称
- `priority`：匹配优先级
- `match`：租户、用户、路由、方法、IP 等匹配条件
- `key`：限流身份组成字段
- `algorithm`：`token_bucket` 或 `sliding_window`
- `rate`：周期内补充的令牌数
- `period`：补充周期
- `burst`：最大令牌容量
- `cost`：固定值或由请求计算
- `max_concurrency`：可选的并发限制
- `on_store_error`：`reject` 或 `allow`
- `enabled`：是否启用
- `version`：配置版本

同一请求可以匹配多个策略，必须全部通过才允许执行。这样可以同时实现：

- 全局限制
- 租户限制
- 用户限制
- 特定 API 限制

策略解析结果应在配置发布时预编译，避免每次请求解析通配符或正则表达式。

---

## 4. 限流算法

### 推荐：分布式 Token Bucket

状态：

```text
tokens: 当前令牌数
timestamp: 上次更新时间
```

每次请求：

```text
elapsed = now - timestamp
refilled = elapsed * rate / period
tokens = min(burst, tokens + refilled)
allowed = tokens >= cost
if allowed:
    tokens -= cost
```

Redis Lua 脚本必须一次完成：

1. 读取状态
2. 根据当前时间补充令牌
3. 判断是否允许
4. 写回状态
5. 设置 TTL
6. 返回 allowed、remaining、retryAfter 等结果

Lua 脚本内不能依赖 Redis 之外的共享状态。时间戳由应用传入，便于测试；生产环境也可以使用 Redis `TIME`，避免实例时钟偏差。

### Key 设计

```text
rl:v1:{policyName}:{hash(identity)}
```

例如：

```text
rl:v1:tenant-default:tenant_42|POST:/orders
```

注意：

- 对身份内容做规范化和哈希，防止 Key 过长。
- 多租户场景应使用 Redis Cluster hash tag，例如：
  `rl:v1:{tenant_42}:policy_hash`
- 不将原始 IP、用户标识直接写入日志或指标标签。
- TTL 至少为：
  `max(period * burst / rate, period) + safety_margin`

---

## 5. 并发限制

速率限制不等于并发限制，应单独实现。

### 单实例并发

使用实例内的 semaphore，仅作为本实例优化。它不能代表集群总并发数。

### 集群并发

使用 Redis 租约：

```text
concurrency key = cc:v1:{policy}:{identity}
```

获取流程：

1. Lua 中检查当前占用数。
2. 小于 `max_concurrency` 时写入唯一 request token。
3. 为 token 设置 TTL。
4. 请求完成后通过 Lua 按 token 释放。
5. 超时请求由 TTL 自动回收。

推荐存储结构：

```text
Redis Hash:
  token -> expirationTimestamp
```

Lua 脚本负责清理过期 token、判断容量、写入新 token。释放操作必须校验 token，避免一个请求错误释放另一个请求的租约。

请求上下文中保存：

```text
Lease {
  key
  token
  acquired: boolean
}
```

无论业务成功、失败还是超时，都必须在 `finally/defer` 中释放。

---

## 6. 请求处理流程

```text
1. 读取当前 PolicySnapshot
2. 提取并规范化 RequestContext
3. 匹配策略
4. 计算每个策略的限流 Key 和 cost
5. 批量执行所有 Token Bucket 检查
6. 若任一策略拒绝，返回 429
7. 获取所有并发租约
8. 若任一并发策略拒绝，释放已获取租约并返回 429
9. 执行业务处理
10. 在 finally/defer 中释放并发租约
11. 记录指标和必要日志
```

为避免部分成功：

- 多个速率策略应使用一个 Lua 脚本批量处理，或设计回滚脚本。
- 如果分别调用多个脚本，必须具备失败回滚能力，否则可能出现“请求被拒绝但令牌已消耗”。
- 并发租约获取失败时，必须释放之前已获取的租约。

---

## 7. 配置与热更新

配置对象设计为不可变快照：

```text
PolicySnapshot {
  version
  policies: immutable list
  compiledMatchers
  createdAt
}
```

`Limiter` 内部持有一个原子引用：

```text
AtomicReference<PolicySnapshot>
```

更新时：

1. 读取新配置。
2. 校验字段、范围、重复策略和冲突。
3. 预编译匹配器。
4. 构造完整的新快照。
5. 原子替换引用。

请求只读取快照，不修改快照。旧快照由正在执行的请求自然释放。

必须校验：

- `rate > 0`
- `period > 0`
- `burst >= 1`
- `cost > 0`
- `max_concurrency >= 1`
- Key 维度合法
- 策略名称唯一
- 版本单调递增
- 正则或表达式复杂度受限

---

## 8. Redis 故障策略

每个策略单独配置：

```text
on_store_error: reject | allow
```

建议：

- 认证、计费、写操作：`reject`
- 非关键读接口：可 `allow`
- 默认行为：`reject`，避免 Redis 故障导致流量失控

故障时返回：

- 对外统一返回 `503` 或内部定义的限流依赖错误
- 不把 Redis 错误详情暴露给客户端
- 记录 `limiter_store_error`
- 使用熔断和短超时，避免限流模块拖垮业务线程

不建议自动切换到本地限流作为“等价替代”，因为多实例下会改变实际配额。若需要本地保护，应明确标记为独立的过载保护层。

---

## 9. HTTP 响应约定

允许请求可返回：

```http
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 42
X-RateLimit-Reset: 1
```

拒绝请求：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
```

响应体统一，例如：

```json
{
  "code": "RATE_LIMITED",
  "message": "too many requests",
  "retry_after_ms": 850
}
```

不要将内部策略名称、Redis Key 或租户信息返回给客户端。

---

## 10. 指标设计

指标标签必须有限且可控，禁止使用用户 ID、IP、完整 URL 等高基数字段。

建议指标：

```text
api_limiter_requests_total{
  result="allowed|rate_limited|concurrency_limited|store_error",
  route_group,
  policy_group
}

api_limiter_tokens_consumed_total{
  policy_group
}

api_limiter_decision_latency_seconds{
  result
}

api_limiter_store_latency_seconds{
  operation
}

api_limiter_store_errors_total{
  operation
}

api_limiter_active_concurrency{
  policy_group
}
```

日志字段：

- request ID
- route group
- policy group
- decision
- retry after
- config version
- store latency

日志中避免记录完整身份 Key。

---

## 11. 测试方案

### 单元测试

覆盖：

- 令牌初始容量
- 令牌补充
- 突发流量
- `cost > 1`
- 精确边界时间
- 多策略全部通过
- 任一策略拒绝
- 配置匹配优先级
- 配置热更新的原子可见性
- Key 规范化和哈希
- `retryAfter` 计算
- 并发租约获取、释放和重复释放
- 租约 TTL 回收
- Redis 错误时 `allow/reject`
- 请求异常时租约释放

使用可控 `FakeClock`，禁止在测试中依赖真实等待。

### 并发测试

- 多线程同时消耗同一个 Key，允许数不得超过容量。
- 多实例模拟同一个 Redis 状态。
- 并发租约不能超过 `max_concurrency`。
- 配置更新和请求检查同时进行，不得出现半旧半新的配置。
- 高并发下不得出现负令牌数或重复释放。

### 集成测试

使用真实 Redis 或 Redis Testcontainer：

- 多个 limiter 实例共享限流状态。
- Lua 脚本在 Cluster 模式下运行。
- Redis 重启、超时、连接池耗尽。
- Key TTL 正确设置。
- 脚本返回值与业务层 Decision 一致。

### 属性测试

可验证以下不变量：

```text
tokens >= 0
tokens <= burst
allowed 请求的累计 cost 不超过可生成令牌总量 + burst
active concurrency <= max_concurrency
release 不会增加不存在的租约
```

### 压力测试

记录：

- P50/P95/P99 限流决策延迟
- Redis CPU、内存、命令延迟
- 限流模块自身 CPU 和内存
- 429 比例
- Redis 错误时业务吞吐和失败比例

---

## 12. 推荐默认值

```text
algorithm: token_bucket
cost: 1
on_store_error: reject
request timeout: 50ms
redis command timeout: 20ms
max policy count per request: 8
max key length: 256 bytes
default burst: rate * 2
```

最终模块应由应用显式创建：

```text
limiter = NewLimiter(
  policyProvider,
  redisStateStore,
  metricsSink,
  logger,
  clock,
)
```

这样可以避免全局可变状态，支持独立测试、多个限流实例并存，以及在单元测试中替换时间、存储和指标实现。
