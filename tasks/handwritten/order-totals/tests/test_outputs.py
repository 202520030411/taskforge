"""Hidden tests. The program is always run in a subprocess, on inputs the agent has not seen."""
import csv
import io
import json
import random
import subprocess
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PROGRAM = "/app/order_totals.py"


def run(csv_text):
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.csv", Path(tmp) / "out.json"
        src.write_text(csv_text)
        proc = subprocess.run(["python3", PROGRAM, str(src), str(dst)], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert dst.exists(), "no output file was written"
        return json.loads(dst.read_text(), object_pairs_hook=list)


def check(pairs, expected):
    keys = [k for k, _ in pairs]
    assert keys == sorted(keys), f"keys not in ascending order: {keys}"
    got = dict(pairs)
    assert set(got) == set(expected), f"customers {sorted(got)} != {sorted(expected)}"
    for k, v in expected.items():
        assert isinstance(got[k], (int, float)) and not isinstance(got[k], bool), f"{k}: not a number"
        assert abs(got[k] - v) < 1e-9, f"{k}: {got[k]} != {v}"


def reference(csv_text):
    totals = {}
    rows = [line for line in csv_text.splitlines() if line.strip()]
    for row in csv.DictReader(io.StringIO("\n".join(rows))):
        status = row["status"].strip().lower()
        if status == "cancelled":
            continue
        amount = Decimal(row["amount"].replace(",", "").strip())
        customer = row["customer_id"].strip()
        totals[customer] = totals.get(customer, Decimal(0)) + (-amount if status == "refunded" else amount)
    return {k: float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for k, v in totals.items()}


def test_sample():
    check(run(Path("/app/samples/input.csv").read_text()), json.loads(Path("/app/samples/expected.json").read_text()))


def test_stated_edge_cases():
    text = (
        "order_id,customer_id,status,amount\n"
        "2001, C10 ,PAID,\"2,500.25\"\n"
        "2002,C11,Cancelled,10.00\n"
        "\n"
        "2003,C10,refunded,500.25\n"
        "2004,C12,paid,3.333\n"
        "2005,C12,paid,3.333\n"
        "2006,C13,CANCELLED,99.00\n"
        "2007,C14,paid,0.10\n"
        "2008,C14,paid,0.20\n"
    )
    check(run(text), {"C10": 2000.0, "C12": 6.67, "C14": 0.3})


def test_generated_file():
    rng = random.Random(20261116)
    lines = ["order_id,customer_id,status,amount"]
    for i in range(300):
        customer = f"{' ' * rng.randint(0, 2)}K{rng.randint(1, 40):03d}{' ' * rng.randint(0, 2)}"
        status = rng.choice(["paid", "Paid", "PAID", "refunded", "Refunded", "cancelled", "Cancelled"])
        amount = rng.uniform(0.01, 9000)
        text = f"\"{amount:,.2f}\"" if amount >= 1000 else f"{amount:.2f}"
        lines.append(f"{9000 + i},{customer},{status},{text}")
        if rng.random() < 0.05:
            lines.append("")
    text = "\n".join(lines) + "\n"
    check(run(text), reference(text))
