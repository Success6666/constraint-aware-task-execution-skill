{
  "components": [
    {
      "name": "Session API",
      "responsibility": "提供会话创建、查询、刷新、撤销和批量撤销接口；校验身份、权限、参数及幂等请求。"
    },
    {
      "name": "Session Service",
      "responsibility": "实现会话生命周期、滑动过期、并发控制、设备管理和风险策略。"
    },
    {
      "name": "Token Manager",
      "responsibility": "生成和校验短期访问令牌及长期刷新令牌，支持令牌轮换、重放检测和密钥版本管理。"
    },
    {
      "name": "Session Repository",
      "responsibility": "封装持久化访问，支持按会话ID、用户ID、令牌哈希和状态查询。"
    },
    {
      "name": "Cache and Revocation Store",
      "responsibility": "缓存活跃会话和撤销状态，降低认证链路延迟并支持快速失效。"
    },
    {
      "name": "Policy and Audit Module",
      "responsibility": "执行空闲超时、绝对超时、设备数限制、异常IP检测，并记录安全审计事件。"
    },
    {
      "name": "Key Management Adapter",
      "responsibility": "从密钥管理系统读取签名密钥，支持轮换、灰度验证和旧密钥过渡期。"
    }
  ],
  "data_model": {
    "sessions": {
      "id": "UUID，主键",
      "user_id": "用户标识，建立索引",
      "refresh_token_hash": "刷新令牌哈希，唯一索引，不保存明文令牌",
      "access_token_version": "访问令牌版本或撤销代次",
      "status": "ACTIVE、REVOKED、EXPIRED、COMPROMISED",
      "device_id": "设备标识",
      "user_agent": "客户端信息，可脱敏",
      "ip_hash": "来源IP哈希或脱敏值",
      "created_at": "创建时间",
      "last_seen_at": "最近活动时间",
      "expires_at": "绝对过期时间",
      "idle_expires_at": "空闲过期时间",
      "revoked_at": "撤销时间",
      "revoke_reason": "撤销原因",
      "metadata": "受限大小的扩展JSON字段"
    },
    "session_events": {
      "id": "递增ID或UUID",
      "session_id": "关联会话",
      "user_id": "关联用户",
      "event_type": "CREATED、REFRESHED、REVOKED、EXPIRED、RISK_BLOCKED",
      "request_id": "请求追踪标识",
      "occurred_at": "事件时间",
      "ip_hash": "来源IP哈希",
      "metadata": "结构化事件详情"
    },
    "key_versions": {
      "kid": "密钥版本标识",
      "algorithm": "签名算法",
      "status": "ACTIVE、VERIFY_ONLY、RETIRED",
      "activated_at": "启用时间",
      "retired_at": "停用时间"
    },
    "indexes_and_constraints": [
      "sessions(id) 主键",
      "sessions(refresh_token_hash) 唯一索引",
      "sessions(user_id, status, expires_at) 组合索引",
      "sessions(user_id, device_id, status) 组合索引",
      "session_events(session_id, occurred_at) 索引",
      "所有时间字段统一使用UTC",
      "敏感字段仅保存哈希、脱敏值或加密值"
    ]
  },
  "api": {
    "authentication": "管理接口要求已认证主体具备相应权限；令牌接口使用HTTPS、限流和审计。",
    "endpoints": [
      {
        "method": "POST",
        "path": "/v1/sessions",
        "purpose": "创建会话并返回访问令牌、刷新令牌及过期信息",
        "idempotency": "支持Idempotency-Key，重复请求返回同一结果或明确冲突"
      },
      {
        "method": "POST",
        "path": "/v1/sessions/{id}/refresh",
        "purpose": "刷新访问令牌并轮换刷新令牌；旧刷新令牌立即失效"
      },
      {
        "method": "GET",
        "path": "/v1/sessions",
        "purpose": "查询当前用户的会话列表，支持设备、状态和分页过滤"
      },
      {
        "method": "DELETE",
        "path": "/v1/sessions/{id}",
        "purpose": "撤销指定会话，要求用户本人或管理员权限"
      },
      {
        "method": "DELETE",
        "path": "/v1/sessions",
        "purpose": "撤销当前用户除当前会话外的全部会话"
      },
      {
        "method": "POST",
        "path": "/v1/sessions/introspect",
        "purpose": "内部服务验证令牌有效性、主体、权限和会话状态"
      }
    ],
    "response_rules": [
      "统一返回request_id、错误码、消息和可选详情",
      "对不存在、已撤销和无权限资源采用不泄露状态的响应策略",
      "刷新令牌重放时撤销关联会话或令牌族并触发高优先级审计事件",
      "所有写操作使用乐观锁或条件更新，避免并发刷新产生多个有效令牌"
    ]
  },
  "migration": [
    {
      "phase": 1,
      "actions": [
        "创建sessions、session_events和key_versions表及索引",
        "部署只读仓储和审计写入能力",
        "配置密钥管理、缓存、限流和监控"
      ]
    },
    {
      "phase": 2,
      "actions": [
        "在兼容模式下生成新格式令牌",
        "双写旧会话系统与新服务，校验数据一致性",
        "按用户或租户灰度启用新会话校验"
      ]
    },
    {
      "phase": 3,
      "actions": [
        "将令牌验证和刷新流量切换至新服务",
        "对存量会话执行惰性迁移或强制重新登录策略",
        "持续监控错误率、刷新重放、延迟和撤销传播时间"
      ]
    },
    {
      "phase": 4,
      "actions": [
        "停止旧系统写入并保留只读回滚窗口",
        "确认无活跃依赖后归档旧数据和代码",
        "删除兼容逻辑，完成密钥和配置清理"
      ]
    },
    {
      "rollback": "保留旧验证路径和可切换配置；发生异常时停止新令牌签发，恢复旧校验并使新会话进入只读或批量撤销状态。"
    }
  ],
  "tests": {
    "unit": [
      "令牌签名、解析、过期、密钥版本和声明校验",
      "滑动过期、绝对过期、设备限制和策略判断",
      "刷新令牌轮换、重放检测、撤销原因和幂等处理"
    ],
    "integration": [
      "API与数据库事务、唯一约束、乐观锁和分页查询",
      "缓存命中、缓存失效、撤销状态传播和缓存不可用降级",
      "密钥管理系统轮换及旧密钥验证过渡",
      "旧系统双写、双读和灰度切换一致性"
    ],
    "security": [
      "令牌伪造、篡改、重放、算法混淆和跨租户访问",
      "越权撤销、用户枚举、敏感信息泄露和日志脱敏",
      "限流、暴力刷新、异常IP和设备指纹策略"
    ],
    "reliability": [
      "数据库、缓存、密钥服务不可用时的超时和降级",
      "并发刷新、重复撤销和消息重复投递",
      "节点重启、时钟偏差、网络分区及恢复后的数据一致性"
    ],
    "performance": [
      "按目标峰值验证创建、校验、刷新和撤销吞吐",
      "验证P95/P99延迟、缓存命中率和撤销传播SLA",
      "长时间运行测试内存、连接池、索引和事件表增长"
    ],
    "acceptance": [
      "端到端覆盖登录、续期、登出、单设备撤销和全端登出",
      "灰度期间新旧系统结果一致率达到发布阈值",
      "审计事件完整、可检索且满足合规保留周期"
    ]
  }
}