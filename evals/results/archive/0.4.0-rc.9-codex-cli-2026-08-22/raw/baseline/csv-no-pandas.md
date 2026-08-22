Design a small Python CLI using only the standard library.

**Command**

```bash
python sales_summary.py input.csv summary.csv \
  --date-column date \
  --product-column product \
  --amount-column amount
```

Optional flags:

```text
--delimiter ","          CSV delimiter
--encoding utf-8         Input encoding
--month-format "%Y-%m"   Grouping format
--strict                 Fail on malformed rows
```

**Processing flow**

1. Open the input with `newline=""` and the selected encoding.
2. Parse rows with `csv.DictReader`.
3. Validate that the required columns exist.
4. For each row:
   - Parse the date with `datetime.date.fromisoformat()` or a documented accepted format.
   - Normalize it to `YYYY-MM`.
   - Parse the sales amount as `Decimal`, avoiding floating-point rounding errors.
   - Reject or report missing, invalid, or negative values according to `--strict`.
5. Aggregate by `(product, month)` using a dictionary:

```python
totals[(product, month)] += amount
```

6. Export sorted results to the output CSV.

**Output format**

```csv
product,month,total_sales
Widget A,2026-01,1250.50
Widget A,2026-02,980.00
Widget B,2026-01,430.25
```

Use a fixed two-decimal representation when writing totals.

**Suggested module structure**

```text
sales_summary/
  __init__.py
  cli.py          # argparse setup and exit codes
  reader.py       # CSV parsing and validation
  aggregator.py   # grouping and Decimal totals
  writer.py       # deterministic CSV export
tests/
  test_reader.py
  test_aggregator.py
  test_cli.py
```

For a single-file utility, keep the same logical separation as functions:

```python
read_sales_rows(...)
aggregate_sales(...)
write_summary(...)
main(...)
```

**Error handling**

- Exit `2` for invalid command-line arguments.
- Exit `1` for input/output or validation failures.
- Include row numbers in malformed-row errors.
- In non-strict mode, skip invalid rows and print a warning to stderr.
- Write to a temporary output file and replace the destination only after successful processing, preventing partial summaries.

**Testing**

Cover:

- Multiple products and months.
- Rows in arbitrary order.
- Duplicate product/month pairs.
- Decimal amounts such as `0.10 + 0.20`.
- Missing columns and blank values.
- Invalid dates and amounts.
- Quoted fields containing commas.
- Empty input files.
- Strict versus non-strict behavior.
- Stable output ordering and exact totals.

This approach uses `csv`, `argparse`, `datetime`, `decimal`, and `collections` only; pandas is unnecessary.