"""Small, re-runnable end-to-end demo of the accounts-payable settlement ledger.

One tiny working directory, two complete handover projects and three fixed
order lines.  Every interesting rule gets an in-process assertion, so the
script itself is a check:

1. initialization scopes on the handover list's ``complete`` projects plus the
   fixed order-line -> project table (no qualification is re-judged);
2. invoices consume preceding receipts FIFO, split across receipts and
   partially settle; the unbacked tail books nothing and is never rescued by a
   receipt arriving later;
3. floor-cumulative money: the first ``k`` units book ``floor(k*C/Q)`` cents;
4. a red voucher first cancels the unwritten tail (no entry), then claws the
   settled tail back with the exact cents originally booked, returning the
   quantity to the originating receipt for a later invoice;
5. an invoice re-pointed at another order line withdraws its stale entries and
   drags its red voucher to the new line;
6. malformed / out-of-scope / dangling / over-limit batches are rejected as a
   whole, leaving the stored vouchers and entries untouched.

Run it with:  python3 -I examples/ap_ledger_demo.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import ap_ledger  # noqa: E402


def write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def receipt(vid, btime, line, qty):
    return {"voucher": vid, "type": "receipt", "business_time": btime,
            "order_line": line, "quantity": qty}


def invoice(vid, btime, lines):
    return {"voucher": vid, "type": "invoice", "business_time": btime,
            "lines": [{"line": no, "order_line": line, "quantity": q,
                       "amount_cents": c} for no, line, q, c in lines]}


def red(vid, btime, original_invoice, original_line, qty):
    return {"voucher": vid, "type": "red", "business_time": btime,
            "original_invoice": original_invoice,
            "original_line": original_line, "quantity": qty}


def signatures(rows):
    return [(r["voucher"], r["original_line"], r["receipt"],
             r["quantity"], r["amount_cents"]) for r in rows]


def main():
    tmp = tempfile.mkdtemp(prefix="ap-ledger-demo-")
    work = os.path.join(tmp, "work")
    print("working directory:", work)

    # -- 1. fixed scope from the existing handover list ---------------------
    handover = os.path.join(tmp, "handover.json")
    mapping = os.path.join(tmp, "order_lines.json")
    write(handover, {"projects": [
        {"project": "P1", "status": "complete", "items": []},
        {"project": "P2", "status": "complete", "items": []},
        {"project": "P9", "status": "incomplete",
         "missing_items": ["permit"]},          # never enters the ledger scope
    ]})
    write(mapping, {"order_lines": [
        {"order_line": "OL-100", "project": "P1"},
        {"order_line": "OL-200", "project": "P2"},
        {"order_line": "OL-300", "project": "P1"},
    ]})
    init = ap_ledger.init_workdir(work, handover, mapping)
    assert init["complete_projects"] == ["P1", "P2"]
    assert set(init["order_lines"]) == {"OL-100", "OL-200", "OL-300"}
    print("initialized order lines:", init["order_lines"])

    def apply(operations):
        return ap_ledger.apply_batch(work, {"operations": operations})

    def entries(line):
        return ap_ledger.export_line(work, line)["entries"]

    # -- 2. FIFO split, partial settlement, no retroactive rescue ------------
    apply([
        {"op": "add", "voucher": receipt("GR1", 10, "OL-100", 4)},
        {"op": "add", "voucher": receipt("GR2", 11, "OL-100", 3)},
        # 10 units for 1000 cents; only 7 have receipt backing.
        {"op": "add", "voucher": invoice("INV1", 20, [
            ("1", "OL-100", 10, 1000)])},
    ])
    assert signatures(entries("OL-100")) == [
        ("INV1", "1", "GR1", 4, 400),
        ("INV1", "1", "GR2", 3, 300),
    ]
    # A later receipt never auto-settles INV1's unbooked 3-unit tail.
    apply([{"op": "add", "voucher": receipt("GR3", 30, "OL-100", 5)}])
    assert len(entries("OL-100")) == 2
    print("FIFO + partial settlement:")
    print(json.dumps(ap_ledger.export_line(work, "OL-100"),
                     indent=2, ensure_ascii=False))

    # -- 3. floor-cumulative money across a segment split -------------------
    apply([
        {"op": "add", "voucher": receipt("GR4", 12, "OL-200", 2)},
        {"op": "add", "voucher": receipt("GR5", 13, "OL-200", 1)},
        # Q=3, C=10: cumulative floor(k*10/3) = 0, 3, 6, 10.
        {"op": "add", "voucher": invoice("INV2", 21, [
            ("7", "OL-200", 3, 10)])},
    ])
    assert signatures(entries("OL-200")) == [
        ("INV2", "7", "GR4", 2, 6),
        ("INV2", "7", "GR5", 1, 4),
    ]
    print("floor-cumulative split:", signatures(entries("OL-200")))

    # -- 4. red voucher: free tail first, then exact-cents clawback ---------
    # On OL-300: 4 units received, invoice of 6 (so 2 units are unwritten).
    apply([
        {"op": "add", "voucher": receipt("GR6", 10, "OL-300", 4)},
        {"op": "add", "voucher": invoice("INV3", 20, [
            ("3", "OL-300", 6, 600)])},
        # Reverse 4: the 2 unwritten units disappear silently, then 2 settled
        # units leave from the tail, giving back exactly their original cents.
        {"op": "add", "voucher": red("RD1", 30, "INV3", "3", 4)},
    ])
    assert signatures(entries("OL-300")) == [
        ("INV3", "3", "GR6", 4, 400),
        ("RD1", "3", "GR6", -2, -200),
    ]
    # The returned 2 units are now usable by the next invoice, FIFO first.
    apply([
        {"op": "add", "voucher": receipt("GR7", 40, "OL-300", 2)},
        {"op": "add", "voucher": invoice("INV4", 50, [
            ("1", "OL-300", 3, 300)])},
    ])
    # GR6's returned balance (2) precedes GR7; GR7 supplies the last unit.
    assert signatures(entries("OL-300"))[-2:] == [
        ("INV4", "1", "GR6", 2, 200),
        ("INV4", "1", "GR7", 1, 100),
    ]
    print("red clawback + returned balance:")
    print(json.dumps(ap_ledger.export_line(work, "OL-300"),
                     indent=2, ensure_ascii=False))

    # -- 5. correction re-pointing an invoice moves its red voucher too ------
    apply([{"op": "add", "voucher": receipt("GR8", 10, "OL-200", 2)}])
    apply([{"op": "replace", "voucher": invoice("INV3", 20, [
        ("3", "OL-200", 6, 600)])}])
    # OL-300 loses INV3/RD1 entirely (its receipts remain); OL-200 gains them.
    assert all(r["voucher"] == "INV4" for r in entries("OL-300"))
    assert {r["voucher"] for r in entries("OL-200")} >= {"INV3", "RD1"}
    state = ap_ledger.load_state(work)
    assert "RD1" in state["line_vouchers"]["OL-200"]
    assert "RD1" not in state["line_vouchers"]["OL-300"]
    print("re-pointed invoice entries on OL-200:",
          signatures(entries("OL-200")))

    # -- 6. rejected batches leave the ledger exactly as it was --------------
    snapshot_path = ap_ledger.state_path(work)
    with open(snapshot_path, "rb") as fh:
        snapshot = fh.read()
    rejected = [
        # unknown order line
        {"operations": [{"op": "add", "voucher":
                         receipt("BAD", 1, "OL-X", 1)}]},
        # dangling original invoice reference
        {"operations": [{"op": "add", "voucher":
                         red("BAD", 9, "GHOST", "1", 1)}]},
        # red before the original invoice
        {"operations": [{"op": "add", "voucher":
                         red("BAD", 5, "INV1", "1", 1)}]},
        # cumulative red quantity above the original line quantity
        {"operations": [{"op": "add", "voucher":
                         red("BAD", 99, "INV1", "1", 99)}]},
        # malformed shape
        {"operations": [{"op": "add", "voucher":
                         {"voucher": "BAD", "type": "receipt",
                          "business_time": 1.0, "order_line": "OL-100",
                          "quantity": 1}}]},
        # same voucher id operated on twice in one batch
        {"operations": [
            {"op": "delete", "voucher": "GR1"},
            {"op": "delete", "voucher": "GR1"}]},
    ]
    for doc in rejected:
        try:
            ap_ledger.apply_batch(work, doc)
        except ap_ledger.LedgerError as exc:
            print("rejected as expected:", exc)
        else:  # pragma: no cover
            raise AssertionError(f"batch should have been rejected: {doc}")
        with open(snapshot_path, "rb") as fh:
            assert fh.read() == snapshot, "rejected batch mutated the ledger"

    status = ap_ledger.ledger_status(work)
    print("final status:", json.dumps(status, indent=2, ensure_ascii=False))
    all_entries = ap_ledger.export_all(work)
    print("total exported entries:", len(all_entries["entries"]))
    print("\nall demo assertions passed")


if __name__ == "__main__":
    main()
