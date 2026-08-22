## 总体设计

采用“无状态实例 + 共享限流存储”的架构：

```text
请求
  -> Limiter 实例
      -> 生成限流 Key
      -> 读取策略快照
      -> Redis/其他共享存储执行原子限流
      -> 返回 Allow / RetryAfter
      -> 记录指标
```

每个服务实例只持有自己的依赖和配置快照，不使用进程级全局可变变量。多实例之间通过 Redis 等共享存储协调。

## 核心接口

```go
type Limiter interface {
    Allow(ctx context.Context, req Request) (Decision, error)
}

type Request struct {
    TenantID string
    Route    string
    Method   string
    Subject  string // 用户、API Key、IP 等
}

type Decision struct {
    Allowed   bool
    Limit     int
    Remaining int
    RetryAfter time.Duration
    Reason    string
}
```

通过构造函数注入依赖：

```go
func NewLimiter(
    store Store,
    policy PolicyProvider,
    clock Clock,
    metrics Metrics,
) *Limiter
```

`Store`、`Clock`、`Metrics` 都是接口，便于测试和替换实现。

## 策略配置

策略应支持多级匹配，优先级从高到低：

1. 租户 + 路由 + Subject
2. 租户 + 路由
3. 租户
4. 全局默认策略

示例：

```yaml
policies:
  - name: tenant-route
    match:
      tenant: acme
      route: /v1/orders
    algorithm: token_bucket
    capacity: 100
    refill_rate: 20
    window: 1s
    cost: 1

  - name: default
    match: {}
    algorithm: sliding_window
    limit: 1000
    window: 60s
```

配置对象解析后转为不可变结构：

```go
type Policy struct {
    Name       string
    Algorithm  Algorithm
    Capacity   int
    RefillRate float64
    Limit      int
    Window     time.Duration
    Cost       int
}
```

配置更新时创建新快照，再通过实例内部的 `atomic.Value` 替换，避免读请求加锁：

```go
type PolicyProvider interface {
    Snapshot() *PolicySnapshot
}
```

旧快照由读请求自然释放，不修改原对象。

## 限流算法

### 1. Token Bucket

适合控制平均速率，同时允许短时突发。

状态：

```text
tokens
last_refill_timestamp
```

Redis 中使用 Lua 脚本完成：

- 读取状态
- 按时间补充 token
- 判断是否足够
- 扣减 token
- 设置 TTL
- 返回剩余 token 和重试时间

所有操作在 Redis 单线程脚本中原子执行，避免多个服务实例并发竞争。

### 2. Sliding Window

适合严格的时间窗口限制。

可使用 Redis Sorted Set：

```text
key = rate:{policy}:{dimension}
score = request_timestamp
member = unique_request_id
```

Lua 脚本原子完成：

1. 删除窗口外记录
2. 统计当前窗口数量
3. 未超限则写入当前请求
4. 设置过期时间

高流量场景下，优先使用 Redis 原子计数器或滑动窗口计数器，避免 Sorted Set 过大。

## Key 设计

```text
rate:{policy_id}:{tenant_id}:{route}:{subject_hash}
```

注意：

- 对 Subject 做哈希，避免特殊字符破坏 Key。
- 限制 Key 长度。
- 不把原始 Token、邮箱等敏感信息写入存储。
- 明确区分环境、区域和版本，例如：

```text
rate:{env}:{region}:{policy}:{dimension}
```

## 并发安全

### 分布式安全

Redis Lua 脚本保证“检查 + 扣减”不可分割。

不要采用：

```text
GET -> 本地计算 -> SET
```

除非使用 `WATCH/MULTI` 或等价 CAS，否则在多实例下会超卖。

### 实例内部安全

实例内部只保存：

- 不可变配置快照
- Redis 客户端
- 指标对象
- 可选的有界本地缓存

如果使用本地缓存：

- 使用带容量上限的 LRU/TTL 缓存。
- 本地缓存只能做加速，不能作为最终限流依据。
- 不使用无界 `map`。
- 通过 `sync.RWMutex` 或并发安全缓存实现。

### Redis 故障策略

配置化选择：

```text
fail_open  : Redis 异常时放行
fail_closed: Redis 异常时拒绝
```

通常建议：

- 内部关键接口：`fail_closed`
- 面向用户的非关键接口：`fail_open`，但触发告警
- 增加 Redis 超时和熔断，避免限流模块拖垮业务请求

## 指标

指标接口由调用方注入，避免绑定具体监控系统：

```go
type Metrics interface {
    Allowed(policy string)
    Rejected(policy string, reason string)
    BackendError(policy string)
    ObserveLatency(policy string, d time.Duration)
}
```

建议指标：

```text
ratelimit_allowed_total
ratelimit_rejected_total
ratelimit_backend_errors_total
ratelimit_decision_latency_seconds
ratelimit_remaining
```

标签必须有界，避免将 `user_id`、完整 URL、Token 等作为标签。推荐标签：

```text
policy
route_template
algorithm
reason
```

可选日志字段：

```text
request_id
policy
dimension_hash
allowed
remaining
retry_after
```

不要记录敏感原始标识。

## 失败响应

HTTP 场景建议返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 2
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 2
```

响应体：

```json
{
  "code": "RATE_LIMITED",
  "message": "too many requests",
  "retry_after_seconds": 2
}
```

## 测试方案

### 单元测试

使用可控时钟和内存 Store：

- Token Bucket 初始容量
- token 补充和时间边界
- 单次 cost 大于剩余 token
- Sliding Window 窗口过期
- 多级策略匹配和优先级
- `RetryAfter` 计算
- 配置热更新
- 未配置策略时的默认行为

### 并发测试

- 多 goroutine 同时调用 `Allow`
- 验证总放行数不超过限制
- 使用 Go race detector 或等价工具
- 测试同一 Key 与不同 Key 的并发行为
- 测试配置更新与请求并发执行

### 分布式集成测试

启动多个 Limiter 实例，共享同一个 Redis：

```text
N 个实例并发发起 M 个请求
断言最终允许数 <= limit + 明确的算法误差
```

同时覆盖：

- Redis 重启
- 网络超时
- 脚本执行失败
- TTL 清理
- 多区域或不同实例时钟偏差

### 属性测试

对任意请求序列验证：

```text
允许数不会超过策略定义
拒绝后 RetryAfter 非负
时间单调前进时 token 不会减少
配置快照切换不会出现部分字段来自新旧配置的混合状态
```

## 推荐模块结构

```text
ratelimit/
  limiter.go          // 公共 API
  policy.go           // 策略模型与匹配
  token_bucket.go     // 算法实现
  sliding_window.go
  store.go            // Store 接口
  redis_store.go      // Redis 实现
  metrics.go          // 指标接口
  clock.go            // 时间接口
  errors.go
  limiter_test.go
  redis_integration_test.go
```

关键原则是：限流器实例化管理所有依赖，策略采用不可变快照，跨实例状态放入共享存储，并通过原子脚本完成决策。这样既能支持水平扩展，也能避免全局可变状态带来的测试隔离和并发问题。