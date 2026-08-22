Design the pipeline as a small, restartable Python service with three durable layers:

- **Input:** hourly JSONL objects/files
- **State:** SQLite checkpoint and deduplication database
- **Output:** partitioned Parquet written with PyArrow

Use `pydantic` for schema validation, `pyarrow` for Parquet, and a scheduler such as cron, Airflow, or a container job.

## Layout

```text
landing/events/
  date=2026-08-22/hour=13/*.jsonl

curated/events/
  event_date=2026-08-22/event_hour=13/part-<run-id>.parquet

quarantine/events/
  date=2026-08-22/hour=13/invalid-<run-id>.jsonl

state/etl.sqlite
```

## Event contract

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    event_time: datetime
    source: str = Field(min_length=1)
    payload: dict
```

Validation rules should include:

- Required fields and types
- UTC timestamps
- Non-empty `event_id`
- Allowed event types, if applicable
- Maximum payload size
- Reject unknown fields unless explicitly supported

Invalid records go to quarantine with the original line, source file, line number, and validation error.

## Hourly workflow

1. Determine the target hour, for example `2026-08-22T13:00Z`.
2. Discover all input files for that hour.
3. Create or load a checkpoint row.
4. Read JSONL incrementally, tracking file and line offsets.
5. Parse and validate each record.
6. Deduplicate by `event_id`.
7. Add partition columns derived from `event_time`.
8. Write valid records to a temporary Parquet file.
9. Atomically rename the temporary file into the target partition.
10. Commit the checkpoint and deduplication records in one transaction.
11. Emit metrics and mark the hour complete.

A simplified processing loop:

```python
def process_hour(hour, files, db, output_root):
    run_id = uuid.uuid4().hex
    valid = []
    invalid = []

    for path in files:
        for line_no, raw in enumerate(path.open(), start=1):
            try:
                event = Event.model_validate_json(raw)
            except Exception as exc:
                invalid.append({
                    "source_file": str(path),
                    "line_number": line_no,
                    "raw": raw.rstrip("\n"),
                    "error": str(exc),
                })
                continue

            inserted = db.execute(
                "INSERT OR IGNORE INTO seen_events(event_id, first_seen_hour) "
                "VALUES (?, ?)",
                (event.event_id, hour.isoformat()),
            ).rowcount

            if inserted:
                valid.append({
                    **event.model_dump(),
                    "event_date": event.event_time.date().isoformat(),
                    "event_hour": event.event_time.hour,
                })

    write_quarantine(invalid, hour, run_id)

    if not valid:
        db.mark_complete(hour, row_count=0)
        return

    table = pyarrow.Table.from_pylist(valid)
    partition = output_root / (
        f"event_date={hour.date()}/event_hour={hour.hour:02d}"
    )
    temp = partition / f".part-{run_id}.parquet.tmp"
    final = partition / f"part-{run_id}.parquet"

    partition.mkdir(parents=True, exist_ok=True)
    pyarrow.parquet.write_table(table, temp, compression="zstd")
    fsync_file(temp)
    atomic_rename(temp, final)

    db.mark_complete(
        hour,
        row_count=len(valid),
        output_file=str(final),
        output_checksum=sha256(final),
    )
```

For production, insert deduplication keys into a staging table first, then move them to `seen_events` only when the output commit succeeds. This prevents a crash from losing records that were marked seen but never written.

## Checkpoint schema

```sql
CREATE TABLE checkpoints (
  hour TEXT PRIMARY KEY,
  status TEXT NOT NULL,               -- STARTED, WRITTEN, COMMITTED, FAILED
  input_manifest TEXT NOT NULL,       -- JSON list of files and sizes
  output_file TEXT,
  output_checksum TEXT,
  row_count INTEGER DEFAULT 0,
  invalid_count INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL,
  error TEXT
);

CREATE TABLE seen_events (
  event_id TEXT PRIMARY KEY,
  first_seen_hour TEXT NOT NULL
);
```

Store the input manifest, including file path, size, modification time, and checksum. If the same hour is rerun with an identical manifest and status `COMMITTED`, return immediately.

## Recovery behavior

- `STARTED`: safely restart processing.
- `WRITTEN`: verify the output checksum; finalize the checkpoint if valid, otherwise remove the incomplete output and rerun.
- `COMMITTED`: no-op.
- Missing or changed input manifest: fail loudly and require an explicit reprocess decision.
- Write output to a temporary filename and rename atomically so readers never observe partial Parquet files.
- Keep failed inputs and validation errors for replay.

For late-arriving events, process a configurable lookback window, such as the previous 24 hours, and write a new Parquet part into the original event-time partition. Periodic compaction can merge small files.

## Tests

Unit tests:

- Valid event parses successfully.
- Missing, malformed, extra, or oversized fields are quarantined.
- Duplicate `event_id` values produce one output record.
- Partition columns are derived correctly from UTC timestamps.
- Empty hourly input completes successfully.
- Checksum and manifest values are recorded.

Integration tests:

- End-to-end JSONL to Parquet conversion.
- Rerunning a committed hour is idempotent.
- Simulated crash after Parquet rename resumes without duplication.
- Simulated crash before rename leaves no committed checkpoint.
- Late events land in the correct historical partition.
- Corrupt input files are reported without losing valid files.

Property-based tests can generate arbitrary JSON records to verify that validation never crashes the worker and that deduplication remains deterministic.

Operational metrics should include input files, lines read, valid rows, invalid rows, duplicates dropped, output bytes, processing duration, and checkpoint status.