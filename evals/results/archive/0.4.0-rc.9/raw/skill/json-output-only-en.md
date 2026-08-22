{
  "components": [
    {
      "name": "Rotation API",
      "responsibilities": [
        "Accept authenticated rotation requests",
        "Validate key ownership and rotation eligibility",
        "Return rotation status without exposing key material after creation"
      ]
    },
    {
      "name": "Rotation Orchestrator",
      "responsibilities": [
        "Create replacement keys",
        "Coordinate activation, overlap, and revocation",
        "Retry idempotent steps and record failures"
      ]
    },
    {
      "name": "Key Provider Adapter",
      "responsibilities": [
        "Create, activate, and revoke keys in each supported provider",
        "Normalize provider errors and identifiers"
      ]
    },
    {
      "name": "Secret Distributor",
      "responsibilities": [
        "Write replacement credentials to the configured secret store",
        "Notify or trigger consumers to reload credentials",
        "Verify consumer adoption before revocation"
      ]
    },
    {
      "name": "Scheduler and Worker",
      "responsibilities": [
        "Start rotations based on key age and policy",
        "Process queued rotation jobs",
        "Recover interrupted workflows"
      ]
    },
    {
      "name": "Audit and Monitoring",
      "responsibilities": [
        "Record immutable rotation events",
        "Publish latency, success, failure, and overdue-key metrics",
        "Alert on failed rotations and expired overlap windows"
      ]
    }
  ],
  "data_model": {
    "api_keys": {
      "fields": {
        "id": "UUID",
        "tenant_id": "UUID",
        "provider": "string",
        "provider_key_id": "string",
        "secret_reference": "string",
        "status": "pending|active|retiring|revoked|failed",
        "created_at": "timestamp",
        "activated_at": "timestamp|null",
        "expires_at": "timestamp|null",
        "last_rotated_at": "timestamp|null",
        "version": "integer"
      },
      "constraints": [
        "Key material is stored only in the secret store",
        "At most one active key per tenant, provider, and credential slot unless overlap is enabled"
      ]
    },
    "rotation_policies": {
      "fields": {
        "id": "UUID",
        "tenant_id": "UUID",
        "provider": "string",
        "rotation_interval_days": "integer",
        "overlap_minutes": "integer",
        "verification_timeout_minutes": "integer",
        "enabled": "boolean",
        "next_rotation_at": "timestamp"
      }
    },
    "rotation_jobs": {
      "fields": {
        "id": "UUID",
        "api_key_id": "UUID",
        "idempotency_key": "string",
        "state": "queued|creating|distributing|verifying|revoking|completed|failed",
        "replacement_key_id": "UUID|null",
        "attempt_count": "integer",
        "last_error_code": "string|null",
        "started_at": "timestamp|null",
        "completed_at": "timestamp|null",
        "created_at": "timestamp"
      }
    },
    "audit_events": {
      "fields": {
        "id": "UUID",
        "tenant_id": "UUID",
        "rotation_job_id": "UUID|null",
        "actor_type": "user|service|scheduler",
        "actor_id": "string",
        "event_type": "string",
        "metadata": "JSON",
        "created_at": "timestamp"
      }
    }
  },
  "endpoints": [
    {
      "method": "POST",
      "path": "/v1/keys/{key_id}/rotations",
      "purpose": "Start an idempotent rotation",
      "request": {
        "idempotency_key": "string",
        "overlap_minutes": "integer|null"
      },
      "responses": {
        "202": "Rotation accepted",
        "404": "Key not found",
        "409": "Rotation already in progress"
      }
    },
    {
      "method": "GET",
      "path": "/v1/rotations/{rotation_id}",
      "purpose": "Retrieve workflow state, timestamps, and sanitized errors"
    },
    {
      "method": "POST",
      "path": "/v1/rotations/{rotation_id}/retry",
      "purpose": "Retry a failed rotation from the last safe checkpoint"
    },
    {
      "method": "POST",
      "path": "/v1/rotations/{rotation_id}/cancel",
      "purpose": "Cancel before the old key is revoked and clean up unused replacement credentials"
    },
    {
      "method": "GET",
      "path": "/v1/keys",
      "purpose": "List key metadata, status, age, and next scheduled rotation"
    },
    {
      "method": "PUT",
      "path": "/v1/rotation-policies/{provider}",
      "purpose": "Create or update a tenant rotation policy"
    },
    {
      "method": "GET",
      "path": "/v1/audit-events",
      "purpose": "Query tenant-scoped rotation history"
    }
  ],
  "rollout": [
    {
      "phase": 1,
      "scope": "Foundation",
      "actions": [
        "Implement one provider adapter and one secret-store integration",
        "Enable manual rotations in a development environment",
        "Verify audit redaction and idempotent recovery"
      ]
    },
    {
      "phase": 2,
      "scope": "Internal canary",
      "actions": [
        "Rotate non-production credentials with an overlap window",
        "Measure consumer adoption and provider error rates",
        "Exercise rollback and interrupted-job recovery"
      ]
    },
    {
      "phase": 3,
      "scope": "Production canary",
      "actions": [
        "Enable selected low-risk tenants",
        "Require successful replacement-key verification before revocation",
        "Monitor alerts, latency, and authentication failures"
      ]
    },
    {
      "phase": 4,
      "scope": "Scheduled adoption",
      "actions": [
        "Enable policy-based scheduling by tenant",
        "Increase tenant coverage gradually",
        "Publish operational runbooks and support procedures"
      ]
    },
    {
      "phase": 5,
      "scope": "General availability",
      "actions": [
        "Enable supported providers broadly",
        "Track overdue credentials and service-level objectives",
        "Periodically test provider adapters and recovery procedures"
      ]
    }
  ],
  "tests": {
    "unit": [
      "State-machine transitions and invalid-transition rejection",
      "Rotation eligibility and scheduling calculations",
      "Provider error normalization",
      "Audit-field redaction",
      "Idempotency-key handling"
    ],
    "integration": [
      "Create, distribute, verify, and revoke against provider sandboxes",
      "Secret-store version creation and retrieval",
      "Worker restart at every workflow checkpoint",
      "Retry behavior for timeouts, rate limits, and partial provider failures"
    ],
    "end_to_end": [
      "Manual rotation with successful overlap",
      "Scheduled rotation across multiple tenants",
      "Consumer verification failure preserves the old key",
      "Cancellation removes an unused replacement key",
      "Repeated requests return the same rotation job"
    ],
    "security": [
      "Tenant isolation and authorization checks",
      "No key material in logs, database records, metrics, or API responses",
      "Secret-store access uses least-privilege identities",
      "Audit events capture actors and security-sensitive state changes",
      "Replay and forged-callback attempts are rejected"
    ],
    "operational": [
      "Load test scheduled rotation bursts",
      "Alert on failures, overdue rotations, and stuck jobs",
      "Verify dashboards and runbooks during a failure drill",
      "Confirm recovery after database, queue, provider, and secret-store outages"
    ]
  }
}