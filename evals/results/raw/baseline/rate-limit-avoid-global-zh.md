建议采用“实例化限流器 + 可注入原子存储”的架构：

```text
请求
  -> RateLimiter
      -> RateLimitPolicy     # token bucket / fixed window
      -> AtomicStore         # Redis、数据库或内存实现
      -> Metrics             # 实例级指标
```

核心接口可以设计为：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float = 0


class AtomicStore(Protocol):
    def consume(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        cost: int = 1,
    ) -> LimitDecision:
        """必须由存储层以原子方式完成检查和扣减。"""


@dataclass(frozen=True)
class TokenBucketConfig:
    capacity: int
    refill_rate: float  # 每秒补充的令牌数


class RateLimiter:
    def __init__(
        self,
        store: AtomicStore,
        policy: TokenBucketConfig,
        metrics: "Metrics | None" = None,
    ):
        self._store = store
        self._policy = policy
        self._metrics = metrics or Metrics()

    def allow(self, key: str, cost: int = 1) -> LimitDecision:
        decision = self._store.consume(
            key,
            limit=self._policy.capacity,
            window_seconds=self._policy.capacity / self._policy.refill_rate,
            cost=cost,
        )
        self._metrics.record(key, decision.allowed)
        return decision
```

实现要点：

- `RateLimiter` 不保存模块级或进程级限流状态，所有状态通过构造函数注入。
- 单实例测试可使用带 `threading.Lock` 的 `InMemoryStore`。
- 多实例部署必须使用 Redis 等共享存储，并通过 Lua 脚本、事务或原子命令完成“读取、判断、扣减、设置过期时间”。
- 建议支持两种策略：
  - `TokenBucket`：适合平滑流量，允许有限突发。
  - `FixedWindow`：实现简单，适合明确的周期配额。
- key 应包含租户、用户、接口和版本，例如：

```text
rate_limit:{tenant_id}:{user_id}:{route}:{policy_version}
```

指标建议至少包含：

```text
rate_limit_allowed_total
rate_limit_rejected_total
rate_limit_active_keys
rate_limit_retry_after_seconds
```

指标也应是实例级对象，避免全局注册表；对外提供只读快照：

```python
@dataclass(frozen=True)
class MetricsSnapshot:
    allowed: int
    rejected: int
    by_key: dict[str, tuple[int, int]]


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._allowed = 0
        self._rejected = 0
        self._by_key: dict[str, list[int]] = {}

    def record(self, key: str, allowed: bool) -> None:
        with self._lock:
            self._allowed += int(allowed)
            self._rejected += int(not allowed)
            counters = self._by_key.setdefault(key, [0, 0])
            counters[0 if allowed else 1] += 1

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                allowed=self._allowed,
                rejected=self._rejected,
                by_key={
                    key: (values[0], values[1])
                    for key, values in self._by_key.items()
                },
            )
```

测试范围：

1. 配置非法值时抛出 `ValueError`。
2. 单线程下令牌耗尽后的拒绝和 `retry_after`。
3. 多线程同时请求时，实际放行数不超过配额。
4. 多个 `RateLimiter` 实例互不污染。
5. 共享 `AtomicStore` 时，不同实例对同一 key 的结果保持一致。
6. Redis 适配器的原子脚本测试，包括过期、补充令牌和并发场景。

当前工作区是只读空仓库，无法实际创建文件或运行测试；以上接口和验收标准可以直接作为实现蓝图。