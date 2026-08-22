# API 限流模块设计

## 1. 目标与总体方案

采用**分布式 Token Bucket（令牌桶）**：

- Redis 作为跨实例共享状态存储。
- Redis Lua 脚本完成“读取、补充令牌、扣减、设置过期时间”原子操作。
- API 实例本身不保存权威限流状态，避免多实例之间不一致。
- 策略配置使用不可变快照，通过依赖注入传入。
- Redis Key 包含策略版本，策略变更后自动使用新桶。

结构：

```text
Request
  -> IdentityResolver
  -> PolicyResolver
  -> RateLimiter
       -> Redis Lua atomic operation
  -> Allow / 429
```

---

## 2. 策略配置

```text
RateLimitPolicy {
    name:             string
    version:          string
    scope:            enum { GLOBAL, TENANT, USER, API_KEY, IP }
    requestsPerSecond: decimal
    burst:            integer
    cost:             integer = 1
    enabled:          boolean = true
    failMode:         enum { OPEN, CLOSED }
}
```

策略匹配顺序建议：

1. API + 租户
2. API + 用户
3. API Key
4. IP
5. 全局默认策略

示例：

```json
{
  "name": "orders-create-tenant",
  "version": "2025-03-01",
  "scope": "TENANT",
  "requestsPerSecond": 100,
  "burst": 200,
  "cost": 1,
  "enabled": true,
  "failMode": "CLOSED"
}
```

配置要求：

- `requestsPerSecond > 0`
- `burst >= cost`
- `version` 每次策略变更时递增或更新。
- 配置加载后生成不可变快照。
- 不将用户 ID、IP 等高基数字段放入指标标签。

---

## 3. 限流 Key

```text
rl:{policyName}:{policyVersion}:{scope}:{identity}
```

示例：

```text
rl:orders-create-tenant:2025-03-01:TENANT:tenant-123
```

Redis Cluster 环境可使用 Hash Tag，确保单次脚本只访问一个槽位：

```text
rl:{orders-create-tenant:tenant-123}:2025-03-01
```

Key 中包含策略版本，可避免修改速率后旧桶状态污染新策略。

---

## 4. Redis Lua 原子算法

桶状态：

```text
tokens          当前令牌数
last_refill_ms  上次补充时间
```

伪代码：

```lua
local key = KEYS[1]

local rate = tonumber(ARGV[1])          -- 每秒补充令牌数
local capacity = tonumber(ARGV[2])      -- burst
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local now = redis.call("TIME")
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)

local tokens = tonumber(redis.call("HGET", key, "tokens"))
local last = tonumber(redis.call("HGET", key, "last_refill_ms"))

if tokens == nil then
    tokens = capacity
    last = now_ms
end

local elapsed = math.max(0, now_ms - last)
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

local allowed = 0
local retry_after_ms = 0

if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    retry_after_ms = math.ceil((cost - tokens) * 1000 / rate)
end

redis.call("HSET", key,
    "tokens", tokens,
    "last_refill_ms", now_ms
)
redis.call("PEXPIRE", key, ttl_ms)

return { allowed, tokens, retry_after_ms }
```

建议：

```text
ttl_ms = ceil(burst / requestsPerSecond * 1000) * 2
```

使用 Redis `TIME` 而非应用实例时间，避免多实例时钟偏差。

---

## 5. 核心接口

```text
interface RateLimiter {
    LimitResult check(
        Policy policy,
        String identity,
        int cost
    )
}
```

```text
LimitResult {
    boolean allowed
    long remaining
    Duration retryAfter
    String policyName
}
```

策略和依赖通过构造函数注入：

```text
RateLimiter(
    RedisClient redis,
    PolicyProvider policyProvider,
    IdentityResolver identityResolver,
    Clock clock,
    Metrics metrics
)
```

不得使用静态可变桶、静态缓存或进程级共享限流状态。

---

## 6. 请求处理行为

允许请求时：

```http
X-RateLimit-Limit: 200
X-RateLimit-Remaining: 199
```

拒绝请求时：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
X-RateLimit-Limit: 200
X-RateLimit-Remaining: 0
```

`Retry-After` 使用 Lua 返回值计算，向上取整为秒。

建议在网关或中间件层执行：

```text
resolve identity
resolve policy
check limiter
if denied:
    emit metrics
    return 429
continue request
```

同一请求只执行一次限流检查。

---

## 7. 并发安全

### Redis 层

- Lua 脚本是 Redis 单线程原子执行。
- 同一 Key 的并发请求不会发生超发。
- 不允许使用“先 GET、再 SET”的多次命令实现扣减。

### 应用层

- Policy 快照不可变。
- Redis 客户端连接池由框架管理，但限流器不持有全局可变桶。
- 不在实例本地缓存令牌数量。
- 可缓存策略配置，但缓存必须是实例依赖的一部分，并支持版本更新。

### Redis 故障策略

由策略配置决定：

- `OPEN`：Redis 异常时放行，适合非关键接口。
- `CLOSED`：Redis 异常时拒绝或返回服务降级，适合成本高、风险高的接口。

无论哪种模式，都记录后端错误指标。

---

## 8. 配置更新

配置源可以是配置中心、数据库或管理接口，提供：

```text
PolicySnapshot getSnapshot()
```

更新流程：

1. 加载并校验完整配置。
2. 创建新的不可变快照。
3. 原子替换当前实例引用。
4. 更新策略版本。
5. 新请求使用新版本 Key。

无需主动删除旧 Redis Key，TTL 到期后自动清理。

多实例最终一致时，短时间内可能存在不同版本策略。若要求严格一致，应将策略版本和内容放入统一配置中心，并在版本未同步时暂停启用新策略。

---

## 9. 指标

指标标签保持低基数：

```text
rate_limit_requests_total{
    policy,
    route,
    result="allowed|denied|backend_error"
}

rate_limit_backend_latency_seconds{
    backend="redis"
}

rate_limit_backend_errors_total{
    policy,
    error_type
}

rate_limit_check_duration_seconds{
    policy
}
```

可选：

```text
rate_limit_remaining
```

只建议做聚合、采样或直方图，不记录用户 ID、IP、API Key。

日志中可记录：

- 策略名和版本
- 路由
- 拒绝原因
- retry-after
- Redis 错误类型

敏感身份信息应脱敏或使用不可逆摘要。

---

## 10. 测试方案

### 单元测试

覆盖：

- 首次请求获得完整 burst。
- 连续请求正确扣减令牌。
- 令牌按时间补充。
- 超过容量时拒绝。
- `retryAfter` 计算正确。
- `cost > 1`。
- 策略禁用。
- 不同 identity 互不影响。
- 不同策略版本使用不同 Key。
- `OPEN` 和 `CLOSED` 故障模式。

### Lua/Redis 集成测试

覆盖：

- 并发请求不会超过 burst。
- 多个应用实例共享同一个限流桶。
- Redis Key 正确过期。
- Redis `TIME` 被正确使用。
- 速率为小数时结果正确。
- Redis 重启或连接失败时行为符合配置。

### 并发测试

启动多个限流器实例，同时向同一 identity 发起大量请求：

```text
总允许数 <= burst + 时间窗口内补充的令牌数
```

重点验证：

- 无双重扣减。
- 无超发。
- 不同 identity 之间不会互相阻塞。
- Redis Cluster 下 Key 位于同一槽位。

### 性能测试

测量：

- 限流检查 P50/P95/P99 延迟。
- Redis CPU 和网络开销。
- 高并发下拒绝比例。
- 策略数量和 identity 数量增长后的 Key 数量。

### 属性测试

对随机请求序列验证：

```text
任何时间段内，允许请求量
<= burst + rate * elapsed_seconds
```

并验证令牌数始终满足：

```text
0 <= tokens <= burst
```

这样即可在多实例部署下获得原子、一致、可观测且不依赖全局可变状态的 API 限流能力。
