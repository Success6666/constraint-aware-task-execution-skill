下面给出一个适合多实例部署的 API 限流模块设计。核心原则是：实例内状态只属于 `Limiter` 对象；跨实例共享的限额交给 Redis 等外部存储；配置、时钟、指标和存储均通过依赖注入提供。

## 1. 模块接口

以下以 Go 为例：

```go
type Request struct {
    Route   string
    Subject string // 用户、租户、API Key 等
    Cost    int64  // 默认 1
}

type Decision struct {
    Allowed   bool
    Remaining int64
    RetryAfter time.Duration
    PolicyID  string
}

type Limiter interface {
    Allow(ctx context.Context, req Request) (Decision, error)
}

type Policy struct {
    ID              string
    Route           string
    Scope           string // ip/user/tenant/api_key
    RequestsPerSec  int64
    Burst           int64
    CostEnabled     bool
    FailureMode     FailureMode // FailOpen / FailClosed
}

type Config struct {
    Policies []Policy
}
```

业务层使用：

```go
decision, err := limiter.Allow(ctx, Request{
    Route:   "GET /v1/orders",
    Subject: tenantID,
})

if err != nil {
    return err
}
if !decision.Allowed {
    w.Header().Set("Retry-After",
        strconv.FormatInt(int64(decision.RetryAfter.Seconds()), 10))
    http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
    return nil
}
```

## 2. 组件结构

```go
type Store interface {
    Allow(
        ctx context.Context,
        key string,
        rate int64,
        burst int64,
        cost int64,
    ) (Decision, error)
}

type Metrics interface {
    Allowed(policyID string)
    Rejected(policyID string)
    Error(policyID string)
}

type Clock interface {
    Now() time.Time
}

type Engine struct {
    config  atomic.Pointer[Config]
    store   Store
    metrics Metrics
    clock   Clock
    lookup  PolicyLookup
}
```

所有状态都挂在 `Engine` 实例上，没有包级可变变量：

```go
func NewEngine(
    cfg *Config,
    store Store,
    metrics Metrics,
    clock Clock,
) *Engine {
    e := &Engine{
        store:   store,
        metrics: metrics,
        clock:   clock,
        lookup:  NewPolicyLookup(cfg),
    }

    e.config.Store(cloneConfig(cfg))
    return e
}
```

配置热更新使用不可变快照：

```go
func (e *Engine) UpdateConfig(cfg *Config) {
    next := cloneConfig(cfg)
    e.config.Store(next)
    e.lookup.Update(next)
}
```

读路径只读取快照，不需要长时间持锁。

## 3. 多实例限流算法

### 推荐方案：Redis 原子令牌桶

每个限流键：

```text
ratelimit:{policy_id}:{subject}
```

Redis 中保存：

```text
tokens
timestamp_ms
```

通过 Lua 脚本一次性完成：

1. 读取当前 token 数和上次更新时间。
2. 按时间补充 token。
3. 判断是否足够支付本次请求成本。
4. 扣除 token。
5. 设置 TTL。
6. 返回允许结果、剩余 token 和重试时间。

伪代码：

```lua
local state = redis.call("HMGET", KEYS[1], "tokens", "ts")
local tokens = tonumber(state[1]) or ARGV[2]
local previous = tonumber(state[2]) or ARGV[3]

local now = tonumber(ARGV[3])
local elapsed = math.max(0, now - previous)
tokens = math.min(ARGV[2], tokens + elapsed * ARGV[1] / 1000)

local cost = tonumber(ARGV[4])
if tokens < cost then
    local retry_ms = math.ceil((cost - tokens) * 1000 / ARGV[1])
    return {0, math.floor(tokens), retry_ms}
end

tokens = tokens - cost
redis.call("HSET", KEYS[1], "tokens", tokens, "ts", now)
redis.call("PEXPIRE", KEYS[1], ARGV[5])
return {1, math.floor(tokens), 0}
```

Redis 脚本执行具有原子性，因此多个 API 实例之间不会发生超卖。

### 本地模式

可以提供 `LocalStore`，用于：

- 单实例部署；
- 开发和测试；
- Redis 不可用时的降级策略。

本地实现使用分片锁：

```go
type LocalStore struct {
    shards []localShard
}

type localShard struct {
    mu sync.Mutex
    m  map[string]*bucket
}
```

每个 `Engine` 创建自己的 `LocalStore`，不会污染其他实例。

## 4. 策略配置示例

```yaml
policies:
  - id: tenant-default
    route: "*"
    scope: tenant
    requests_per_sec: 100
    burst: 200
    failure_mode: fail_closed

  - id: order-create
    route: "POST /v1/orders"
    scope: tenant
    requests_per_sec: 10
    burst: 20
    failure_mode: fail_closed

  - id: public-read
    route: "GET /v1/catalog"
    scope: ip
    requests_per_sec: 50
    burst: 100
    failure_mode: fail_open
```

策略匹配建议：

1. 精确路由优先；
2. 方法加路径优先于路径通配；
3. 更具体的策略优先；
4. 同一优先级禁止重复配置；
5. 无匹配策略默认放行或使用显式默认策略。

## 5. 并发安全

需要保证：

- Redis 端通过 Lua 原子更新；
- 本地桶通过分片锁保护；
- 配置使用原子快照替换；
- `Metrics` 实现必须自身并发安全；
- `Clock` 可注入，避免测试依赖真实时间；
- 限流键必须规范化，避免不同实例生成不同 key；
- Redis key 设置 TTL，防止无限增长。

不要在限流路径中使用全局 `map`、全局配置指针或全局单例。

## 6. 指标设计

建议提供以下指标：

```text
api_rate_limit_allowed_total{policy}
api_rate_limit_rejected_total{policy,reason}
api_rate_limit_errors_total{policy,error}
api_rate_limit_decision_latency_seconds{policy}
api_rate_limit_backend_latency_seconds{backend}
```

注意：

- 不要把 `user_id`、IP、订单号作为 Prometheus label；
- `policy` 数量应有上限；
- `reason` 使用固定枚举，例如 `exhausted`、`backend_error`；
- 记录 `Retry-After` 时可使用日志或 tracing，而不是高基数指标。

## 7. 错误与降级策略

```go
type FailureMode int

const (
    FailOpen FailureMode = iota
    FailClosed
)
```

Redis 失败时：

- `FailClosed`：拒绝请求，适合写操作、计费接口、认证接口；
- `FailOpen`：允许请求，适合低风险读接口；
- 可选短时本地兜底桶，但必须限制容量和 TTL。

后端异常要与“正常被限流”区分统计，避免运维误判。

## 8. 测试方案

### 单元测试

覆盖：

- 令牌桶初始容量；
- 正常消耗和剩余 token；
- 补充速率；
- 突发流量；
- `cost > 1`；
- `RetryAfter` 计算；
- 策略匹配优先级；
- 配置热更新；
- `FailOpen` / `FailClosed`。

使用假的时钟：

```go
type FakeClock struct {
    mu  sync.Mutex
    now time.Time
}

func (c *FakeClock) Now() time.Time {
    c.mu.Lock()
    defer c.mu.Unlock()
    return c.now
}
```

### 并发测试

启动大量 goroutine 同时调用同一个 key，验证：

```text
允许数 <= burst + rate * elapsed
```

并检查：

- 无数据竞争；
- 不出现负 token；
- 配置更新期间请求结果始终来自完整快照。

使用：

```bash
go test -race ./...
```

### Redis 集成测试

使用 Testcontainers 或独立 Redis 测试实例：

- 多个 `Engine` 共享同一 Redis；
- 并发请求总允许数不超过全局额度；
- 脚本异常时验证降级模式；
- key TTL 正确设置；
- Redis 重启后的恢复行为。

### 属性测试

随机生成：

- 请求时间间隔；
- 请求成本；
- 并发顺序；
- 配置速率和 burst。

验证限流不变量，而不是只验证固定样例。

## 9. 部署建议

每个应用实例独立创建：

```text
HTTP Middleware
    -> Policy Lookup
    -> RedisStore
    -> Metrics Adapter
```

Redis 建议：

- 使用高可用部署；
- 限流脚本版本随应用发布；
- key 使用 hash tag，确保 Redis Cluster 下脚本涉及的 key 位于同一 slot；
- 设置合理连接池和超时；
- 将 Redis 延迟、错误率纳入告警。

这样可以同时满足多实例一致性、并发安全、可观测性和测试可控性，并避免依赖全局可变状态。