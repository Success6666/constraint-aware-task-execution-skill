{
  "components": {
    "api_service": [
      "Authenticate administrators or service principals.",
      "Create, list, rotate, and revoke keys.",
      "Enforce tenant ownership, scopes, expiration, and idempotency."
    ],
    "key_manager": [
      "Generate 256-bit cryptographically secure random secrets.",
      "Return each plaintext secret only once.",
      "Store only a keyed HMAC-SHA-256 digest and a non-secret lookup prefix.",
      "Perform rotation and revocation in database transactions."
    ],
    "authentication_middleware": [
      "Extract the key from the Authorization header.",
      "Resolve by key ID or prefix, verify the digest, status, expiration, tenant, and required scope.",
      "Emit authentication audit events without logging secrets."
    ],
    "audit_service": [
      "Record creation, rotation, revocation, use failures, and administrative actor identity.",
      "Publish structured events with tenant ID, key ID, request ID, timestamp, and result."
    ],
    "scheduled_worker": [
      "Expire keys and completed rotation grace periods.",
      "Emit alerts for keys nearing expiration and repeated failed authentication."
    ],
    "storage": [
      "Use a relational database with unique constraints and transactional updates.",
      "Use encrypted transport and encrypted database backups.",
      "Keep the HMAC pepper in a managed secret store, separate from the database."
    ]
  },
  "data_model": {
    "api_keys": {
      "id": "UUID primary key",
      "tenant_id": "UUID indexed",
      "name": "string",
      "key_prefix": "string indexed",
      "secret_digest": "binary HMAC-SHA-256 digest",
      "scopes": "array of normalized scope strings",
      "status": "active | rotating | revoked | expired",
      "created_at": "timestamp",
      "expires_at": "nullable timestamp",
      "last_used_at": "nullable timestamp",
      "revoked_at": "nullable timestamp",
      "revoked_by": "nullable actor ID",
      "rotation_parent_id": "nullable UUID",
      "rotation_grace_ends_at": "nullable timestamp",
      "metadata": "JSON object"
    },
    "rotation_records": {
      "id": "UUID primary key",
      "tenant_id": "UUID",
      "old_key_id": "UUID",
      "new_key_id": "UUID",
      "requested_by": "actor ID",
      "requested_at": "timestamp",
      "grace_ends_at": "timestamp",
      "completed_at": "nullable timestamp",
      "idempotency_key": "string unique per tenant and operation"
    },
    "audit_events": {
      "id": "UUID primary key",
      "tenant_id": "UUID",
      "actor_id": "nullable ID",
      "event_type": "string",
      "key_id": "nullable UUID",
      "request_id": "string",
      "result": "success | failure",
      "details": "JSON object without secrets",
      "created_at": "timestamp"
    },
    "constraints": [
      "Each key ID is globally unique.",
      "Key prefixes are non-secret and may be reused only according to the lookup strategy.",
      "A tenant may have at most one active rotation per key.",
      "Revoked keys cannot become active again.",
      "Scopes and metadata are validated and normalized before persistence."
    ]
  },
  "endpoints": {
    "POST /v1/tenants/{tenant_id}/api-keys": {
      "purpose": "Create an API key.",
      "request": {
        "name": "string",
        "scopes": "array of strings",
        "expires_at": "optional timestamp",
        "metadata": "optional object"
      },
      "response": {
        "status": 201,
        "body": "key_id, name, key_prefix, scopes, expires_at, created_at, secret"
      },
      "rules": [
        "Require administrative authorization.",
        "Return secret only in this response.",
        "Support Idempotency-Key and return the original result for retries."
      ]
    },
    "GET /v1/tenants/{tenant_id}/api-keys": {
      "purpose": "List keys without secrets.",
      "query": "status, limit, cursor",
      "response": {
        "status": 200,
        "body": "paginated key metadata including status and last_used_at"
      }
    },
    "POST /v1/tenants/{tenant_id}/api-keys/{key_id}/rotate": {
      "purpose": "Create a replacement key while preserving the old key during a grace period.",
      "request": {
        "grace_period_seconds": "optional bounded integer",
        "expires_at": "optional timestamp",
        "scopes": "optional replacement scope set",
        "name": "optional string"
      },
      "response": {
        "status": 201,
        "body": "rotation_id, old_key_id, new_key_id, grace_ends_at, new secret"
      },
      "rules": [
        "Require administrative authorization.",
        "Atomically create the replacement and mark the old key rotating.",
        "Allow both keys until grace_ends_at.",
        "After grace_ends_at, mark the old key expired.",
        "Use Idempotency-Key to make retries return the same replacement."
      ]
    },
    "POST /v1/tenants/{tenant_id}/api-keys/{key_id}/revoke": {
      "purpose": "Immediately disable a key.",
      "request": {
        "reason": "optional string"
      },
      "response": {
        "status": 204
      },
      "rules": [
        "Require administrative authorization.",
        "Make revocation immediately effective for all authentication requests.",
        "Make repeated revocation idempotent."
      ]
    },
    "GET /v1/tenants/{tenant_id}/api-keys/{key_id}": {
      "purpose": "Retrieve non-secret key metadata and rotation state.",
      "response": {
        "status": 200,
        "body": "key metadata, status, and rotation details"
      }
    },
    "POST /v1/auth/validate": {
      "purpose": "Internal validation endpoint for services that cannot use shared authentication middleware.",
      "request": {
        "api_key": "string",
        "required_scopes": "array of strings"
      },
      "response": {
        "status": 200,
        "body": "authenticated, tenant_id, key_id, scopes"
      },
      "rules": [
        "Restrict access to trusted internal callers.",
        "Never return the submitted key or its digest.",
        "Return a generic 401 response for invalid, expired, or revoked credentials."
      ]
    },
    "common_security_rules": [
      "Require TLS.",
      "Never log, persist, or include secrets in telemetry.",
      "Apply tenant isolation, administrative authorization, rate limits, and request IDs.",
      "Use generic authentication failure responses to prevent key enumeration."
    ]
  },
  "rollout": {
    "phase_1": [
      "Create schema, secret-store integration, key generation, hashing, and audit events.",
      "Implement create, list, revoke, and validation paths.",
      "Add metrics for issuance, validation failures, revocations, and latency."
    ],
    "phase_2": [
      "Implement transactional rotation with configurable default and maximum grace periods.",
      "Deploy scheduled expiration processing.",
      "Enable alerts for upcoming expiration and abnormal failure rates."
    ],
    "phase_3": [
      "Run compatibility tests with all consumers.",
      "Enable rotation for internal tenants first, then progressively for external tenants.",
      "Require new consumers to use rotation and expiration policies."
    ],
    "operational_controls": [
      "Use feature flags for rotation and grace-period enforcement.",
      "Back up database and audit data before production enablement.",
      "Provide an emergency revoke-all operation restricted to a break-glass role.",
      "Monitor active-key counts, rotation completion, expired-key attempts, and audit delivery."
    ]
  },
  "tests": {
    "unit": [
      "Generate unpredictable secrets and verify digest calculation.",
      "Verify scope, expiration, status, and tenant authorization decisions.",
      "Verify rotation state transitions and grace-period boundaries.",
      "Verify secret redaction from logs and audit payloads."
    ],
    "integration": [
      "Create a key and authenticate with it.",
      "Verify the plaintext secret is returned once and never from read endpoints.",
      "Rotate a key and verify both keys work during grace, then only the replacement works.",
      "Revoke a key and verify authentication fails immediately.",
      "Verify expiration, invalid scopes, malformed credentials, and cross-tenant access denial.",
      "Verify database uniqueness and transaction rollback behavior."
    ],
    "api": [
      "Validate request schemas, status codes, pagination, and error formats.",
      "Verify Idempotency-Key behavior under retries and concurrent requests.",
      "Verify authorization, rate limits, TLS requirements, and generic authentication errors."
    ],
    "security": [
      "Test brute-force resistance and rate-limit enforcement.",
      "Confirm secrets are absent from application logs, traces, metrics, backups, and audit events.",
      "Test pepper rotation procedure and digest migration.",
      "Run dependency, static-analysis, and penetration tests."
    ],
    "reliability": [
      "Test concurrent rotations and revocations.",
      "Test worker retries and duplicate expiration processing.",
      "Test database failover during issuance and rotation.",
      "Verify audit delivery retry and alerting behavior."
    ]
  }
}
