# Order totals per customer

`/app/data/` holds order exports from a small online shop. Write a program `/app/order_totals.py` that summarises one export file:

    python3 /app/order_totals.py INPUT.csv OUTPUT.json

**Input.** A CSV file with a header row and the columns `order_id,customer_id,status,amount`.

- `amount` is in US dollars and may use a comma as a thousands separator, for example `"1,234.50"` (such values are quoted).
- `customer_id` may have spaces around it; ignore them.
- `status` is `paid`, `refunded` or `cancelled`, in any letter case. Ignore `cancelled` rows. A `refunded` row counts as a negative amount.
- Ignore blank lines.

**Output.** A JSON object that maps each `customer_id` to that customer's total, as a number rounded to 2 decimal places. A customer whose rows are all cancelled must not appear. Write the keys in ascending order.

`/app/samples/input.csv` and `/app/samples/expected.json` are one input and its correct output. Your program must work for any file in this format, not only the sample.
