#!/bin/bash
set -e
cat > /app/order_totals.py <<'PY'
import csv
import json
import sys
from decimal import ROUND_HALF_UP, Decimal


def main(src, dst):
    totals = {}
    with open(src, newline="") as f:
        rows = [line for line in f if line.strip()]
    for row in csv.DictReader(rows):
        status = row["status"].strip().lower()
        if status == "cancelled":
            continue
        amount = Decimal(row["amount"].replace(",", "").strip())
        if status == "refunded":
            amount = -amount
        customer = row["customer_id"].strip()
        totals[customer] = totals.get(customer, Decimal(0)) + amount
    out = {c: float(t.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for c, t in sorted(totals.items())}
    with open(dst, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
PY
python3 /app/order_totals.py /app/samples/input.csv /tmp/check.json
python3 -c "import json; assert json.load(open('/tmp/check.json')) == json.load(open('/app/samples/expected.json'))"
