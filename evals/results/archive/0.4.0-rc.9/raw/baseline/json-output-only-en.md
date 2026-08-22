{
  "components": [
    {
      "name": "Rotation API",
      "responsibilities": [
        "Authenticate and authorize rotation requests",
        "Create, activate, revoke, and inspect API keys",
        "Support idempotent requests and audit correlation IDs"
      ]
    },
    {
      "name": "Rotation Orchestrator",
      "responsibilities": [
        "Execute configurable rotation workflows",
        "Enforce overlap and expiration policies",
        "Retry recoverable failures and resume interrupted rotations"
      ]
    },
    {
      "name": "Key Generator",
      "responsibilities": [
        "Generate cryptographically secure keys",
        "Return plaintext key material only once",
        "Store only keyed hashes or encrypted key material"
      ]
    },
    {
      "name": "Secret Store Adapter",
      "responsibilities": [
        "Publish new credentials to the configured secret manager",
        "Verify secret versions before activation",
        "Support provider-specific implementations behind one interface"
      ]
    },
    {
      "name": "Scheduler and Worker Queue",
      "responsibilities": [
        "Schedule periodic rotations",
        "Process rotation jobs with leases and deduplication",
        "Move exhausted failures to a dead-letter queue"
      ]
    },
    {
      "name": "Audit and Observability",
      "responsibilities": [
        "Record immutable lifecycle events without secret values",
        "Emit metrics, structured logs, and traces",
        "Alert on failures, overdue rotations, and unexpected key usage"
      ]
    }
  ],
  "data_model": {
    "api_key": {
      "fields": {
        "id": "UUID",
        "owner_id": "UUID",
        "service_id": "UUID",
        "key_prefix": "string",
        "secret_hash": "string",
        "status": "pending|active|retiring|revoked|expired",
        "created_at": "timestamp",
        "activated_at": "nullable timestamp",
        "expires_at": "nullable timestamp",
        "revoked_at": "nullable timestamp",
        "last_used_at": "nullable timestamp",
        "version": "integer"
      },
      "constraints": [
        "Never persist plaintext API keys",
        "Allow at most the configured number of active keys per service",
        "Use optimistic locking through version"
      ]
    },
    "rotation_policy": {
      "fields": {
        "id": "UUID",
        "service_id": "UUID",
        "rotation_interval_days": "integer",
        "overlap_minutes": "integer",
        "maximum_key_age_days": "integer",
        "automatic_rotation": "boolean",
        "next_rotation_at": "timestamp",
        "updated_at": "timestamp"
      }
    },
    "rotation_job": {
      "fields": {
        "id": "UUID",
        "service_id": "UUID",
        "old_key_id": "nullable UUID",
        "new_key_id": "nullable UUID",
        "status": "queued|generating|publishing|verifying|activating|retiring|completed|failed|cancelled",
        "idempotency_key": "string",
        "attempt_count": "integer",
        "error_code": "nullable string",
        "error_detail": "nullable redacted string",
        "created_at": "timestamp",
        "updated_at": "timestamp",
        "completed_at": "nullable timestamp"
      },
      "constraints": [
        "Idempotency key is unique within the requesting tenant",
        "State transitions are validated transactionally"
      ]
    },
    "audit_event": {
      "fields": {
        "id": "UUID",
        "actor_id": "nullable UUID",
        "service_id": "UUID",
        "key_id": "nullable UUID",
        "rotation_job_id": "nullable UUID",
        "action": "string",
        "outcome": "success|failure",
        "request_id": "string",
        "metadata": "redacted JSON object",
        "created_at": "timestamp"
      }
    }
  },
  "endpoints": [
    {
      "method": "POST",
      "path": "/v1/services/{service_id}/rotations",
      "purpose": "Start a rotation",
      "requirements": [
        "Require an Idempotency-Key header",
        "Return 202 with the rotation job identifier",
        "Reject unauthorized services and conflicting active jobs"
      ]
    },
    {
      "method": "GET",
      "path": "/v1/rotations/{rotation_id}",
      "purpose": "Return rotation status and redacted failure information"
    },
    {
      "method": "POST",
      "path": "/v1/rotations/{rotation_id}/cancel",
      "purpose": "Cancel a rotation before activation or retirement"
    },
    {
      "method": "GET",
      "path": "/v1/services/{service_id}/keys",
      "purpose": "List key metadata without secret material"
    },
    {
      "method": "POST",
      "path": "/v1/services/{service_id}/keys/{key_id}/revoke",
      "purpose": "Immediately revoke a compromised or obsolete key"
    },
    {
      "method": "GET",
      "path": "/v1/services/{service_id}/rotation-policy",
      "purpose": "Retrieve the service rotation policy"
    },
    {
      "method": "PUT",
      "path": "/v1/services/{service_id}/rotation-policy",
      "purpose": "Create or update the service rotation policy"
    }
  ],
  "rollout": [
    {
      "phase": "Foundation",
      "actions": [
        "Deploy schema, audit logging, metrics, and secret-store integration behind feature flags",
        "Validate backup, recovery, and encryption controls",
        "Create operational dashboards and incident runbooks"
      ]
    },
    {
      "phase": "Shadow",
      "actions": [
        "Identify keys due for rotation without creating credentials",
        "Compare recommendations with existing manual processes",
        "Tune policies, alerts, and capacity limits"
      ]
    },
    {
      "phase": "Internal Pilot",
      "actions": [
        "Enable manual rotations for low-risk internal services",
        "Use an overlap window and verify both new-key success and old-key traffic decline",
        "Exercise rollback by retaining the old key until verification completes"
      ]
    },
    {
      "phase": "Limited Automation",
      "actions": [
        "Enable scheduled rotations for selected services",
        "Apply concurrency limits and automatic pause thresholds",
        "Require service-owner approval for high-risk integrations"
      ]
    },
    {
      "phase": "General Availability",
      "actions": [
        "Expand automation by tenant and service tier",
        "Publish service-level objectives and support procedures",
        "Enforce maximum key age after adoption targets are met"
      ]
    }
  ],
  "tests": {
    "unit": [
      "Key generation entropy and format",
      "Lifecycle state-transition validation",
      "Policy scheduling and overlap calculations",
      "Authorization and redaction rules",
      "Retry classification and backoff behavior"
    ],
    "integration": [
      "Database transactions and optimistic locking",
      "Secret-store publication and version verification",
      "Queue redelivery, lease expiration, and dead-letter handling",
      "Idempotent rotation requests under concurrent delivery",
      "Audit-event completeness without secret leakage"
    ],
    "end_to_end": [
      "Generate, publish, verify, activate, retire, and revoke a key",
      "Recover from failure before activation while preserving the old key",
      "Resume an interrupted job without creating duplicate active keys",
      "Cancel eligible rotations and reject cancellation after irreversible steps",
      "Perform emergency revocation and confirm immediate authentication failure"
    ],
    "security": [
      "Attempt privilege escalation across tenants and services",
      "Scan logs, traces, errors, and database fields for plaintext secrets",
      "Validate encryption, key-hash verification, rate limiting, and replay protection",
      "Test compromised worker and secret-store permission boundaries"
    ],
    "resilience": [
      "Inject database, queue, network, and secret-store outages",
      "Verify rollback and recovery after partial publication",
      "Load-test scheduled rotation bursts and API throttling",
      "Confirm alerts for stuck jobs, repeated failures, and overdue rotations"
    ]
  }
}