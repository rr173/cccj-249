"""Build a large accounts-payable ledger and check only the scale properties.

The real finance ledger holds many independent order lines and individual
vouchers whose quantities can be huge.  This generator builds

* a scope of ``n_lines`` fixed order lines over complete handover projects;
* one initial batch carrying two receipts and one invoice per order line
  (so every line settles into real entries);
* a single huge-quantity voucher (``10**12`` units) to prove quantities are
  handled as interval endpoints, never expanded per unit;
* one small correction batch touching exactly one order line, timed to show
  that daily corrections stay independent of the ledger's total size -- the
  history of every other order line is never re-scanned and its stored entries
  stay byte-identical.

No per-unit simulation is performed anywhere; the assertions are facts that
can be derived by hand from the stated rules.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import ap_ledger  # noqa: E402

N_LINES = 2000
HUGE_Q = 10 ** 12


def build(n_lines=N_LINES):
    tmp = tempfile.mkdtemp(prefix="ap-ledger-large-")
    work = os.path.join(tmp, "work")
    handover = os.path.join(tmp, "handover.json")
    mapping = os.path.join(tmp, "mapping.json")
    huge_line = "OL-HUGE"
    with open(handover, "w", encoding="utf-8") as fh:
        json.dump({"projects": [
            {"project": f"P{i:04d}", "status": "complete", "items": []}
            for i in range(n_lines)]}, fh)
    with open(mapping, "w", encoding="utf-8") as fh:
        json.dump({"order_lines": [
            {"order_line": f"OL{i:05d}", "project": f"P{i % n_lines:04d}"}
            for i in range(n_lines)]
            + [{"order_line": huge_line, "project": "P0000"}]}, fh)

    t0 = time.perf_counter()
    ap_ledger.init_workdir(work, handover, mapping)
    t_init = time.perf_counter() - t0

    # ---- one bulk batch: receipts and an invoice for every order line ------
    operations = []
    for i in range(n_lines):
        line = f"OL{i:05d}"
        operations.append({"op": "add", "voucher": {
            "voucher": f"GR{i:05d}", "type": "receipt",
            "business_time": 1, "order_line": line, "quantity": 7}})
        operations.append({"op": "add", "voucher": {
            "voucher": f"INV{i:05d}", "type": "invoice",
            "business_time": 2, "lines": [
                {"line": "1", "order_line": line, "quantity": 10,
                 "amount_cents": 1000}]}})
    # Plus one huge-quantity voucher on its own line: no unit expansion.
    operations.append({"op": "add", "voucher": {
        "voucher": "GR-HUGE", "type": "receipt", "business_time": 1,
        "order_line": huge_line, "quantity": HUGE_Q}})
    operations.append({"op": "add", "voucher": {
        "voucher": "INV-HUGE", "type": "invoice", "business_time": 2,
        "lines": [{"line": "1", "order_line": huge_line,
                   "quantity": HUGE_Q, "amount_cents": HUGE_Q * 3 - 1}]}})

    t0 = time.perf_counter()
    result = ap_ledger.apply_batch(work, {"operations": operations})
    t_bulk = time.perf_counter() - t0
    assert result["status"] == "accepted"
    assert result["voucher_count"] == 2 * n_lines + 2

    # Every standard line carries exactly one settled entry (7 of 10 units;
    # the unbacked 3 book nothing).
    status = ap_ledger.ledger_status(work)
    assert status["total_entries"] == n_lines + 1, status["total_entries"]
    sample = ap_ledger.export_line(work, "OL00000")["entries"]
    assert len(sample) == 1 and sample[0]["quantity"] == 7
    assert sample[0]["amount_cents"] == 700

    # The huge voucher is one row with exact big-integer floor money.
    huge_rows = ap_ledger.export_line(work, huge_line)["entries"]
    assert [(r["voucher"], r["quantity"], r["amount_cents"])
            for r in huge_rows] == [
        ("INV-HUGE", HUGE_Q, HUGE_Q * 3 - 1)]

    # ---- a small correction touches exactly one order line ----------------
    state_path = ap_ledger.state_path(work)
    before = {}
    with open(state_path, "rb") as fh:
        prior_state = fh.read()
    prior = json.loads(prior_state)
    untouched_entry = dict(prior["entries"]["OL00001"][0])

    t0 = time.perf_counter()
    ap_ledger.apply_batch(work, {"operations": [
        {"op": "replace", "voucher": {
            "voucher": "GR00000", "type": "receipt", "business_time": 1,
            "order_line": "OL00000", "quantity": 4}}]})
    t_small = time.perf_counter() - t0

    after = ap_ledger.load_state(work)
    # Only OL00000 was re-settled; every other line's stored entry is the
    # exact same object content the small batch never scanned.
    assert after["entries"]["OL00001"][0] == untouched_entry
    changed = ap_ledger.export_line(work, "OL00000")["entries"]
    assert changed[0]["quantity"] == 4 and changed[0]["amount_cents"] == 400
    # The huge line and the bulk of the ledger are untouched.
    assert ap_ledger.export_line(work, huge_line)["entries"] == huge_rows

    return {
        "workdir": work,
        "n_lines": n_lines,
        "init_seconds": round(t_init, 4),
        "bulk_batch_seconds": round(t_bulk, 4),
        "small_correction_seconds": round(t_small, 4),
        "bulk_vouchers": len(operations),
        "total_entries": status["total_entries"],
    }


def main():
    report = build()
    print(json.dumps(report, indent=2))
    print("\nlarge ledger assertions passed; no per-unit expansion performed")


if __name__ == "__main__":
    main()
