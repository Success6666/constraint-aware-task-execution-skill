设计一个“实例无状态、限流状态外置”的模块，推荐以 Redis 作为多实例共享存储；单实例或测试环境可注入内存实现。核心对象不依赖包级全局变量，所有状态通过构造函数传入。

**1. 模块边界**

```text
API Handler
    -> RateLimiter.Allow(ctx, RequestIdentity, Cost)
        -> PolicyProvider.Get(route, tenant)
        -> CounterStore.Consume(key, policy, cost)
        -> MetricsRecorder.Record(result)
```

核心接口可以定义为：

```go
type RateLimiter interface {
    Allow(ctx context.Context, req Request, cost int64) (Decision, error)
}

type PolicyProvider interface {
    Get(ctx context.Context, route, tenant string) (Policy, error)
}

type CounterStore interface {
    Consume(ctx context.Context, key string, p Policy, cost int64) (StoreResult, error)
}

type MetricsRecorder interface {
    Record(ctx context.Context, event LimitEvent)
}
```

`RateLimiter` 只持有显式依赖：

```go
type Limiter struct {
    policies PolicyProvider
    store    CounterStore
    metrics  MetricsRecorder
    clock    Clock
}
```

不要在包级变量中保存 limiter、配置、计数器或 Redis 客户端。

**2. 策略配置**

```go
type Policy struct {
    Name       string
    Algorithm  Algorithm // token_bucket, fixed_window, sliding_window
    Rate       float64   // 每秒补充或允许的请求数
    Burst      int64
    Window     time.Duration
    KeyBy      []KeyPart // ip, user, tenant, route, api_key
    Cost       int64
    TTL        time.Duration
    FailMode   FailMode // open, closed
    Enabled    bool
}
```

示例：

```yaml
policies:
  public-api:
    algorithm: token_bucket
    rate: 100
    burst: 200
    key_by: [tenant, route]
    ttl: 10m
    fail_mode: open

  login:
    algorithm: sliding_window
    limit: 20
    window: 1m
    key_by: [ip, route]
    fail_mode: closed
```

配置来源可实现为：

- 静态配置：启动时加载；
- 数据库或配置中心：定时刷新；
- 配置中心 Watch：变更时生成新的不可变快照。

配置更新采用“替换快照”，而不是原地修改共享对象：

```go
type SnapshotProvider struct {
    current atomic.Pointer[ConfigSnapshot]
}
```

这样读路径无锁，旧请求仍能安全使用旧策略。

**3. 多实例一致性**

生产环境使用 Redis 实现 `CounterStore`：

- 每个限流键对应一个 Redis key；
- 使用 Lua 脚本完成“读取、计算、写回、设置 TTL”；
- 脚本在 Redis 内原子执行，避免多个实例并发覆盖；
- 使用 Redis `TIME`，不要使用各应用实例的本地时间；
- Redis Cluster 下使用稳定的 key hash tag，例如：

```text
rl:{tenant:123}:route:/v1/orders
```

Token Bucket 的 Lua 操作逻辑：

```text
1. 读取 tokens 与 last_timestamp
2. 按 Redis 当前时间补充 token
3. 判断 tokens >= cost
4. 扣减 token 或返回拒绝
5. 写回状态并设置 TTL
6. 返回 allowed、remaining、retry_after
```

多实例部署时，应用实例本身不保存限流计数，因此扩容、缩容和请求转移不会改变配额。

Redis 不可用时由 `FailMode` 决定：

- `open`：允许请求，记录错误指标；
- `closed`：拒绝请求，返回 503 或明确的限流依赖错误。

不建议静默切换到本地计数器，否则会导致每个实例都拥有独立配额，实际限额随实例数放大。

**4. 单实例内存实现**

内存实现只用于开发、测试或明确的单实例场景：

```go
type MemoryStore struct {
    shards []memoryShard
}

type memoryShard struct {
    mu sync.Mutex
    m  map[string]Bucket
}
```

按 key hash 到固定 shard，每个 shard 使用独立锁，避免一个全局大锁。桶状态在锁内完成读改写，定期清理过期键，防止内存无限增长。

**5. 返回结果与 HTTP 行为**

```go
type Decision struct {
    Allowed    bool
    Remaining  int64
    Limit      int64
    RetryAfter time.Duration
    PolicyName string
}
```

拒绝时：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 3
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 3
```

建议区分：

- 配额不足：429；
- Redis 或配置依赖故障：根据 `FailMode` 返回放行或 503；
- 策略不存在：启动时校验，运行时按明确的默认策略处理。

**6. 指标**

指标名称保持固定，避免把用户 ID、IP、完整 URL 等高基数字段放进标签。

```text
rate_limit_requests_total{
  policy, route, result
}

rate_limit_rejected_total{
  policy, route, reason
}

rate_limit_store_latency_seconds{
  backend, operation
}

rate_limit_store_errors_total{
  backend, operation
}

rate_limit_config_version{
  source
}
```

`result` 可取 `allowed`、`rejected`、`fail_open`、`fail_closed`。日志中可以记录脱敏后的 key hash，但不要记录原始 API Key 或 IP。

**7. 测试设计**

单元测试：

- token bucket 补充、扣减、突发容量；
- fixed/sliding window 边界；
- 多维 key 生成和策略匹配；
- `Retry-After` 计算；
- `fail_open` / `fail_closed`；
- 配置快照替换时读请求的一致性。

并发测试：

- 多 goroutine 同时消费同一个 key；
- 验证成功次数不超过 burst + replenished tokens；
- 使用 race detector；
- 内存 store 的 shard 锁竞争测试。

Redis 集成测试：

- 多个 limiter 实例共享同一 Redis；
- 并发请求总量不超过全局配额；
- Lua 脚本原子性；
- TTL、Redis 重启和网络错误行为；
- Redis Cluster key hash tag。

属性测试：

```text
任何时间段内：
accepted_cost <= configured_quota + burst
remaining >= 0
rejected 请求不会改变可用 token
```

压测重点：

- 高并发同 key；
- 高并发不同 key；
- Redis 延迟升高时的 P95/P99；
- 配置热更新期间的吞吐和错误率。

**推荐落地顺序**

先实现 `Policy`、`RateLimiter` 和 `MemoryStore`，完成算法及并发单测；再实现 Redis Lua `CounterStore` 和跨实例集成测试；最后补齐指标、配置热更新和故障模式。这样核心逻辑可在无外部依赖的情况下验证，同时生产部署保持实例无状态。