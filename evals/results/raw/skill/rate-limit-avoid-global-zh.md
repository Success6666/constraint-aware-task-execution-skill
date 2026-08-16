## API 限流模块设计

```text
HTTP/gRPC 请求
    │
    ▼
RateLimitFilter
    ├── PolicyResolver：匹配策略、生成限流键
    ├── RateLimiter：执行限流判定
    │      └── Redis GCRA Lua：跨实例原子判定
    ├── MetricsRecorder：记录低基数指标
    └── ResponseWriter：返回 429 与限流响应头
```

### 1. 核心接口

```java
public interface RateLimiter {
    LimitDecision acquire(LimitRequest request);
}

public record LimitRequest(
    String policyId,
    String subjectKey,
    int permits
) {}

public record LimitDecision(
    boolean allowed,
    long limit,
    long remaining,
    Duration retryAfter,
    Instant resetAt,
    String reason
) {}
```

所有组件通过构造器注入。策略快照、Redis 客户端和指标记录器均由应用实例持有，不使用静态单例容器。

### 2. 策略配置

```yaml
rate-limit:
  failure-mode: open
  key-prefix: api-rate-limit
  policies:
    - id: login-by-ip
      enabled: true
      priority: 100
      match:
        path: /api/login
        methods: [POST]
      key:
        dimensions: [clientIp]
      rate:
        requests: 10
        period: 1m
        burst-capacity: 3

    - id: tenant-api
      enabled: true
      priority: 50
      match:
        path: /api/**
      key:
        dimensions: [tenantId, routeId]
      rate:
        requests: 1000
        period: 1m
        burst-capacity: 50
```

规则按 `priority` 降序匹配，首条命中生效，避免多条规则分别扣减造成部分成功。配置加载时校验唯一 ID、正数配额、合法周期和受控键维度。

动态配置使用不可变 `PolicySnapshot`。新配置完整解析成功后，通过实例内的原子引用一次替换；请求始终读取同一个版本快照。

### 3. 分布式并发模型

采用 Redis GCRA 算法：

- Redis `TIME` 提供统一时间，规避多实例时钟漂移。
- 每个限流键保存理论到达时间 `TAT`。
- Lua 脚本在 Redis 内完成读取、判定、更新和 TTL 设置。
- Redis Cluster 中每次判定只操作一个键，不产生跨槽事务。
- 键格式：`rl:{policyId}:<sha256(canonical dimensions)>`。
- 配额、间隔和时间统一使用整数微秒，避免浮点累计误差。
- TTL 根据恢复到满容量所需时间计算，自动清理冷键。
- 脚本通过 `SCRIPT LOAD` 加载，遇到 `NOSCRIPT` 时重新加载并重试一次。

核心判定：

```text
interval = ceil(periodMicros / requests)
tolerance = (burstCapacity - 1) * interval
tat = max(redisTime, storedTat)
allowAt = tat - tolerance

redisTime < allowAt  => 拒绝，retryAfter = allowAt - redisTime
否则                => 接受，newTat = tat + permits * interval
```

Redis 不可用时按策略选择：

- `open`：放行并记录降级指标，适合普通业务接口。
- `closed`：拒绝并返回服务不可用，适合登录、验证码等安全接口。
- 不启用本地兜底配额，因为各实例独立计数会放大总额度。

### 4. HTTP 行为

允许请求时继续调用下游；拒绝时返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 12
```

响应体使用现有统一错误格式，例如错误码 `RATE_LIMIT_EXCEEDED`。代理场景下仅从受信任代理写入的转发头解析客户端 IP，防止伪造 IP 绕过限流。

### 5. 指标

建议暴露以下 Prometheus 指标：

```text
rate_limit_decisions_total{policy,outcome,reason}
rate_limit_decision_duration_seconds{policy}
rate_limit_redis_operations_total{operation,outcome}
rate_limit_redis_duration_seconds{operation}
rate_limit_degraded_total{policy,mode}
rate_limit_policy_snapshot_version
rate_limit_policy_reload_total{outcome}
```

标签只包含策略 ID、结果和固定原因，不包含用户、IP、URL参数或完整路径，避免时间序列基数失控。日志记录策略 ID、配置版本、结果和追踪 ID；限流键仅记录摘要。

### 6. 测试与验收

- 单元测试：策略优先级、路径匹配、键规范化、响应头、配置快照切换。
- Lua 集成测试：首次请求、突发容量、持续恢复、TTL、Redis 时间、脚本重载。
- 并发测试：多个线程同时请求同一键，成功数不得超过对应时间窗口允许值。
- 多实例测试：启动至少 3 个限流器实例，共用真实 Redis，验证总配额而非单实例配额。
- 属性测试：将随机请求序列与内存参考模型逐次比较。
- 故障测试：Redis 超时、连接断开、`NOSCRIPT`、配置更新失败以及恢复。
- 压测：验证 Redis 延迟、429 比例和键数量；限流器自身不应产生无界线程、队列或缓存。

当前仓库只有技能文件且执行环境为只读，无法创建 `tmp-docs` 任务书、源码和测试，也无法提交版本。外部 Edge 的联网依赖检查同样被执行策略拦截，因此本次交付为实现级设计，尚未写入项目。