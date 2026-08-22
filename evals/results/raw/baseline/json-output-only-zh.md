{
  "components": {
    "session_api": {
      "responsibilities": [
        "创建、查询、刷新、撤销和批量撤销会话",
        "校验调用方身份及会话所有权",
        "处理幂等请求、并发刷新和统一错误响应"
      ],
      "stateless": true,
      "dependencies": [
        "关系型数据库",
        "Redis",
        "用户身份服务",
        "审计日志服务",
        "密钥管理服务"
      ]
    },
    "session_store": {
      "primary_store": "关系型数据库",
      "cache_store": "Redis",
      "database_usage": [
        "保存会话元数据、哈希后的刷新令牌、撤销状态和版本号",
        "支持审计查询、设备管理和跨实例一致性"
      ],
      "redis_usage": [
        "缓存活跃会话摘要",
        "保存刷新令牌短期互斥锁",
        "保存访问令牌黑名单直至其自然过期"
      ]
    },
    "token_service": {
      "access_token": "短时效 JWT，建议有效期 10 分钟，包含 sub、sid、iat、exp、aud、iss 和 session_version",
      "refresh_token": "高熵随机不透明令牌，仅保存其 HMAC-SHA-256 摘要",
      "token_rotation": true,
      "refresh_reuse_detection": true
    },
    "policy_service": {
      "responsibilities": [
        "应用会话空闲超时和绝对超时策略",
        "限制每个用户、租户、应用和设备的活跃会话数量",
        "根据用户状态、风险等级和客户端类型决定是否允许刷新"
      ]
    },
    "audit_service": {
      "events": [
        "session.created",
        "session.refreshed",
        "session.revoked",
        "session.reuse_detected",
        "session.expired",
        "session.bulk_revoked"
      ],
      "event_fields": [
        "event_id",
        "event_type",
        "occurred_at",
        "actor_id",
        "user_id",
        "session_id",
        "client_id",
        "ip_hash",
        "user_agent_hash",
        "reason",
        "request_id"
      ]
    },
    "background_jobs": [
      "清理已过期会话和历史刷新令牌",
      "清理 Redis 黑名单",
      "发布长期未使用会话的过期事件",
      "检测并处理异常刷新重放"
    ],
    "security_controls": [
      "刷新令牌只通过 TLS 传输",
      "刷新令牌使用至少 256 位随机熵",
      "数据库仅保存令牌摘要，不记录原始令牌",
      "敏感日志进行脱敏",
      "所有管理接口执行租户隔离和权限校验",
      "对创建、刷新、撤销接口执行按用户、客户端和 IP 的限流",
      "服务间调用使用 mTLS 或签名服务凭证"
    ]
  },
  "data_model": {
    "sessions": {
      "primary_key": "session_id",
      "fields": {
        "session_id": {
          "type": "uuid",
          "required": true
        },
        "user_id": {
          "type": "string",
          "required": true
        },
        "tenant_id": {
          "type": "string",
          "required": true
        },
        "client_id": {
          "type": "string",
          "required": true
        },
        "device_id": {
          "type": "string",
          "required": false
        },
        "status": {
          "type": "enum",
          "values": [
            "active",
            "revoked",
            "expired"
          ],
          "required": true
        },
        "session_version": {
          "type": "integer",
          "required": true,
          "default": 1
        },
        "created_at": {
          "type": "timestamp",
          "required": true
        },
        "last_seen_at": {
          "type": "timestamp",
          "required": true
        },
        "idle_expires_at": {
          "type": "timestamp",
          "required": true
        },
        "absolute_expires_at": {
          "type": "timestamp",
          "required": true
        },
        "revoked_at": {
          "type": "timestamp",
          "required": false
        },
        "revoked_reason": {
          "type": "string",
          "required": false
        },
        "ip_hash": {
          "type": "string",
          "required": false
        },
        "user_agent_hash": {
          "type": "string",
          "required": false
        },
        "metadata": {
          "type": "json",
          "required": false
        }
      },
      "indexes": [
        "tenant_id, user_id, status",
        "tenant_id, client_id, status",
        "device_id, status",
        "absolute_expires_at",
        "last_seen_at"
      ]
    },
    "refresh_tokens": {
      "primary_key": "refresh_token_id",
      "fields": {
        "refresh_token_id": {
          "type": "uuid",
          "required": true
        },
        "session_id": {
          "type": "uuid",
          "required": true
        },
        "token_hash": {
          "type": "binary",
          "required": true,
          "description": "使用服务端密钥计算的 HMAC-SHA-256 摘要"
        },
        "family_id": {
          "type": "uuid",
          "required": true
        },
        "parent_token_id": {
          "type": "uuid",
          "required": false
        },
        "status": {
          "type": "enum",
          "values": [
            "active",
            "used",
            "revoked",
            "expired"
          ],
          "required": true
        },
        "issued_at": {
          "type": "timestamp",
          "required": true
        },
        "used_at": {
          "type": "timestamp",
          "required": false
        },
        "expires_at": {
          "type": "timestamp",
          "required": true
        },
        "replaced_by_token_id": {
          "type": "uuid",
          "required": false
        }
      },
      "constraints": [
        "token_hash 唯一",
        "session_id 必须引用 sessions.session_id",
        "status=active 时同一 session_id 最多存在一个未过期令牌",
        "令牌过期时间不得晚于所属会话 absolute_expires_at"
      ],
      "indexes": [
        "token_hash",
        "session_id, status",
        "family_id, status",
        "expires_at"
      ]
    },
    "session_events": {
      "primary_key": "event_id",
      "fields": {
        "event_id": {
          "type": "uuid",
          "required": true
        },
        "event_type": {
          "type": "string",
          "required": true
        },
        "session_id": {
          "type": "uuid",
          "required": false
        },
        "user_id": {
          "type": "string",
          "required": true
        },
        "tenant_id": {
          "type": "string",
          "required": true
        },
        "reason": {
          "type": "string",
          "required": false
        },
        "payload": {
          "type": "json",
          "required": false
        },
        "created_at": {
          "type": "timestamp",
          "required": true
        }
      },
      "indexes": [
        "tenant_id, user_id, created_at",
        "session_id, created_at"
      ]
    },
    "redis_keys": {
      "session_cache": "session:{session_id}",
      "refresh_lock": "session-refresh-lock:{session_id}",
      "access_blacklist": "access-revoked:{session_id}:{session_version}",
      "ttl_rule": "所有键的 TTL 不得超过对应会话或令牌的剩余有效期"
    },
    "invariants": [
      "撤销会话时 session.status 必须变为 revoked，session_version 必须递增",
      "同一刷新令牌只能成功使用一次",
      "检测到已使用刷新令牌再次出现时，立即撤销整个 refresh token family 和所属会话",
      "会话不能超过 absolute_expires_at，刷新只能延长 idle_expires_at，不能延长绝对过期时间",
      "任何租户只能访问本租户下的会话",
      "删除用户或禁用用户后，其全部 active 会话必须在最终一致性窗口内撤销"
    ]
  },
  "api": {
    "conventions": {
      "base_path": "/v1",
      "content_type": "application/json",
      "authentication": "Bearer access token 或受信任的内部服务凭证",
      "request_id": "支持 X-Request-Id，未提供时由服务生成",
      "idempotency": "创建会话支持 Idempotency-Key；同一键在 24 小时内返回同一结果",
      "error_format": {
        "fields": [
          "code",
          "message",
          "request_id",
          "details"
        ]
      }
    },
    "endpoints": [
      {
        "method": "POST",
        "path": "/sessions",
        "purpose": "创建会话",
        "authorization": "调用方必须代表目标用户或通过内部身份服务完成认证",
        "request": {
          "user_id": "string",
          "tenant_id": "string",
          "client_id": "string",
          "device_id": "string",
          "scopes": [
            "string"
          ],
          "remember_me": "boolean",
          "context": {
            "ip": "string",
            "user_agent": "string"
          }
        },
        "response_201": {
          "session": "SessionSummary",
          "access_token": "string",
          "token_type": "Bearer",
          "expires_in": "integer",
          "refresh_token": "string"
        },
        "behavior": [
          "校验用户、租户、客户端和策略",
          "创建 session、refresh token 和审计事件",
          "原始 refresh token 仅在响应中返回一次"
        ]
      },
      {
        "method": "GET",
        "path": "/sessions",
        "purpose": "查询当前用户会话列表",
        "authorization": "当前用户或具有 session.read 权限的内部服务",
        "query": [
          "status",
          "device_id",
          "client_id",
          "page_size",
          "page_token"
        ],
        "response_200": {
          "items": [
            "SessionSummary"
          ],
          "next_page_token": "string|null"
        }
      },
      {
        "method": "GET",
        "path": "/sessions/{session_id}",
        "purpose": "查询单个会话",
        "authorization": "会话所属用户或具有 session.read 权限的内部服务",
        "response_200": {
          "session": "SessionDetail"
        }
      },
      {
        "method": "POST",
        "path": "/sessions/{session_id}/refresh",
        "purpose": "轮换刷新令牌并签发访问令牌",
        "authorization": "请求体中的 refresh_token 必须属于目标会话",
        "request": {
          "refresh_token": "string"
        },
        "response_200": {
          "session": "SessionSummary",
          "access_token": "string",
          "token_type": "Bearer",
          "expires_in": "integer",
          "refresh_token": "string"
        },
        "behavior": [
          "使用数据库事务和 session 级互斥锁保证并发安全",
          "将当前令牌从 active 原子更新为 used，并创建新令牌",
          "更新 last_seen_at 和 idle_expires_at",
          "刷新失败不泄露令牌是否存在",
          "发现重放时返回 401 SESSION_REUSE_DETECTED，并撤销整个会话"
        ]
      },
      {
        "method": "POST",
        "path": "/sessions/{session_id}/revoke",
        "purpose": "撤销单个会话",
        "authorization": "会话所属用户或具有 session.revoke 权限的内部服务",
        "request": {
          "reason": "string"
        },
        "response_204": {},
        "behavior": [
          "重复撤销保持幂等",
          "递增 session_version",
          "撤销全部关联刷新令牌",
          "发布 session.revoked 事件"
        ]
      },
      {
        "method": "POST",
        "path": "/sessions/revoke-all",
        "purpose": "撤销用户全部会话或按条件撤销",
        "authorization": "用户本人只能撤销自己的会话；管理员需具备 session.revoke_all 权限",
        "request": {
          "user_id": "string",
          "except_session_id": "string|null",
          "client_id": "string|null",
          "reason": "string"
        },
        "response_202": {
          "operation_id": "uuid",
          "affected_count": "integer|null"
        },
        "behavior": [
          "批量操作使用分页批处理，避免单事务锁定大量记录",
          "每批更新状态、递增版本并发布事件",
          "返回 operation_id 供异步查询"
        ]
      },
      {
        "method": "GET",
        "path": "/sessions/active",
        "purpose": "内部校验会话是否仍然有效",
        "authorization": "内部服务凭证",
        "query": {
          "session_id": "uuid",
          "session_version": "integer"
        },
        "response_200": {
          "active": "boolean",
          "expires_at": "timestamp",
          "scopes": [
            "string"
          ]
        }
      },
      {
        "method": "DELETE",
        "path": "/sessions/{session_id}",
        "purpose": "撤销单个会话的兼容接口",
        "authorization": "与 POST revoke 相同",
        "response_204": {}
      }
    ],
    "session_summary": {
      "fields": [
        "session_id",
        "user_id",
        "client_id",
        "device_id",
        "status",
        "created_at",
        "last_seen_at",
        "idle_expires_at",
        "absolute_expires_at",
        "is_current"
      ],
      "exclusions": [
        "refresh_token",
        "token_hash",
        "完整 IP",
        "完整 User-Agent"
      ]
    },
    "error_codes": [
      "INVALID_REQUEST",
      "UNAUTHENTICATED",
      "FORBIDDEN",
      "SESSION_NOT_FOUND",
      "SESSION_REVOKED",
      "SESSION_EXPIRED",
      "INVALID_REFRESH_TOKEN",
      "SESSION_REUSE_DETECTED",
      "SESSION_LIMIT_EXCEEDED",
      "RATE_LIMITED",
      "IDEMPOTENCY_CONFLICT"
    ],
    "default_policies": {
      "access_token_ttl_seconds": 600,
      "refresh_token_ttl_seconds": 2592000,
      "idle_timeout_seconds": 2592000,
      "absolute_timeout_seconds": 7776000,
      "maximum_active_sessions_per_user": 20,
      "maximum_active_sessions_per_device": 5
    }
  },
  "migration": {
    "strategy": "expand_migrate_contract",
    "phases": [
      {
        "name": "expand",
        "steps": [
          "创建 sessions、refresh_tokens、session_events 表及索引",
          "创建 Redis 键空间和 TTL 策略",
          "部署只读会话查询接口和审计事件发布能力",
          "为旧认证系统增加 session_id、session_version 和 client_id 的兼容字段"
        ],
        "rollback": "删除新服务流量路由，保留新增表和数据以便继续分析"
      },
      {
        "name": "dual_write",
        "steps": [
          "旧认证系统成功建立登录状态后同步创建新会话",
          "使用 outbox 机制保证数据库写入和事件发布最终一致",
          "对双写失败进行重试和告警，不阻断已有登录流程",
          "校验新旧系统的活跃会话数量、过期时间和撤销状态"
        ],
        "exit_criteria": [
          "连续 7 天双写成功率达到 99.99%",
          "无未解释的会话状态差异",
          "刷新令牌重放检测和撤销链路通过演练"
        ]
      },
      {
        "name": "cutover",
        "steps": [
          "先按租户或客户端灰度启用新会话创建和刷新",
          "逐步扩大流量并监控登录成功率、刷新失败率、撤销延迟和数据库负载",
          "新旧令牌验证同时保留一个完整刷新周期",
          "默认使用新会话服务签发令牌"
        ],
        "rollback": "通过路由开关恢复旧签发链路；已签发的新访问令牌继续按其 TTL 有效，必要时递增全局会话版本并批量撤销"
      },
      {
        "name": "contract",
        "steps": [
          "停止旧系统创建新会话",
          "等待旧刷新令牌自然过期或主动撤销",
          "移除旧会话存储和兼容字段",
          "保留审计数据及迁移期间的只读查询能力"
        ],
        "exit_criteria": [
          "旧令牌占比低于 0.1%",
          "无依赖方继续调用旧接口",
          "完成安全审计和数据留存确认"
        ]
      }
    ],
    "backfill": {
      "approach": "按用户分片、限速、可重试的批处理",
      "rules": [
        "无法安全恢复原始刷新令牌时不迁移令牌，仅要求用户重新登录",
        "迁移的会话默认标记为 active，但 idle_expires_at 不得晚于迁移时间加默认空闲超时",
        "来源不可信、已过期或状态不明确的会话直接标记为 revoked",
        "回填过程写入 migration_source 和 migration_batch_id"
      ]
    },
    "operational_requirements": [
      "所有迁移步骤支持幂等执行",
      "建立旧新状态对账任务",
      "迁移前备份数据库并验证恢复流程",
      "设置数据库、Redis、错误率和令牌重放检测告警"
    ]
  },
  "tests": {
    "unit": [
      "验证会话创建时的默认 TTL、scope 和版本号",
      "验证空闲超时与绝对超时的计算边界",
      "验证访问令牌 claims 包含正确的 session_id 和 session_version",
      "验证无效、过期、撤销和跨租户会话均被拒绝",
      "验证撤销操作递增 session_version 且重复调用幂等",
      "验证策略正确限制用户、设备和客户端的活跃会话数",
      "验证错误响应不暴露令牌存在性或敏感数据"
    ],
    "integration": [
      "创建会话后可查询会话摘要，且不会返回原始刷新令牌",
      "刷新令牌成功轮换并使旧令牌不可再次使用",
      "并发提交同一刷新令牌时最多一个请求成功",
      "重放已使用刷新令牌会撤销整个令牌 family 和所属会话",
      "批量撤销只影响目标用户、租户、客户端和排除项范围内的会话",
      "数据库提交失败时不产生错误审计事件或错误成功响应",
      "outbox 重试不会产生重复业务事件",
      "Redis 不可用时按定义的降级策略运行，且不会绕过数据库安全校验"
    ],
    "contract": [
      "校验所有 HTTP 方法、路径、状态码、分页和错误码",
      "校验 Idempotency-Key 重复请求返回一致结果",
      "校验旧客户端能处理新增字段并忽略未知字段",
      "校验内部校验接口拒绝普通用户凭证",
      "校验事件 schema、版本号和必填字段"
    ],
    "security": [
      "令牌摘要无法反推出原始令牌",
      "跨用户、跨租户读取和撤销全部失败",
      "重放检测、令牌轮换和版本失效机制通过攻击测试",
      "限流、暴力刷新和异常 IP 行为触发预期策略",
      "日志、指标和审计记录不包含原始访问令牌、刷新令牌或完整隐私信息",
      "验证 TLS、mTLS、密钥轮换和密钥失效后的行为"
    ],
    "performance": [
      "在目标峰值并发下验证创建、刷新和撤销接口延迟",
      "验证批量撤销不会造成长事务、锁等待或连接池耗尽",
      "验证活跃会话查询分页在大用户量下保持稳定",
      "验证 Redis 缓存命中、失效和数据库回源行为",
      "验证过期清理任务不会影响在线请求"
    ],
    "migration": [
      "验证重复回填不会产生重复会话或刷新令牌",
      "验证旧新状态对账能发现创建、撤销和过期差异",
      "验证灰度切流、全量切流和回滚开关",
      "验证旧令牌过渡期、新令牌过渡期和完全收口后的行为",
      "验证迁移中断后可从最后成功批次继续执行"
    ],
    "acceptance_criteria": [
      "同一刷新令牌在任意并发条件下最多成功一次",
      "会话撤销后，新访问令牌在一个访问令牌 TTL 内被拒绝或通过版本校验失效",
      "所有公开接口均具备认证、授权、限流、审计和一致错误处理",
      "迁移具备可观测、可暂停、可重试和可回滚能力",
      "关键链路的成功率、延迟、撤销传播延迟和重放检测均有监控与告警"
    ]
  }
}
