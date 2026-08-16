Build a small Python CLI using only the standard library.

**Command**

```bash
python sales_summary.py \
  --input sales.csv \
  --output monthly_summary.csv \
  --date-column date \
  --product-column product \
  --quantity-column quantity \
  --amount-column amount
```

**Input assumptions**

The CSV contains one sale per row, for example:

```csv
date,product,quantity,amount
2026-01-03,Widget A,2,19.98
2026-01-18,Widget B,1,12.50
2026-02-02,Widget A,3,29.97
```

Define whether `amount` means the row’s total sale value or unit price. Prefer treating it as the row total unless a separate `unit_price` column is provided.

**Processing flow**

1. Parse command-line arguments with `argparse`.
2. Open the input with `newline=""` and an explicit encoding such as UTF-8.
3. Read rows with `csv.DictReader`.
4. Validate required columns and reject malformed rows with clear line-numbered errors:
   - missing date, product, quantity, or amount
   - invalid date format
   - non-numeric or negative quantity
   - non-numeric amount
5. Convert each date to a month key such as `2026-01` using `datetime.strptime`.
6. Group records by `(product, month)`.
7. Accumulate:
   - `total_quantity`
   - `total_sales`
   - `transaction_count`
8. Sort output by month, then product.
9. Write the summary with `csv.DictWriter`.

A `defaultdict` is sufficient for grouping:

```python
groups[(product, month)]["total_quantity"] += quantity
groups[(product, month)]["total_sales"] += amount
groups[(product, month)]["transaction_count"] += 1
```

**Output format**

```csv
month,product,total_quantity,total_sales,transaction_count
2026-01,Widget A,2,19.98,1
2026-01,Widget B,1,12.50,1
2026-02,Widget A,3,29.97,1
```

Use `Decimal` for monetary values rather than `float`, and format totals to two decimal places when exporting.

**Recommended structure**

```text
sales_summary.py
tests/
  test_sales_summary.py
README.md
```

Keep the implementation separated into small functions:

- `parse_args()`
- `parse_row(row, line_number, column_config)`
- `month_key(date_text, date_format)`
- `aggregate_sales(rows)`
- `write_summary(path, summaries)`

**CLI options**

Include:

- `--input` and `--output`
- configurable column names
- `--date-format` defaulting to `%Y-%m-%d`
- `--delimiter` defaulting to `,`
- `--on-error {fail,skip}` to control malformed-row handling
- `--encoding` defaulting to `utf-8`

**Testing plan**

Cover:

- multiple products in the same month
- the same product across different months
- duplicate rows
- zero quantities and zero amounts
- malformed dates and numbers
- quoted product names containing commas
- alternate delimiters
- deterministic output ordering
- correct decimal rounding

This keeps the tool dependency-free, stream-friendly, and suitable for large CSV files without loading the entire input into memory.