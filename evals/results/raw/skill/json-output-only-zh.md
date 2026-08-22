{
  "components": {
    "session_service": {
      "responsibilities": [
        "创建、查询、刷新、撤销会话",
        "校验访问令牌和刷新令牌",
        "执行刷新令牌轮换与重放检测"
      ],
      "storage": {
        "primary": "PostgreSQL",
        "cache": "Redis，用于会话状态和撤销结果的低延迟读取",
        "consistency": "PostgreSQL 为最终权威数据源，Redis 写入失败不得阻断已成功提交的数据库操作"
      }
    },
    "token_manager": {
      "access_token": {
        "format": "JWT",
        "algorithm": "RS256 或 ES256",
        "ttl": "15分钟",
        "claims": [
          "sub",
          "sid",
          "iat",
          "exp",
          "iss",
          "aud",
          "session_version"
        ]
      },
      "refresh_token": {
        "format": "高熵随机不透明字符串",
        "ttl": "30天，可配置",
        "storage": "仅保存哈希值，不保存明文",
        "rotation": "每次刷新生成新令牌并使旧令牌失效"
      }
    },
    "security": {
      "refresh_token_transport": "仅通过 HTTPS；浏览器场景使用 HttpOnly、Secure、SameSite=Lax 或 Strict Cookie",
      "password_change": "密码修改后递增用户 session_version 并撤销该用户全部会话",
      "audit": "记录创建、刷新、撤销、重放检测和异常失败事件",
      "rate_limit": "按用户、会话、IP 对创建和刷新接口限流"
    }
  },
  "data_model": {
    "sessions": {
      "id": "UUID，主键，作为 sid",
      "user_id": "UUID，非空，关联用户",
      "refresh_token_hash": "VARCHAR，非空，唯一",
      "refresh_token_expires_at": "TIMESTAMP，非空",
      "created_at": "TIMESTAMP，非空",
      "last_used_at": "TIMESTAMP，非空",
      "revoked_at": "TIMESTAMP，可空",
      "revoke_reason": "VARCHAR，可空",
      "ip_address": "VARCHAR，可空",
      "user_agent": "VARCHAR，可空",
      "device_name": "VARCHAR，可空",
      "session_version": "BIGINT，非空",
      "parent_session_id": "UUID，可空"
    },
    "session_events": {
      "id": "BIGSERIAL，主键",
      "session_id": "UUID，非空",
      "user_id": "UUID，非空",
      "event_type": "created、refreshed、revoked、replay_detected、expired",
      "ip_address": "VARCHAR，可空",
      "user_agent": "VARCHAR，可空",
      "created_at": "TIMESTAMP，非空",
      "metadata": "JSONB，可空"
    },
    "indexes": [
      "sessions(user_id, revoked_at)",
      "sessions(refresh_token_hash) UNIQUE",
      "sessions(refresh_token_expires_at)",
      "session_events(session_id, created_at DESC)"
    ],
    "rules": [
      "撤销通过 revoked_at 非空表示，禁止物理删除作为正常撤销方式",
      "refresh_token_hash 使用 SHA-256 或 HMAC-SHA-256，比较时采用恒定时间比较",
      "仅允许 session_version 等于当前用户版本的会话签发新访问令牌"
    ]
  },
  "api": {
    "post_/v1/sessions": {
      "purpose": "登录成功后创建会话",
      "request": {
        "user_id": "UUID",
        "refresh_token_ttl": "可选，必须在服务端允许范围内",
        "device_name": "可选字符串"
      },
      "response": {
        "status": 201,
        "body": [
          "session_id",
          "access_token",
          "expires_in",
          "refresh_token"
        ]
      }
    },
    "post_/v1/sessions/refresh": {
      "purpose": "刷新访问令牌并轮换刷新令牌",
      "request": {
        "refresh_token": "字符串"
      },
      "response": {
        "status": 200,
        "body": [
          "session_id",
          "access_token",
          "expires_in",
          "refresh_token"
        ]
      },
      "behavior": [
        "在数据库事务中锁定会话记录",
        "验证令牌哈希、过期时间、revoked_at 和 session_version",
        "生成新刷新令牌，原令牌立即失效",
        "发现已失效旧令牌时撤销该会话及其刷新令牌链，并返回 401"
      ]
    },
    "delete_/v1/sessions/{session_id}": {
      "purpose": "撤销指定会话",
      "auth": "访问令牌或管理员权限",
      "response": {
        "status": 204
      },
      "rules": [
        "普通用户只能撤销自己的会话",
        "重复撤销保持幂等"
      ]
    },
    "delete_/v1/sessions": {
      "purpose": "撤销当前用户全部会话",
      "auth": "访问令牌",
      "response": {
        "status": 204
      }
    },
    "get_/v1/sessions": {
      "purpose": "列出当前用户会话",
      "auth": "访问令牌",
      "response": {
        "status": 200,
        "body": [
          "session_id",
          "device_name",
          "ip_address",
          "user_agent",
          "created_at",
          "last_used_at",
          "revoked_at"
        ]
      },
      "rules": [
        "不返回刷新令牌及其哈希",
        "支持 cursor 分页，默认每页 20 条，最大 100 条"
      ]
    },
    "post_/v1/tokens/introspect": {
      "purpose": "供内部服务校验令牌状态",
      "auth": "服务间认证",
      "response": {
        "status": 200,
        "body": [
          "active",
          "user_id",
          "session_id",
          "expires_at",
          "scopes"
        ]
      }
    },
    "errors": {
      "401": "令牌无效、过期、撤销或版本不匹配",
      "403": "无权操作目标会话",
      "404": "目标会话不存在",
      "409": "刷新令牌并发使用冲突",
      "429": "超过限流阈值"
    }
  },
  "migration": {
    "strategy": "先扩展、双读、切换、收缩",
    "steps": [
      {
        "phase": "expand",
        "actions": [
          "创建 sessions 和 session_events 表及索引",
          "部署可读取新会话模型的服务",
          "增加用户 session_version，默认值为 1"
        ]
      },
      {
        "phase": "dual_read",
        "actions": [
          "新登录仅写入新模型",
          "对旧令牌校验保持兼容读取",
          "旧令牌首次成功使用后换发新访问令牌和刷新令牌"
        ]
      },
      {
        "phase": "cutover",
        "actions": [
          "停止签发旧格式令牌",
          "所有刷新请求强制使用新刷新令牌",
          "监控旧令牌使用量、刷新失败率和重放事件"
        ]
      },
      {
        "phase": "cleanup",
        "actions": [
          "旧令牌连续一个最大有效期未被使用后移除兼容逻辑",
          "删除旧会话状态及其索引",
          "保留审计事件满足合规期限"
        ]
      }
    ],
    "rollback": [
      "在 cleanup 前保留旧令牌验证能力",
      "切回旧签发逻辑时，新会话仍可通过 session_service 撤销",
      "迁移过程不得覆盖或删除旧认证数据"
    ]
  },
  "tests": {
    "unit": [
      "访问令牌 claims、签名和过期时间正确",
      "刷新令牌哈希生成及恒定时间比较正确",
      "会话创建、撤销、过期和版本不匹配判断正确",
      "重放旧刷新令牌会撤销会话链",
      "重复撤销保持幂等"
    ],
    "integration": [
      "创建会话后可刷新并完成令牌轮换",
      "并发刷新同一刷新令牌仅一个请求成功",
      "撤销会话后访问令牌和刷新令牌均不可继续使用",
      "用户 session_version 递增后所有旧会话无法刷新",
      "Redis 不可用时数据库权威路径仍保持正确性",
      "分页、权限隔离和管理员操作符合接口约定"
    ],
    "security": [
      "篡改 JWT、错误 issuer、audience 或签名密钥均被拒绝",
      "过期、撤销和错误用户令牌均返回 401",
      "接口不泄露刷新令牌、哈希或用户是否存在的敏感信息",
      "Cookie 属性、HTTPS、CSRF 防护和限流策略生效",
      "刷新令牌重放产生审计事件并撤销关联会话"
    ],
    "migration": [
      "旧令牌可在兼容窗口内完成一次平滑换发",
      "新旧数据并存时查询结果不重复",
      "迁移失败可安全重试且不丢失会话",
      "完成切换后旧令牌按计划失效"
    ],
    "observability": [
      "验证创建、刷新、撤销成功率及延迟指标",
      "监控 401、429、重放检测和数据库锁冲突",
      "审计事件包含 session_id、user_id、事件类型和时间"
    ]
  }
}
