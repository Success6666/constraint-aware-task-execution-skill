{
  "components": [
    {
      "name": "session-api",
      "responsibility": "提供会话创建、查询、刷新、撤销和批量下线接口，完成参数校验、身份认证、幂等控制与响应封装"
    },
    {
      "name": "session-service",
      "responsibility": "实现会话生命周期、并发登录策略、滑动过期、绝对过期、令牌轮换和风险控制等核心业务逻辑"
    },
    {
      "name": "token-service",
      "responsibility": "生成高熵访问令牌和刷新令牌，仅持久化令牌哈希；支持密钥轮换、令牌重放检测和常量时间比较"
    },
    {
      "name": "session-store",
      "responsibility": "以关系型数据库作为权威数据源，保存会话、令牌族、撤销状态和审计记录"
    },
    {
      "name": "session-cache",
      "responsibility": "使用 Redis 缓存活跃会话、撤销标记和用户会话索引；采用短 TTL，并在缓存失效时回源数据库"
    },
    {
      "name": "cleanup-worker",
      "responsibility": "分批清理过期会话和历史审计数据，修复缓存索引，并通过分布式锁避免重复执行"
    },
    {
      "name": "event-publisher",
      "responsibility": "通过事务发件箱发布 session.created、session.refreshed、session.revoked 和 session.expired 事件"
    },
    {
      "name": "observability",
      "responsibility": "采集请求延迟、错误率、活跃会话数、刷新失败率、缓存命中率和异常撤销量，并记录不含原始令牌的结构化日志"
    }
  ],
  "data_model": {
    "sessions": {
      "fields": {
        "id": "UUID，主键",
        "user_id": "用户标识，必填并建立索引",
        "tenant_id": "租户标识，可选；多租户场景下参与所有查询条件",
        "status": "ACTIVE、REVOKED、EXPIRED",
        "created_at": "创建时间",
        "last_seen_at": "最近活动时间",
        "idle_expires_at": "滑动过期时间",
        "absolute_expires_at": "绝对过期时间",
        "revoked_at": "撤销时间，可空",
        "revoke_reason": "撤销原因，可空",
        "device_id": "设备稳定标识，可空",
        "device_name": "用户可识别的设备名称，可空",
        "ip_hash": "标准化 IP 的加盐哈希",
        "user_agent": "截断并清洗后的客户端信息",
        "version": "乐观锁版本号"
      },
      "indexes": [
        "tenant_id、user_id、status",
        "status、idle_expires_at",
        "status、absolute_expires_at",
        "device_id"
      ]
    },
    "refresh_tokens": {
      "fields": {
        "id": "UUID，主键",
        "session_id": "关联 sessions.id",
        "family_id": "令牌族标识，用于轮换和重放检测",
        "token_hash": "刷新令牌的单向哈希，唯一索引",
        "issued_at": "签发时间",
        "expires_at": "过期时间",
        "consumed_at": "被轮换使用的时间，可空",
        "replaced_by_id": "后继令牌标识，可空",
        "revoked_at": "撤销时间，可空"
      },
      "constraints": [
        "禁止保存原始访问令牌或刷新令牌",
        "刷新令牌成功使用后立即标记 consumed_at",
        "已消费令牌再次出现时撤销整个 family_id 对应的会话"
      ]
    },
    "session_audit": {
      "fields": {
        "id": "递增主键或 UUID",
        "session_id": "会话标识",
        "user_id": "用户标识",
        "event_type": "CREATED、REFRESHED、REVOKED、EXPIRED、REPLAY_DETECTED",
        "occurred_at": "事件时间",
        "request_id": "请求追踪标识",
        "metadata": "经过白名单过滤的 JSON 数据"
      }
    },
    "outbox_events": {
      "fields": {
        "id": "UUID，主键",
        "aggregate_id": "会话标识",
        "event_type": "事件类型",
        "payload": "事件载荷 JSON",
        "created_at": "创建时间",
        "published_at": "发布时间，可空",
        "retry_count": "发布重试次数"
      }
    }
  },
  "api": [
    {
      "method": "POST",
      "path": "/v1/sessions",
      "purpose": "认证成功后创建会话",
      "request": {
        "user_id": "由可信认证上下文提供",
        "device_id": "可选",
        "device_name": "可选"
      },
      "response": {
        "session_id": "UUID",
        "access_token": "短期令牌",
        "refresh_token": "仅在创建时返回",
        "expires_in": "访问令牌有效秒数",
        "absolute_expires_at": "会话绝对过期时间"
      },
      "controls": [
        "支持 Idempotency-Key",
        "按用户、IP 和设备限流",
        "执行单用户最大活跃会话数策略"
      ]
    },
    {
      "method": "GET",
      "path": "/v1/sessions/{session_id}",
      "purpose": "查询当前会话详情",
      "authorization": "仅会话所有者或具备会话管理权限的管理员可访问"
    },
    {
      "method": "GET",
      "path": "/v1/users/me/sessions",
      "purpose": "分页列出当前用户的活跃会话和设备信息",
      "query": {
        "cursor": "可选游标",
        "limit": "默认 20，最大 100"
      }
    },
    {
      "method": "POST",
      "path": "/v1/sessions/refresh",
      "purpose": "轮换刷新令牌并签发新的访问令牌",
      "request": {
        "refresh_token": "必填"
      },
      "behavior": [
        "在单个数据库事务中消费旧令牌并创建新令牌",
        "使用行锁或条件更新处理并发刷新",
        "检测重放后撤销整个令牌族"
      ]
    },
    {
      "method": "DELETE",
      "path": "/v1/sessions/{session_id}",
      "purpose": "撤销指定会话",
      "behavior": "幂等；重复撤销仍返回成功"
    },
    {
      "method": "DELETE",
      "path": "/v1/users/me/sessions",
      "purpose": "撤销当前用户除当前会话外的全部会话",
      "query": {
        "except_current": "默认 true"
      }
    },
    {
      "method": "POST",
      "path": "/v1/internal/sessions/introspect",
      "purpose": "供受信任内部服务校验会话状态",
      "controls": [
        "仅允许服务身份调用",
        "响应不包含令牌哈希或敏感设备数据",
        "设置严格超时和调用方限流"
      ]
    }
  ],
  "migration": {
    "strategy": "采用兼容性优先的分阶段迁移，支持随时回滚且不要求停机",
    "phases": [
      {
        "phase": 1,
        "name": "准备",
        "actions": [
          "建立会话、刷新令牌、审计和发件箱表",
          "添加索引、外键、数据保留策略和监控面板",
          "部署新服务但不接收生产流量"
        ]
      },
      {
        "phase": 2,
        "name": "双写与影子校验",
        "actions": [
          "旧系统继续作为读取权威，同时异步或事务性写入新存储",
          "对抽样请求执行影子读取并比较状态、过期时间和用户归属",
          "记录差异但不影响用户请求"
        ]
      },
      {
        "phase": 3,
        "name": "历史迁移",
        "actions": [
          "按主键游标分批迁移有效会话",
          "仅迁移仍有效且可安全转换的数据",
          "通过行数、校验和与抽样查询验证迁移结果"
        ]
      },
      {
        "phase": 4,
        "name": "灰度切读",
        "actions": [
          "按租户或用户哈希逐步将 1%、10%、50%、100% 流量切换到新服务",
          "持续观察登录失败率、刷新失败率、延迟和撤销量",
          "异常时立即将读取路由切回旧系统"
        ]
      },
      {
        "phase": 5,
        "name": "切写与收敛",
        "actions": [
          "新服务成为读写权威，旧系统保留只读兼容窗口",
          "停止双写后执行最终差异核对",
          "等待最长会话有效期后下线旧令牌验证逻辑"
        ]
      },
      {
        "phase": 6,
        "name": "清理",
        "actions": [
          "删除旧系统依赖前确认无调用流量",
          "归档必要审计数据",
          "在独立变更中移除旧表、旧配置和兼容代码"
        ]
      }
    ],
    "rollback": [
      "灰度期间通过路由开关恢复旧系统读取",
      "双写保留至新系统稳定期结束，确保回滚后数据连续",
      "数据库变更先扩展后收缩，回滚阶段不删除字段或表",
      "若新令牌格式无法被旧系统识别，则保留兼容验证器或强制重新认证"
    ]
  },
  "tests": {
    "unit": [
      "会话创建、滑动过期和绝对过期边界",
      "最大并发会话策略",
      "访问令牌与刷新令牌生成及哈希校验",
      "刷新令牌轮换和重放检测",
      "撤销幂等性",
      "租户隔离和权限判断"
    ],
    "integration": [
      "数据库事务、乐观锁和并发刷新竞争",
      "Redis 命中、未命中、超时及不可用时的回源行为",
      "事务发件箱与重复事件发布",
      "过期清理任务的分批处理和分布式锁",
      "密钥轮换期间新旧令牌兼容性"
    ],
    "contract": [
      "验证所有接口的请求与响应结构",
      "验证错误码、分页游标和幂等语义",
      "验证内部 introspect 接口的服务身份认证"
    ],
    "security": [
      "原始令牌不会出现在数据库、日志、指标或事件中",
      "令牌枚举、伪造、重放和时序攻击",
      "跨用户及跨租户越权访问",
      "CSRF、注入、恶意 User-Agent 和超长输入",
      "限流、暴力刷新和异常设备登录场景"
    ],
    "migration": [
      "旧数据映射的完整性和幂等性",
      "双写失败补偿与重试",
      "影子读取差异检测",
      "各灰度阶段的路由切换和回滚",
      "旧令牌在兼容窗口内的验证行为"
    ],
    "performance": [
      "按预估峰值两倍执行创建、校验、刷新和撤销压测",
      "验证缓存冷启动和热点用户大量会话场景",
      "确认数据库索引命中且不存在全表扫描",
      "检查 p95 和 p99 延迟、连接池饱和度及错误率"
    ],
    "resilience": [
      "数据库主从切换",
      "Redis 故障和缓存雪崩",
      "事件系统不可用",
      "时钟偏差",
      "服务滚动发布期间的会话连续性"
    ],
    "acceptance_criteria": {
      "availability": "核心校验接口月度可用性不低于 99.95%",
      "latency": "缓存命中时会话校验 p99 小于 50 毫秒，回源数据库时 p99 小于 200 毫秒",
      "consistency": "撤销操作完成后 5 秒内在所有实例生效",
      "security": "刷新令牌重放能够撤销对应令牌族，且任何持久化位置均不存在原始令牌",
      "migration": "全量切换前影子读取差异率低于 0.01%，且不存在跨用户或跨租户差异"
    }
  }
}