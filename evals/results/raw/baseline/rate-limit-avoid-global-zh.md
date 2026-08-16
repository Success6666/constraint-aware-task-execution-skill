可以将限流模块设计成“无全局可变状态、存储可插拔、策略可热更新”的组件。多实例部署时由共享存储承担全局配额，进程内状态只作为可选优化。

## 1. 总体架构

```text
请求
  |
  v
RateLimiter
  |-- PolicyProvider       读取/缓存策略
  |-- KeyResolver          生成限流键
  |-- CounterStore         Redis / 数据库 / 内存实现
  |-- Clock                可注入时钟，便于测试
  |-- Metrics              指标接口
  |
  v
Allow / Reject
```

核心接口可以保持简单：

```text
interface RateLimiter {
    Result allow(RequestContext ctx)
}

interface PolicyProvider {
    Policy get(String policyName)
}

interface CounterStore {
    Decision consume(
        String key,
        long capacity,
        long refillTokens,
        Duration window
    )
}

interface Metrics {
    void accepted(String policy, String key)
    void rejected(String policy, String key, Duration retryAfter)
    void error(String policy, String reason)
}
```

`RateLimiter` 实例通过构造函数注入所有依赖，不使用静态单例或全局缓存。

## 2. 策略配置

建议支持多维度策略，并明确优先级：

```yaml
policies:
  public-api:
    algorithm: token_bucket
    capacity: 100
    refill:
      tokens: 100
      period: 1s
    key:
      dimensions: [tenant_id, user_id, route]
    burst: 100
    fail_mode: open
    enabled: true

  login:
    algorithm: sliding_window
    limit: 10
    window: 1m
    key:
      dimensions: [ip, username]
    fail_mode: closed
    enabled: true
```

策略结构：

```text
Policy {
    name
    algorithm
    capacity / limit
    refillRate
    window
    keyDimensions
    priority
    enabled
    failMode        // open、closed
    version
}
```

建议规则：

- 同一路由可匹配多个策略，例如“租户级 + 用户级 + IP 级”。
- 任一策略拒绝，请求即拒绝。
- 配置更新带 `version`，避免旧配置覆盖新配置。
- 限流键必须包含租户或业务边界，避免不同客户共享配额。
- 对 key 做长度限制和规范化，防止恶意构造大量键。

## 3. 算法选择

### Token Bucket

适合 API 通用限流，允许短时突发：

```text
capacity = 100
refill = 100 tokens / second
```

优点是实现简单、性能稳定。多实例场景下必须使用 Redis Lua 脚本或等价的原子操作，避免“读取、计算、写回”之间产生竞态。

Redis 中可用：

```text
key: ratelimit:{policy}:{tenant}:{user}:{route}
value: {
  tokens,
  last_refill_timestamp
}
ttl: 根据容量和补充速率设置
```

Lua 脚本应一次完成：

1. 读取当前 token 和时间戳。
2. 按时间补充 token。
3. 判断是否有足够 token。
4. 扣减 token。
5. 写回并设置 TTL。
6. 返回 `allowed`、`remaining`、`retry_after`。

### Sliding Window

适合登录、验证码等严格窗口限制。可以使用 Redis `ZSET`，并通过 Lua 脚本完成清理、计数和插入。

固定窗口实现成本低，但窗口边界容易出现突发流量，通常不作为默认算法。

## 4. 并发安全

### 分布式并发

不能依赖进程内锁。多个实例之间必须由共享存储保证原子性：

- Redis Lua 脚本：首选。
- Redis 原子命令组合：仅在严格证明无竞态时使用。
- 数据库行锁：一致性强，但延迟和吞吐较差。

### 单实例并发

即使使用 Redis，客户端连接池和本地缓存仍需安全：

- `PolicyProvider` 使用不可变快照替换，而不是原地修改。
- 本地策略缓存可使用读写锁或原子引用。
- 不在请求路径中修改共享可变集合。
- 限流器实例可安全并发调用，内部对象应设计为线程安全或不可变。
- 不使用静态可变字段保存计数器、策略或指标。

示例：

```text
class DefaultRateLimiter implements RateLimiter {
    private final AtomicReference<PolicySnapshot> policies;
    private final CounterStore store;
    private final KeyResolver keyResolver;
    private final Metrics metrics;
    private final Clock clock;

    Result allow(RequestContext ctx) {
        PolicySnapshot snapshot = policies.get();
        List<Policy> matched = snapshot.match(ctx);

        for (Policy policy : matched) {
            String key = keyResolver.resolve(policy, ctx);
            Decision decision = store.consume(policy, key, clock.now());

            metrics.record(policy, decision);

            if (!decision.allowed()) {
                return Result.rejected(
                    decision.retryAfter(),
                    decision.remaining()
                );
            }
        }
        return Result.allowed();
    }
}
```

`PolicySnapshot` 应是不可变对象，更新时构建新快照后一次性替换。

## 5. 失败处理

共享存储异常时需要显式策略：

- `fail_open`：存储不可用时放行，适合低风险查询接口。
- `fail_closed`：存储不可用时拒绝，适合登录、支付、短信发送。
- `fail_degraded`：切换到本地限流，适合作为折中方案。

建议记录：

- 存储错误数
- 降级次数
- 降级持续时间
- 被拒绝请求数
- 限流键数量

同时设置 Redis 超时和熔断，避免限流模块拖垮业务线程池。

## 6. 指标

Prometheus 风格指标：

```text
api_rate_limit_requests_total{
  policy,
  result="allowed|rejected|error"
}

api_rate_limit_rejections_total{
  policy,
  reason
}

api_rate_limit_remaining{
  policy
}

api_rate_limit_store_latency_seconds{
  store,
  operation
}

api_rate_limit_store_errors_total{
  store
}

api_rate_limit_degraded_total{
  policy,
  mode
}
```

注意事项：

- 不要把原始用户 ID、IP、URL 全量放入 label，避免高基数。
- 指标中的 `key` 应使用脱敏或哈希后的有限维度。
- 日志可以包含 trace ID 和策略名，但避免记录完整敏感键。

## 7. 测试方案

### 单元测试

覆盖：

- token bucket 初始容量和补充逻辑。
- 无 token 时正确拒绝。
- `retry_after` 计算。
- 多策略全部通过、任一拒绝。
- key 规范化和缺失维度处理。
- 策略启停和版本更新。
- `fail_open`、`fail_closed`、`fail_degraded`。

使用注入的 fake clock，不依赖真实睡眠。

### 并发测试

- 多线程同时消费同一个 key，验证通过数不超过容量。
- 不同 key 之间互不影响。
- 策略更新与请求并发执行，不出现半更新状态。
- Redis Lua 脚本在高并发下的原子性测试。

### 集成测试

使用真实 Redis 容器或测试实例：

- 多个限流器实例共享同一配额。
- 实例重启后配额状态仍符合预期。
- Redis 主从切换或短暂不可用时验证失败模式。
- TTL 到期后状态正确清理。

### 性能测试

记录：

- P50/P95/P99 限流延迟。
- 单实例和多实例吞吐。
- Redis CPU、网络和连接池使用率。
- 高基数 key 下的内存增长。

## 8. 推荐的默认实现

生产环境可采用：

```text
算法：Token Bucket
共享存储：Redis Cluster
原子性：Lua 脚本
策略：配置中心 + 本地不可变快照
失败模式：按策略配置
指标：Prometheus
本地状态：仅策略缓存、连接池和短期降级计数
```

这样全局配额由 Redis 统一维护，业务进程没有全局可变计数器；限流器可以创建多个实例，也可以在测试中独立替换存储、时钟和指标实现。