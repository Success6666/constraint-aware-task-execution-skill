{
  "components": {
    "api_service": {
      "responsibilities": [
        "Authenticate callers using an existing valid API key or privileged service identity",
        "Create, list, revoke, rotate, and inspect API keys",
        "Enforce ownership, scopes, tenant isolation, rate limits, and idempotency",
        "Return plaintext key material only at creation or rotation time"
      ],
      "rotation_behavior": {
        "default": "Create a replacement key, keep the previous key active during a configurable grace period, then revoke the previous key",
        "atomicity": "Replacement creation and predecessor transition are committed in one database transaction",
        "grace_period": "Configurable per tenant with a service default, minimum, and maximum",
        "immediate_revocation": "Supported through an explicit endpoint and rotation option",
        "maximum_active_keys": "Configurable per owner and tenant"
      }
    },
    "key_generator": {
      "format": "Opaque randomly generated token with a public key identifier prefix and a cryptographically random secret",
      "randomness": "Use a cryptographically secure random generator with at least 256 bits of entropy",
      "storage": "Store only a keyed hash or memory-hard hash of the secret; never store plaintext key material",
      "display": "Return the complete secret once and a masked suffix thereafter"
    },
    "key_validator": {
      "responsibilities": [
        "Parse key prefix and secret",
        "Look up the key record by identifier",
        "Verify the secret hash",
        "Check status, expiration, tenant, scopes, and revocation timestamp",
        "Update last-used metadata asynchronously"
      ],
      "performance": "Use an indexed key identifier lookup followed by constant-time hash verification",
      "cache": "Optional short-lived positive cache keyed by key identifier; revocations must invalidate or bypass cached authorization within the configured revocation SLA"
    },
    "rotation_scheduler": {
      "responsibilities": [
        "Find keys whose grace period has expired",
        "Revoke eligible predecessor keys",
        "Emit expiration and revocation audit events",
        "Retry safely using idempotent state transitions"
      ],
      "execution": "Use a durable job queue or scheduled worker with bounded retries and dead-letter handling"
    },
    "audit_service": {
      "events": [
        "key.created",
        "key.rotated",
        "key.rotation_requested",
        "key.revoked",
        "key.expired",
        "key.validation_failed",
        "key.grace_period_completed"
      ],
      "properties": [
        "event_id",
        "tenant_id",
        "actor_type",
        "actor_id",
        "key_id",
        "predecessor_key_id",
        "request_id",
        "source_ip",
        "user_agent",
        "timestamp",
        "outcome",
        "reason"
      ],
      "retention": "Retain according to the tenant security and compliance policy; make audit records append-only"
    },
    "notification_service": {
      "responsibilities": [
        "Notify administrators when a key is created, rotated, revoked, or nearing expiration",
        "Never include plaintext key material in notifications",
        "Deliver through configured webhook, email, or administrative event stream"
      ]
    },
    "administration_controls": {
      "requirements": [
        "Require tenant or owner authorization for all management operations",
        "Require step-up authentication or a privileged service identity for bulk rotation and immediate revocation",
        "Support tenant-level defaults for grace period, maximum lifetime, scopes, and notification policy"
      ]
    }
  },
  "data_model": {
    "api_keys": {
      "primary_key": "key_id",
      "fields": {
        "key_id": "string, immutable public identifier",
        "tenant_id": "string, required, indexed",
        "owner_id": "string, required, indexed",
        "name": "string, required, tenant-unique",
        "secret_hash": "binary/string, required, encrypted or protected at rest",
        "hash_version": "string, required",
        "status": "enum: active, grace, revoked, expired",
        "scopes": "array of validated scope identifiers",
        "created_at": "timestamp, required",
        "expires_at": "timestamp, nullable",
        "last_used_at": "timestamp, nullable",
        "last_used_ip": "string, nullable, subject to privacy policy",
        "revoked_at": "timestamp, nullable",
        "revoked_by": "string, nullable",
        "revocation_reason": "string, nullable",
        "predecessor_key_id": "string, nullable, indexed",
        "successor_key_id": "string, nullable, indexed",
        "grace_ends_at": "timestamp, nullable",
        "metadata": "JSON object with constrained size and approved fields",
        "created_request_id": "string, required, indexed"
      },
      "constraints": [
        "A key cannot transition from revoked or expired back to active",
        "grace status requires grace_ends_at and predecessor/successor relationship",
        "expires_at must be later than created_at",
        "revoked_at is immutable once set",
        "At most one active or grace key with the same tenant_id, owner_id, and name unless explicitly configured otherwise",
        "successor_key_id and predecessor_key_id must form valid one-to-one rotation links"
      ]
    },
    "rotation_operations": {
      "primary_key": "rotation_id",
      "fields": {
        "rotation_id": "string",
        "tenant_id": "string",
        "owner_id": "string",
        "source_key_id": "string",
        "replacement_key_id": "string",
        "requested_by": "string",
        "requested_at": "timestamp",
        "grace_ends_at": "timestamp, nullable",
        "immediate_revoke": "boolean",
        "status": "enum: completed, failed",
        "idempotency_key": "string, required"
      },
      "constraints": [
        "Unique on tenant_id, requested_by, and idempotency_key",
        "A completed operation always references an existing replacement key"
      ]
    },
    "idempotency_records": {
      "primary_key": "tenant_id plus idempotency_key",
      "fields": {
        "request_hash": "string",
        "response_status": "integer",
        "response_body": "JSON",
        "created_at": "timestamp",
        "expires_at": "timestamp"
      },
      "behavior": "Reuse the original response for an identical retry and reject reuse with a different request body"
    },
    "audit_events": {
      "primary_key": "event_id",
      "fields": {
        "event_id": "string",
        "event_type": "string",
        "tenant_id": "string",
        "actor_id": "string",
        "actor_type": "string",
        "key_id": "string, nullable",
        "rotation_id": "string, nullable",
        "request_id": "string",
        "occurred_at": "timestamp",
        "payload": "JSON without secrets"
      }
    },
    "indexes": [
      "api_keys(key_id)",
      "api_keys(tenant_id, owner_id, status)",
      "api_keys(tenant_id, name)",
      "api_keys(status, grace_ends_at)",
      "api_keys(expires_at)",
      "rotation_operations(tenant_id, idempotency_key)"
    ]
  },
  "endpoints": {
    "POST /v1/api-keys": {
      "purpose": "Create an API key",
      "authentication": "Privileged tenant or owner authorization",
      "request": {
        "name": "string",
        "scopes": "array",
        "expires_at": "timestamp, optional",
        "metadata": "object, optional"
      },
      "headers": [
        "Authorization",
        "Idempotency-Key"
      ],
      "response": {
        "status": 201,
        "body": [
          "key_id",
          "name",
          "secret",
          "secret_expires_at",
          "scopes",
          "created_at",
          "expires_at"
        ]
      },
      "security": "The secret is returned only in this response and is never returned by list or get endpoints"
    },
    "GET /v1/api-keys": {
      "purpose": "List keys visible to the caller",
      "query_parameters": [
        "owner_id",
        "status",
        "limit",
        "cursor"
      ],
      "response": {
        "status": 200,
        "body": [
          "items containing key_id, name, status, scopes, created_at, expires_at, last_used_at, grace_ends_at, predecessor_key_id, successor_key_id, masked_suffix",
          "next_cursor"
        ]
      }
    },
    "GET /v1/api-keys/{key_id}": {
      "purpose": "Retrieve non-secret metadata for one key",
      "response": {
        "status": 200,
        "body": [
          "key_id",
          "tenant_id",
          "owner_id",
          "name",
          "status",
          "scopes",
          "created_at",
          "expires_at",
          "last_used_at",
          "revoked_at",
          "grace_ends_at",
          "predecessor_key_id",
          "successor_key_id",
          "masked_suffix"
        ]
      }
    },
    "POST /v1/api-keys/{key_id}/rotate": {
      "purpose": "Create a successor key and transition the current key",
      "authentication": "The current key with rotation scope or privileged tenant authorization",
      "request": {
        "grace_period_seconds": "integer, optional",
        "immediate_revoke": "boolean, default false",
        "name": "string, optional",
        "scopes": "array, optional",
        "expires_at": "timestamp, optional"
      },
      "headers": [
        "Authorization",
        "Idempotency-Key"
      ],
      "transaction": [
        "Lock the source key row",
        "Verify source status and authorization",
        "Generate and hash the successor secret",
        "Create the successor",
        "Set source status to revoked if immediate_revoke is true, otherwise grace",
        "Set source grace_ends_at when applicable",
        "Create predecessor and successor links",
        "Write the rotation record and audit event"
      ],
      "response": {
        "status": 201,
        "body": [
          "rotation_id",
          "replacement_key_id",
          "replacement_secret",
          "source_key_id",
          "source_status",
          "grace_ends_at",
          "expires_at"
        ]
      },
      "errors": {
        "409": "Concurrent rotation or conflicting idempotency request",
        "422": "Invalid scopes, lifetime, or grace period",
        "423": "Source key is already revoked or expired"
      }
    },
    "POST /v1/api-keys/{key_id}/revoke": {
      "purpose": "Revoke a key immediately",
      "authentication": "Privileged tenant authorization or key with revocation scope",
      "request": {
        "reason": "string, required"
      },
      "headers": [
        "Authorization",
        "Idempotency-Key"
      ],
      "response": {
        "status": 204
      },
      "behavior": "The operation is idempotent for an already revoked key and invalidates validation caches"
    },
    "POST /v1/api-keys/{key_id}/test": {
      "purpose": "Validate key ownership and configuration without returning secret material",
      "authentication": "The key being tested",
      "response": {
        "status": 200,
        "body": [
          "key_id",
          "valid",
          "status",
          "scopes",
          "expires_at"
        ]
      },
      "security": "Do not expose detailed failure reasons to unauthenticated callers"
    },
    "POST /v1/api-keys/bulk-rotate": {
      "purpose": "Schedule rotation for multiple keys",
      "authentication": "Tenant administrator or platform operator with explicit bulk-rotation permission",
      "request": {
        "selector": "owner, name, scope, status, or expiration criteria",
        "grace_period_seconds": "integer, optional",
        "notify": "boolean, default true"
      },
      "response": {
        "status": 202,
        "body": [
          "job_id",
          "accepted_count",
          "rejected_count"
        ]
      },
      "behavior": "Each key rotation is independently idempotent, audited, rate-limited, and reported through job status"
    },
    "GET /v1/api-key-rotation-jobs/{job_id}": {
      "purpose": "Retrieve bulk rotation progress",
      "response": {
        "status": 200,
        "body": [
          "job_id",
          "status",
          "total",
          "completed",
          "failed",
          "results_without_secrets"
        ]
      }
    },
    "GET /v1/api-key-policy": {
      "purpose": "Retrieve effective tenant rotation policy",
      "authentication": "Tenant administrator",
      "response": {
        "status": 200,
        "body": [
          "default_grace_period_seconds",
          "minimum_grace_period_seconds",
          "maximum_grace_period_seconds",
          "maximum_key_lifetime_seconds",
          "maximum_active_keys",
          "required_scopes",
          "notification_policy"
        ]
      }
    },
    "common_errors": {
      "400": "Malformed request",
      "401": "Missing or invalid authentication",
      "403": "Caller lacks required permission",
      "404": "Resource not found or intentionally hidden",
      "409": "Conflict or idempotency mismatch",
      "413": "Metadata or request exceeds size limits",
      "429": "Rate limit exceeded",
      "500": "Unexpected server error"
    }
  },
  "rollout": {
    "phase_1_foundation": [
      "Define key format, scope registry, tenant policy defaults, and threat model",
      "Implement schema, hashing, key generation, validation, audit events, and authorization",
      "Add feature flags for key creation, rotation, immediate revocation, and bulk rotation"
    ],
    "phase_2_shadow_validation": [
      "Instrument existing authentication to record key identifiers, usage frequency, and invalid-key attempts without changing behavior",
      "Backfill metadata for existing keys where possible",
      "Measure cache invalidation and audit delivery latency",
      "Verify that no logs, traces, metrics, or error reports contain plaintext secrets"
    ],
    "phase_3_limited_enablement": [
      "Enable rotation for internal tenants and selected low-risk tenants",
      "Use a conservative grace period and enforce maximum key lifetime",
      "Monitor rotation success rate, duplicate operations, validation failures, revocation latency, and notification delivery",
      "Provide administrative rollback by disabling new rotations while preserving already-created key records"
    ],
    "phase_4_general_availability": [
      "Enable self-service rotation for all eligible tenants",
      "Enable scheduled and bulk rotation behind separate permissions",
      "Publish client migration guidance requiring clients to deploy the successor before grace expiration",
      "Require audit event delivery and alerting for anomalous rotation volume"
    ],
    "phase_5_enforcement": [
      "Require expiration for newly created keys",
      "Prompt or force rotation of keys exceeding the maximum lifetime",
      "Disable legacy key formats after an announced migration window",
      "Remove compatibility paths only after usage has reached zero and recovery procedures are verified"
    ],
    "operational_requirements": [
      "Back up metadata but exclude recoverable plaintext secrets",
      "Document emergency revocation, tenant lockout recovery, and compromised signing or hashing material procedures",
      "Use separate permissions for viewing metadata, rotating keys, revoking keys, and changing policy",
      "Set an explicit revocation propagation SLO and alert when exceeded"
    ]
  },
  "tests": {
    "unit": [
      "Generate keys with required entropy and valid format",
      "Verify hashing and constant-time secret comparison",
      "Reject malformed, expired, revoked, wrong-tenant, and insufficient-scope keys",
      "Validate scope, expiration, grace-period, metadata, and maximum-key policies",
      "Verify all legal and illegal status transitions",
      "Verify masked secret rendering and absence of plaintext in serialized models"
    ],
    "integration": [
      "Create a key and authenticate successfully with the returned secret",
      "Rotate a key and verify both predecessor and successor behavior during grace",
      "Verify predecessor rejection after grace expiration",
      "Verify immediate rotation revokes the predecessor atomically",
      "Verify concurrent rotations produce one successor or a defined conflict",
      "Verify revoke invalidates active validation caches within the stated SLO",
      "Verify idempotent retries return the original response without creating duplicate keys",
      "Verify idempotency-key reuse with a changed payload is rejected",
      "Verify tenant and owner authorization boundaries",
      "Verify audit events contain required fields and no secrets",
      "Verify notification payloads never include plaintext secrets"
    ],
    "contract": [
      "Validate OpenAPI schemas for all success and error responses",
      "Test pagination cursors, filtering, and stable ordering",
      "Test clients can distinguish grace, revoked, and expired statuses",
      "Test backward compatibility for existing authentication headers and key formats"
    ],
    "security": [
      "Attempt brute-force validation and verify rate limiting and alerting",
      "Test cross-tenant identifier probing and consistent not-found behavior",
      "Scan application logs, traces, metrics, database exports, queue messages, and error reporting for secret leakage",
      "Test replay of captured requests and idempotency behavior",
      "Test authorization bypass through owner, tenant, bulk, and policy endpoints",
      "Run dependency, static-analysis, secret-scanning, and penetration tests"
    ],
    "reliability": [
      "Crash during rotation transaction and verify no partial successor state",
      "Retry scheduler jobs and verify no duplicate revocations or audit events",
      "Delay audit, notification, cache, and queue dependencies and verify bounded behavior",
      "Restore metadata backups and verify active keys remain usable only when their hashes are intact",
      "Load-test validation, listing, and bulk rotation with configured tenant limits"
    ],
    "acceptance_criteria": [
      "Plaintext secret is returned exactly once per successful create or rotate response",
      "No key can be used after revocation propagation completes",
      "Rotation is atomic and idempotent",
      "Every lifecycle transition has an append-only audit event",
      "Existing clients can deploy a successor during the grace period without downtime",
      "All configured policy limits and tenant isolation rules are enforced"
    ]
  }
}
