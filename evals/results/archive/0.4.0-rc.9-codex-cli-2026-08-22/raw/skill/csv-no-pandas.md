Plan for a standard-library Python CLI:

1. **Command interface**

   ```text
   sales-summary INPUT.csv -o SUMMARY.csv
   ```

   Optional flags:

   - `--date-column` — transaction date field, default `date`
   - `--product-column` — product field, default `product`
   - `--amount-column` — sales amount field, default `amount`
   - `--delimiter` — input delimiter, default `,`

2. **Input format**

   Require a header row with fields such as:

   ```csv
   date,product,amount
   2026-01-15,Widget A,19.99
   2026-01-22,Widget A,5.00
   2026-02-03,Widget B,12.50
   ```

   Parse dates with `datetime`, amounts with `decimal.Decimal`, and read rows with Python’s `csv.DictReader`.

3. **Aggregation**

   For each row:

   - Parse the date and derive a month key in `YYYY-MM` format.
   - Read the product name.
   - Convert the amount to `Decimal`.
   - Accumulate by `(product, month)` using a dictionary or `defaultdict`.
   - Track both total sales amount and row count.

   Conceptually:

   ```text
   totals[(product, month)].amount += amount
   totals[(product, month)].count += 1
   ```

4. **Output format**

   Write a CSV summary with:

   ```csv
   product,month,total_sales,transaction_count
   Widget A,2026-01,24.99,2
   Widget B,2026-02,12.50,1
   ```

   Sort output by product, then month. Format monetary values consistently to two decimal places.

5. **Error handling**

   Report actionable errors and exit nonzero for:

   - Missing input file
   - Missing required columns
   - Invalid dates
   - Non-numeric or blank amounts
   - Unwritable output path

   Include the input row number in row-level error messages.

6. **Implementation structure**

   Keep responsibilities separate:

   - `parse_args()` — CLI options
   - `read_sales(path, columns, delimiter)` — row parsing and validation
   - `aggregate(rows)` — grouping and totals
   - `write_summary(path, results)` — sorted CSV export
   - `main()` — orchestration and exit status

7. **Testing**

   Cover:

   - Multiple products across several months
   - Repeated product/month rows
   - Decimal precision
   - Empty input containing only headers
   - Invalid dates and amounts
   - Custom column names and delimiters
   - Stable sorting and exact output formatting

This keeps the tool dependency-free, deterministic, and suitable for shell scripts or scheduled reporting jobs.