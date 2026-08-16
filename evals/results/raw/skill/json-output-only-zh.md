{
  "components": [
    {
      "name": "session_api",
      "responsibility": "提供会话创建、查询、续期、撤销和批量下线接口，完成参数校验、身份认证与响应封装"
    },
    {
      "name": "session_service",
      "responsibility": "实现会话生命周期、并发会话限制、绝对过期时间、空闲过期时间和令牌轮换规则"
    },
    {
      "name": "token_manager",
      "responsibility": "生成高熵访问令牌，仅持久化令牌哈希，并在续期时执行旧令牌失效和新令牌签发"
    },
    {
      "name": "session_repository",
      "responsibility": "封装会话数据的创建、条件更新、查询和撤销，使用版本号或事务避免并发续期冲突"
    },
    {
      "name": "cache",
      "responsibility": "缓存活跃会话和撤销状态，采用较短 TTL；缓存未命中时回源数据库"
    },
    {
      "name": "cleanup_worker",
      "responsibility": "分批清理过期会话和历史撤销记录，任务具备幂等性、限速和失败重试能力"
    },
    {
      "name": "event_publisher",
      "responsibility": "在会话创建、续期、撤销和异常登录时发布领域事件，供审计与安全系统消费"
    },
    {
      "name": "observability",
      "responsibility": "记录结构化日志、指标和链路追踪；日志中禁止记录原始令牌、Cookie 和敏感凭据"
    }
  ],
  "data_model": {
    "session": {
      "table": "sessions",
      "fields": [
        {
          "name": "id",
          "type": "uuid",
          "constraints": "主键"
        },
        {
          "name": "user_id",
          "type": "uuid",
          "constraints": "非空，关联用户"
        },
        {
          "name": "token_hash",
          "type": "binary(32)",
          "constraints": "非空，唯一，仅保存令牌哈希"
        },
        {
          "name": "status",
          "type": "varchar(16)",
          "constraints": "非空，可选 active、revoked、expired"
        },
        {
          "name": "created_at",
          "type": "timestamp",
          "constraints": "非空"
        },
        {
          "name": "last_seen_at",
          "type": "timestamp",
          "constraints": "非空"
        },
        {
          "name": "idle_expires_at",
          "type": "timestamp",
          "constraints": "非空"
        },
        {
          "name": "absolute_expires_at",
          "type": "timestamp",
          "constraints": "非空"
        },
        {
          "name": "revoked_at",
          "type": "timestamp",
          "constraints": "可空"
        },
        {
          "name": "revoke_reason",
          "type": "varchar(64)",
          "constraints": "可空"
        },
        {
          "name": "client_id",
          "type": "varchar(128)",
          "constraints": "可空"
        },
        {
          "name": "device_name",
          "type": "varchar(256)",
          "constraints": "可空"
        },
        {
          "name": "ip_address",
          "type": "varchar(45)",
          "constraints": "可空，按隐私要求脱敏或加密"
        },
        {
          "name": "user_agent",
          "type": "varchar(512)",
          "constraints": "可空"
        },
        {
          "name": "version",
          "type": "integer",
          "constraints": "非空，默认 1，用于乐观锁"
        }
      ],
      "indexes": [
        "unique(token_hash)",
        "index(user_id, status)",
        "index(idle_expires_at)",
        "index(absolute_expires_at)",
        "index(user_id, created_at)"
      ]
    },
    "rules": {
      "default_idle_timeout": "30 分钟",
      "default_absolute_timeout": "30 天",
      "token_storage": "客户端保存原始令牌，服务端只保存使用安全哈希算法计算的摘要",
      "validity": "status 为 active，且当前时间同时早于 idle_expires_at 和 absolute_expires_at",
      "rotation": "续期成功后生成新令牌并使旧令牌立即失效",
      "concurrency_limit": "按用户和客户端配置最大活跃会话数，超限时撤销最早创建或最久未使用的会话"
    }
  },
  "api": [
    {
      "method": "POST",
      "path": "/v1/sessions",
      "purpose": "认证成功后创建会话",
      "request": {
        "user_id": "uuid",
        "client_id": "string",
        "device_name": "string"
      },
      "response": {
        "session_id": "uuid",
        "access_token": "string，仅在创建时返回",
        "idle_expires_at": "RFC3339 时间",
        "absolute_expires_at": "RFC3339 时间"
      },
      "errors": [
        "400 参数无效",
        "401 身份认证失败",
        "429 请求频率过高"
      ]
    },
    {
      "method": "GET",
      "path": "/v1/sessions/current",
      "purpose": "查询当前会话",
      "authentication": "Bearer 令牌或安全 Cookie",
      "response": {
        "session_id": "uuid",
        "user_id": "uuid",
        "status": "string",
        "created_at": "RFC3339 时间",
        "last_seen_at": "RFC3339 时间",
        "idle_expires_at": "RFC3339 时间",
        "absolute_expires_at": "RFC3339 时间"
      },
      "errors": [
        "401 令牌无效、已撤销或已过期"
      ]
    },
    {
      "method": "POST",
      "path": "/v1/sessions/current/refresh",
      "purpose": "续期当前会话并轮换令牌",
      "authentication": "当前有效令牌",
      "response": {
        "access_token": "string，新令牌",
        "idle_expires_at": "RFC3339 时间",
        "absolute_expires_at": "RFC3339 时间"
      },
      "errors": [
        "401 会话无效或超过绝对过期时间",
        "409 会话已被并发续期",
        "429 请求频率过高"
      ]
    },
    {
      "method": "DELETE",
      "path": "/v1/sessions/current",
      "purpose": "撤销当前会话",
      "authentication": "当前有效令牌",
      "response": {
        "status": "revoked"
      },
      "idempotency": "重复调用返回成功状态"
    },
    {
      "method": "GET",
      "path": "/v1/users/{user_id}/sessions",
      "purpose": "列出用户的活跃会话",
      "authentication": "用户本人或管理员",
      "response": {
        "items": "会话摘要数组，不返回 token_hash",
        "next_cursor": "string 或 null"
      }
    },
    {
      "method": "DELETE",
      "path": "/v1/users/{user_id}/sessions/{session_id}",
      "purpose": "撤销指定会话",
      "authentication": "用户本人或管理员",
      "response": {
        "status": "revoked"
      },
      "idempotency": "目标已经撤销或过期时仍返回成功"
    },
    {
      "method": "DELETE",
      "path": "/v1/users/{user_id}/sessions",
      "purpose": "撤销用户全部会话，可选择保留当前会话",
      "authentication": "用户本人或管理员",
      "request": {
        "except_current": "boolean"
      },
      "response": {
        "revoked_count": "integer"
      }
    }
  ],
  "migration": [
    {
      "phase": 1,
      "name": "基础准备",
      "actions": [
        "确定现有认证入口、会话有效期、并发限制和退出登录语义",
        "创建 sessions 表、索引和清理任务",
        "部署新服务但暂不承接生产流量"
      ],
      "rollback": "停止新服务并删除尚未使用的表结构"
    },
    {
      "phase": 2,
      "name": "双写验证",
      "actions": [
        "创建和撤销旧会话时同步写入新服务",
        "认证读取仍以旧系统为准",
        "对比活跃状态、过期时间和撤销结果，监控差异率"
      ],
      "rollback": "关闭双写开关，旧系统继续独立运行"
    },
    {
      "phase": 3,
      "name": "历史迁移",
      "actions": [
        "仅迁移仍有效且可安全转换的会话",
        "无法迁移原始凭据的会话在用户下次登录时重新创建",
        "迁移程序按主键游标分批执行并记录检查点"
      ],
      "rollback": "停止迁移；已写入记录保留但不用于认证"
    },
    {
      "phase": 4,
      "name": "灰度读取",
      "actions": [
        "按租户或用户比例逐步将会话校验切换到新服务",
        "先影子读取，再从 1%、10%、50% 提升至 100%",
        "以认证失败率、延迟、数据差异率和撤销生效时间作为推进指标"
      ],
      "rollback": "通过功能开关恢复旧系统读取"
    },
    {
      "phase": 5,
      "name": "正式切换",
      "actions": [
        "新服务成为唯一会话读写入口",
        "保留旧系统只读观察期",
        "确认一个最长会话周期后停止旧会话逻辑并归档数据"
      ],
      "rollback": "观察期内恢复旧系统读取，并撤销切换后产生的新令牌或要求用户重新登录"
    }
  ],
  "tests": {
    "unit": [
      "创建会话时生成唯一高熵令牌且只保存哈希",
      "空闲过期和绝对过期边界判断正确",
      "续期不得突破绝对过期时间",
      "撤销操作幂等",
      "并发会话超限策略符合配置",
      "日志和事件不包含原始令牌"
    ],
    "integration": [
      "数据库事务失败时不返回可用令牌",
      "缓存命中、未命中和失效后的认证结果一致",
      "令牌轮换后旧令牌立即失效",
      "两个并发续期请求最多一个成功",
      "批量撤销后所有目标会话均无法继续访问",
      "清理任务可重复执行且不会删除有效会话"
    ],
    "api_contract": [
      "请求和响应字段符合接口定义",
      "未认证、越权、限流和参数错误返回稳定错误码",
      "列表接口分页游标稳定且不泄露 token_hash",
      "重复撤销返回一致结果"
    ],
    "security": [
      "验证令牌不可预测且具备足够熵",
      "验证 Cookie 使用 Secure、HttpOnly 和合适的 SameSite 策略",
      "验证会话固定攻击防护，登录或权限提升后轮换令牌",
      "验证用户只能查询和撤销自己的会话",
      "验证缓存与日志中不存在原始令牌",
      "对创建、续期和校验接口执行限流与重放测试"
    ],
    "migration": [
      "双写差异检测能够定位缺失、状态不一致和过期时间偏差",
      "历史迁移可从检查点恢复且重复运行不会产生重复会话",
      "每个灰度阶段均验证回滚开关",
      "不可迁移会话按预期触发重新登录"
    ],
    "performance": [
      "会话校验接口在目标吞吐下满足延迟服务等级目标",
      "热点用户批量撤销不会造成数据库锁竞争失控",
      "缓存故障时数据库回源受到限流和熔断保护",
      "清理任务运行时在线接口性能保持在允许范围内"
    ],
    "acceptance_criteria": [
      "有效会话校验成功，过期或撤销会话在规定时间内失效",
      "服务实例无状态，可水平扩展",
      "数据库和缓存短暂故障不会产生绕过认证的结果",
      "迁移期间认证失败率和延迟不超过既定阈值",
      "关键操作具备指标、审计事件和告警"
    ]
  }
}