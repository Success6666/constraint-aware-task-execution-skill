# Hourly JSONL ETL Design

## 1. Technology

- Python 3.12
- `orjson` for JSONL decoding
- `jsonschema` or `fastjsonschema` for validation
- DuckDB for durable event state, deduplication, and checkpoint metadata
- PyArrow for Parquet writing and schema enforcement
- An hourly scheduler such as cron, Airflow, or a container scheduler
- No Apache Spark

DuckDB is the system of record for accepted events and processing state. Parquet is the published analytical output.

---

## 2. Event contract

Each JSONL record must conform to a versioned JSON Schema.

Example event:

```json
{
  "event_id": "01J8M2K4D7Y7Q5G6H8J9K0M1N2",
  "event_type": "purchase",
  "event_ts": "2025-01-15T13:42:11.123Z",
  "customer_id": "cust-123",
  "amount": 42.50,
  "currency": "USD",
  "attributes": {
    "channel": "web"
  },
  "schema_version": 1
}
```

Required rules:

- `event_id`: non-empty string, globally unique, maximum 128 characters
- `event_type`: non-empty string
- `event_ts`: RFC 3339 timestamp with a UTC offset; normalize to UTC
- `customer_id`: non-empty string
- `amount`: non-negative number
- `currency`: three-letter uppercase string
- `attributes`: JSON object
- `schema_version`: integer currently equal to `1`
- Reject unknown top-level fields unless explicitly allowed by a schema revision

Normalized analytical columns:

```text
event_id        VARCHAR
event_type      VARCHAR
event_ts        TIMESTAMP WITH TIME ZONE
customer_id     VARCHAR
amount          DECIMAL(18, 2)
currency        VARCHAR
attributes      JSON
schema_version INTEGER
event_date      DATE
event_hour      INTEGER
payload_hash    VARCHAR
ingest_batch_id VARCHAR
ingested_at     TIMESTAMP WITH TIME ZONE
```

---

## 3. Processing flow

Each hourly run performs the following steps.

### Step 1: Discover the hourly input

Use a deterministic batch identifier:

```text
batch_id = <source>/<UTC logical hour>/<input content hash>
```

The batch metadata includes:

- Logical UTC hour
- Source identifier
- Content checksum or object version
- Discovery timestamp
- Processing attempt count

If the same batch is seen again, it is not reprocessed as a new batch.

### Step 2: Stream and decode JSONL

Read one line at a time.

For every line:

1. Decode JSON.
2. Validate against the JSON Schema.
3. Normalize timestamp and numeric fields.
4. Compute `payload_hash` from canonical JSON.
5. Derive:
   - `event_date = date(event_ts)`
   - `event_hour = hour(event_ts)`
6. Send invalid records to the rejection dataset with:
   - `batch_id`
   - line number
   - raw record
   - rejection reason
   - validation path

Malformed JSON, schema violations, invalid timestamps, and invalid numeric values are rejected individually. One bad record does not discard the batch.

### Step 3: Deduplicate

Deduplication uses `event_id` as the business key.

Rules:

- The first valid occurrence of an `event_id` becomes canonical.
- Repeated occurrences with the same `payload_hash` are counted as duplicates and ignored.
- Repeated occurrences with a different `payload_hash` are written to a conflict dataset and do not replace the canonical event.
- Deduplication applies across all historical batches, not only within the current hour.

### Step 4: Commit accepted events

Use one DuckDB transaction:

1. Insert the batch record if it does not already exist.
2. Insert accepted events with a uniqueness constraint on `event_id`.
3. Record duplicate and conflict counts.
4. Mark the batch `COMMITTED`.
5. Record all affected `(event_date, event_hour)` partitions and the commit generation.

Conceptual tables:

```sql
CREATE TABLE batches (
    batch_id VARCHAR PRIMARY KEY,
    logical_hour TIMESTAMP WITH TIME ZONE NOT NULL,
    input_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    valid_count BIGINT DEFAULT 0,
    rejected_count BIGINT DEFAULT 0,
    duplicate_count BIGINT DEFAULT 0,
    conflict_count BIGINT DEFAULT 0,
    committed_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE events (
    event_id VARCHAR PRIMARY KEY,
    event_type VARCHAR NOT NULL,
    event_ts TIMESTAMP WITH TIME ZONE NOT NULL,
    customer_id VARCHAR NOT NULL,
    amount DECIMAL(18,2) NOT NULL,
    currency VARCHAR NOT NULL,
    attributes JSON NOT NULL,
    schema_version INTEGER NOT NULL,
    event_date DATE NOT NULL,
    event_hour INTEGER NOT NULL,
    payload_hash VARCHAR NOT NULL,
    ingest_batch_id VARCHAR NOT NULL,
    ingested_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE partition_state (
    event_date DATE NOT NULL,
    event_hour INTEGER NOT NULL,
    desired_generation BIGINT NOT NULL,
    published_generation BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (event_date, event_hour)
);
```

The transaction must commit before any Parquet publication begins.

---

## 4. Parquet layout

Partition by the event timestamp, not ingestion time:

```text
event_date=2025-01-15/event_hour=13/
```

Each partition contains canonical events for that UTC date and hour.

Recommended Parquet settings:

- Compression: Zstandard
- Dictionary encoding for low-cardinality strings
- Explicit Arrow schema matching the event contract
- Stable sort by `event_id`
- Row groups of approximately 64–128 MB
- One current published dataset per partition generation

Late-arriving events are handled by rebuilding the affected partition. This prevents duplicate append output and ensures that each published partition contains the complete canonical set.

For local or POSIX storage:

1. Write the complete partition to a temporary name.
2. Validate row count and schema.
3. Atomically replace the published partition.
4. Update `published_generation`.

For object storage:

1. Write a versioned Parquet object.
2. Validate it.
3. Publish a manifest containing the active object version using a conditional write.
4. Update `published_generation`.

Readers must use the manifest or published generation rather than partially written output.

---

## 5. Checkpointed recovery

The checkpoint is the committed database state, not the scheduler’s process state.

A run is considered complete only when:

1. The batch is `COMMITTED`.
2. Every affected partition has `published_generation >= desired_generation`.
3. The batch is marked `PUBLISHED`.

Recovery behavior:

- If the process fails before the DuckDB transaction commits, retry the batch normally.
- If it fails after the transaction commits but before Parquet publication, the batch remains committed and is not inserted again.
- If it fails during partition publication, compare `desired_generation` and `published_generation`, then rebuild only missing or stale partitions.
- If it fails after publication but before the final checkpoint update, rechecking the generation makes the operation idempotent.
- A retry of an already published batch performs no event duplication.

The scheduler should allow retries with exponential backoff and should never delete committed state automatically.

---

## 6. Hourly orchestration

Each hourly invocation:

1. Process the current UTC hour.
2. Discover newly available batches for that hour.
3. Retry previously committed-but-unpublished batches.
4. Optionally scan a configurable late-arrival window, such as the previous 48 hours.
5. Publish all stale affected partitions.
6. Emit metrics and a run summary.

Suggested configuration:

```yaml
schedule: "hourly"
timezone: "UTC"
late_arrival_hours: 48
max_retries: 5
parquet_compression: "zstd"
schema_version: 1
reject_invalid_records: true
conflict_policy: "quarantine"
```

A completeness watermark may be added separately. For example, an hour can be marked complete only after no new input has appeared for two consecutive runs. This should not prevent late events from being processed later.

---

## 7. Operational metrics

Emit metrics per batch and per run:

- Input record count
- Valid record count
- Rejected record count
- Duplicate count
- Conflict count
- Newly accepted event count
- Affected partition count
- Parquet rows written
- Processing duration
- Retry count
- Current checkpoint state
- Oldest unpublished generation

Alert when:

- A batch remains unpublished beyond the retry threshold
- Rejection rate exceeds a configured limit
- Conflicts occur unexpectedly
- A partition cannot be published
- Input checksums change for an existing batch identifier

---

## 8. Tests

### Schema tests

- Accept a fully valid record.
- Reject missing required fields.
- Reject wrong data types.
- Reject malformed timestamps.
- Reject non-UTC timestamps if UTC-only input is required.
- Reject unknown fields.
- Reject unsupported schema versions.
- Verify timestamp and decimal normalization.

### JSONL ingestion tests

- Process valid and invalid records in the same input.
- Preserve line numbers in rejection records.
- Handle blank lines according to policy.
- Handle malformed JSON without stopping the batch.
- Verify deterministic payload hashing.

### Deduplication tests

- Duplicate within one batch.
- Duplicate across two batches.
- Same `event_id` and same payload.
- Same `event_id` with different payload.
- Verify that the first committed canonical event remains unchanged.
- Verify uniqueness after retry.

### Partitioning tests

- Partition by UTC event date and hour.
- Place late-arriving events in their event-time partition.
- Rebuild a partition without duplicate rows.
- Verify exact Parquet schema, compression, row count, and column values.
- Verify stable output ordering.

### Recovery tests

Simulate interruption:

- Before database commit
- After database commit
- During partition generation
- After partition publication but before checkpoint update

For every case, retry the run and verify:

- Exactly one canonical row per `event_id`
- No lost valid events
- No duplicate Parquet rows
- All affected partitions eventually show matching desired and published generations

### Integration tests

Use a temporary test database and local object-storage emulator or equivalent test backend to verify:

1. Input discovery
2. Validation and rejection
3. Deduplication
4. DuckDB transaction behavior
5. Partition publication
6. Checkpoint recovery
7. Late-arrival rewriting
8. Repeated hourly execution

### Property-based tests

Generate arbitrary valid and invalid events and verify invariants:

```text
count(distinct event_id) = count(all canonical events)
all canonical events satisfy the schema
reprocessing any batch does not change canonical output
published output equals the canonical database projection
```

---

## 9. Core correctness guarantees

The design provides:

- Schema validation before acceptance
- Durable cross-hour deduplication
- Quarantine of malformed and conflicting events
- Event-time partitioned Parquet output
- Safe late-arrival handling
- Restartable, checkpointed processing
- Idempotent retries
- No dependency on Apache Spark
