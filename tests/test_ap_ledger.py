"""Tests for the local accounts-payable settlement ledger (solver/ap_ledger.py).

Every rule in the brief gets a small hand-built witness, and a naive
unit-expanded reference implementation (quantities expanded one unit at a
time, which the real engine must never do) fuzzes the whole settlement
machinery across random correction batches.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import ap_ledger  # noqa: E402

SOLVER = os.path.join(HERE, "..", "solver", "ap_ledger.py")


# ---------------------------------------------------------------------------
# builders and fixtures
# ---------------------------------------------------------------------------
def receipt(vid, btime, line, qty):
    return {"voucher": vid, "type": "receipt", "business_time": btime,
            "order_line": line, "quantity": qty}


def invoice(vid, btime, lines):
    return {"voucher": vid, "type": "invoice", "business_time": btime,
            "lines": [
                {"line": no, "order_line": line, "quantity": q,
                 "amount_cents": c}
                for no, line, q, c in lines]}


def red(vid, btime, original_invoice, original_line, qty):
    return {"voucher": vid, "type": "red", "business_time": btime,
            "original_invoice": original_invoice,
            "original_line": original_line, "quantity": qty}


class LedgerFixture:
    """A fresh temp ledger initialized from a tiny fixed scope."""

    def __init__(self, mapping=None):
        self.tmp = tempfile.mkdtemp()
        self.workdir = os.path.join(self.tmp, "work")
        if mapping is None:
            mapping = [
                {"order_line": "L-A", "project": "P1"},
                {"order_line": "L-B", "project": "P2"},
            ]
        handover = {"projects": [
            {"project": "P1", "status": "complete", "items": []},
            {"project": "P2", "status": "complete", "items": []},
            {"project": "P9", "status": "incomplete",
             "missing_items": ["x"]},
        ]}
        self.handover = self._write("handover.json", handover)
        self.mapping_file = self._write("order_lines.json",
                                        {"order_lines": mapping})
        ap_ledger.init_workdir(self.workdir, self.handover, self.mapping_file)

    def _write(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def apply(self, operations):
        return ap_ledger.apply_batch(self.workdir, {"operations": operations})

    def add(self, *vouchers):
        return self.apply([{"op": "add", "voucher": v} for v in vouchers])

    def entries(self, line="L-A"):
        return ap_ledger.export_line(self.workdir, line)["entries"]

    def state(self):
        return ap_ledger.load_state(self.workdir)


def op(action, voucher=None, vid=None):
    if action == "delete":
        return {"op": "delete", "voucher": vid}
    return {"op": action, "voucher": voucher}


def entry_signature(rows):
    return [(r["voucher"], r["original_invoice"], r["original_line"],
             r["receipt"], r["quantity"], r["amount_cents"]) for r in rows]


# ---------------------------------------------------------------------------
# initialization and scope
# ---------------------------------------------------------------------------
class TestInit(unittest.TestCase):
    def test_scope_is_complete_projects_plus_fixed_lines(self):
        fx = LedgerFixture()
        state = fx.state()
        self.assertEqual(state["order_line_project"],
                         {"L-A": "P1", "L-B": "P2"})
        self.assertEqual(state["vouchers"], {})
        self.assertEqual(sorted(state["line_vouchers"]), ["L-A", "L-B"])
        self.assertEqual(state["entries"], {})

    def test_mapping_to_non_complete_project_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.json")
            m = os.path.join(tmp, "m.json")
            with open(h, "w") as fh:
                json.dump({"projects": [
                    {"project": "P1", "status": "complete"},
                    {"project": "P9", "status": "incomplete"}]}, fh)
            with open(m, "w") as fh:
                json.dump({"order_lines": [
                    {"order_line": "L-A", "project": "P9"}]}, fh)
            with self.assertRaises(ap_ledger.LedgerError):
                ap_ledger.init_workdir(os.path.join(tmp, "w"), h, m)

    def test_duplicate_order_line_and_reinit_rejected(self):
        fx = LedgerFixture()
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.json")
            m = os.path.join(tmp, "m.json")
            with open(h, "w") as fh:
                json.dump({"projects": [
                    {"project": "P1", "status": "complete"}]}, fh)
            with open(m, "w") as fh:
                json.dump({"order_lines": [
                    {"order_line": "L-A", "project": "P1"},
                    {"order_line": "L-A", "project": "P1"}]}, fh)
            with self.assertRaises(ap_ledger.LedgerError):
                ap_ledger.init_workdir(os.path.join(tmp, "w"), h, m)
        with self.assertRaises(ap_ledger.LedgerError):
            ap_ledger.init_workdir(fx.workdir, fx.handover, fx.mapping_file)

    def test_init_needs_at_least_one_complete_project_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = os.path.join(tmp, "h.json")
            m = os.path.join(tmp, "m.json")
            with open(h, "w") as fh:
                json.dump({"projects": [
                    {"project": "P9", "status": "incomplete"}]}, fh)
            with open(m, "w") as fh:
                json.dump({"order_lines": []}, fh)
            with self.assertRaises(ap_ledger.LedgerError):
                ap_ledger.init_workdir(os.path.join(tmp, "w"), h, m)


# ---------------------------------------------------------------------------
# positive settlement: FIFO, splitting, partial settlement
# ---------------------------------------------------------------------------
class TestSettlement(unittest.TestCase):
    def test_basic_fifo_split_and_partial_settlement(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5),
               receipt("GR2", 2, "L-A", 3),
               invoice("INV1", 3, [("1", "L-A", 10, 1000)]))
        # 5 from GR1 + 3 from GR2 = 8 settled; 2 units book nothing.
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 5, 500),
            ("INV1", "INV1", "1", "GR2", 3, 300),
        ])
        # Later receipts never auto-settle the unbooked tail.
        fx.add(receipt("GR3", 9, "L-A", 4))
        self.assertEqual(len(fx.entries()), 2)

    def test_later_invoice_consumes_later_receipt_first_in_first_out(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]),
               receipt("GR2", 3, "L-A", 2),
               invoice("INV2", 4, [("1", "L-A", 2, 400)]))
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 2, 200),
            ("INV2", "INV2", "1", "GR2", 2, 400),
        ])

    def test_one_invoice_splits_across_receipts_with_floor_amounts(self):
        fx = LedgerFixture()
        # 10 units, 100 cents: floor(k*100/10) = 10 per unit here; use C=103.
        fx.add(receipt("GR1", 1, "L-A", 3),
               receipt("GR2", 2, "L-A", 4),
               invoice("INV1", 3, [("1", "L-A", 10, 103)]))
        rows = fx.entries()
        # units [0,3): floor(3*103/10)=30 ; units [3,7): floor(7*103/10)-30
        # = 72 - 30 = 42
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 3, 30),
            ("INV1", "INV1", "1", "GR2", 4, 42),
        ])
        self.assertEqual(sum(r["amount_cents"] for r in rows), 72)

    def test_zero_amount_rows_with_quantity_are_kept(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 4),
               invoice("INV1", 2, [("1", "L-A", 4, 0)]))
        rows = fx.entries()
        self.assertEqual(entry_signature(rows),
                         [("INV1", "INV1", "1", "GR1", 4, 0)])

    def test_no_receipt_no_entries(self):
        fx = LedgerFixture()
        fx.add(invoice("INV1", 1, [("1", "L-A", 3, 300)]))
        self.assertEqual(fx.entries(), [])
        # A receipt arriving afterwards still does not settle the old invoice.
        fx.add(receipt("GR1", 2, "L-A", 3))
        self.assertEqual(fx.entries(), [])

    def test_event_order_is_business_time_then_voucher_id(self):
        fx = LedgerFixture()
        # Same business time: voucher id order decides.  "R-Z" sorts after
        # "INV-A", so the receipt is not "before" the invoice and settles nothing.
        fx.add(receipt("R-Z", 5, "L-A", 2),
               invoice("INV-A", 5, [("1", "L-A", 2, 200)]))
        self.assertEqual(fx.entries(), [])
        # A receipt whose id sorts first at the same time does settle.
        fx2 = LedgerFixture()
        fx2.add(receipt("GR-A", 5, "L-A", 2),
                invoice("INV-A", 5, [("1", "L-A", 2, 200)]))
        self.assertEqual(len(fx2.entries()), 1)

    def test_multi_line_invoice_lines_process_in_line_number_order(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5))
        fx.add(invoice("INV1", 2, [
            ("b", "L-A", 3, 300), ("a", "L-A", 2, 200)]))
        rows = fx.entries()
        # Settlement order is line "a" then "b"; both fit in GR1 anyway.
        self.assertEqual([r["original_line"] for r in rows], ["a", "b"])

    def test_multi_line_invoice_spans_order_lines_independently(self):
        fx = LedgerFixture()
        fx.add(receipt("GR-A", 1, "L-A", 2),
               receipt("GR-B", 1, "L-B", 2),
               invoice("INV1", 2, [
                   ("1", "L-A", 5, 500), ("2", "L-B", 1, 100)]))
        self.assertEqual(entry_signature(fx.entries("L-A")),
                         [("INV1", "INV1", "1", "GR-A", 2, 200)])
        self.assertEqual(entry_signature(fx.entries("L-B")),
                         [("INV1", "INV1", "2", "GR-B", 1, 100)])


# ---------------------------------------------------------------------------
# red vouchers: tail reversal, clawback, returned balance, exact cents
# ---------------------------------------------------------------------------
class TestRedVouchers(unittest.TestCase):
    def test_red_first_cancels_unsettled_tail_without_entries(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5),
               invoice("INV1", 2, [("1", "L-A", 10, 1000)]),
               red("RD1", 3, "INV1", "1", 3))
        # 8 live units, 5 settled; reversing 3 hits only the unwritten tail.
        self.assertEqual(entry_signature(fx.entries()),
                         [("INV1", "INV1", "1", "GR1", 5, 500)])

    def test_red_claws_back_settled_segment_with_exact_cents(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5),
               receipt("GR2", 2, "L-A", 5),
               invoice("INV1", 3, [("1", "L-A", 10, 1000)]),
               red("RD1", 4, "INV1", "1", 3))
        # Fully settled 10 units (5+5). Tail 3 all came from GR2's segment
        # (units 7-9); one red voucher produces one row per touched receipt.
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 5, 500),
            ("INV1", "INV1", "1", "GR2", 5, 500),
            ("RD1", "INV1", "1", "GR2", -3, -300),
        ])

    def test_red_spanning_two_receipts_emits_two_negative_rows(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5),
               receipt("GR2", 2, "L-A", 5),
               invoice("INV1", 3, [("1", "L-A", 10, 1000)]),
               red("RD1", 4, "INV1", "1", 6))
        # Tail 6: GR2's whole segment (5) plus GR1's last unit.
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 5, 500),
            ("INV1", "INV1", "1", "GR2", 5, 500),
            ("RD1", "INV1", "1", "GR1", -1, -100),
            ("RD1", "INV1", "1", "GR2", -5, -500),
        ])

    def test_red_mixed_unwritten_and_settled(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 5),
               invoice("INV1", 2, [("1", "L-A", 10, 1000)]),
               red("RD1", 3, "INV1", "1", 7))
        # 5 settled, 5 unwritten; 5 free reversals book nothing, then 2
        # settled units (the GR1 segment's tail) are clawed back.
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 5, 500),
            ("RD1", "INV1", "1", "GR1", -2, -200),
        ])

    def test_clawed_back_balance_returns_to_originating_receipt_fifo(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 3),
               receipt("GR2", 2, "L-A", 3),
               invoice("INV1", 3, [("1", "L-A", 6, 600)]),
               red("RD1", 4, "INV1", "1", 4),
               invoice("INV2", 6, [("1", "L-A", 3, 300)]))
        # INV1: GR1 units 0-2, GR2 units 3-5. Red 4: GR2's whole segment
        # (units 3-5) plus unit 2 from GR1.  Returned balances: GR2=3, GR1=1.
        # INV2 FIFO: GR1 gets its 1 unit first, then GR2 gets 2.
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 3, 300),
            ("INV1", "INV1", "1", "GR2", 3, 300),
            ("RD1", "INV1", "1", "GR1", -1, -100),
            ("RD1", "INV1", "1", "GR2", -3, -300),
            ("INV2", "INV2", "1", "GR1", 1, 100),
            ("INV2", "INV2", "1", "GR2", 2, 200),
        ])

    def test_clawback_uses_original_segment_cents_not_re_amortization(self):
        fx = LedgerFixture()
        # Q=3, C=10: cumulatives floor(k*10/3) = 0, 3, 6, 10.
        fx.add(receipt("GR1", 1, "L-A", 2),
               receipt("GR2", 2, "L-A", 1),
               invoice("INV1", 3, [("1", "L-A", 3, 10)]),
               red("RD1", 4, "INV1", "1", 1))
        rows = fx.entries()
        # Segments: GR1 units[0,2)=6 cents, GR2 unit[2,3)=4 cents.
        # Reversing the tail unit must claw back exactly those 4 cents, not
        # a per-unit re-amortization of the original 10 cents.
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 2, 6),
            ("INV1", "INV1", "1", "GR2", 1, 4),
            ("RD1", "INV1", "1", "GR2", -1, -4),
        ])

    def test_multiple_red_vouchers_accumulate_in_order(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 4),
               invoice("INV1", 2, [("1", "L-A", 4, 400)]),
               red("RD1", 3, "INV1", "1", 1),
               red("RD2", 4, "INV1", "1", 1))
        rows = fx.entries()
        self.assertEqual(entry_signature(rows), [
            ("INV1", "INV1", "1", "GR1", 4, 400),
            ("RD1", "INV1", "1", "GR1", -1, -100),
            ("RD2", "INV1", "1", "GR1", -1, -100),
        ])


# ---------------------------------------------------------------------------
# batch rejection rules: all-or-nothing, stale state preserved
# ---------------------------------------------------------------------------
class TestRejection(unittest.TestCase):
    def _state_snapshot(self, fx):
        with open(ap_ledger.state_path(fx.workdir), "rb") as fh:
            return fh.read()

    def test_unknown_order_line_rejects_whole_batch(self):
        fx = LedgerFixture()
        snapshot = self._state_snapshot(fx)
        with self.assertRaises(ap_ledger.LedgerError):
            fx.add(receipt("GR1", 1, "L-Z", 1),
                   receipt("GR2", 1, "L-A", 1))
        self.assertEqual(self._state_snapshot(fx), snapshot)
        self.assertEqual(fx.state()["vouchers"], {})

    def test_dangling_red_reference_rejected(self):
        fx = LedgerFixture()
        with self.assertRaises(ap_ledger.LedgerError):
            fx.add(red("RD1", 9, "GHOST", "1", 1))
        # Same when the original invoice is deleted in the same batch without
        # deleting the red voucher.
        fx.add(receipt("GR1", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]),
               red("RD1", 3, "INV1", "1", 1))
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([op("delete", vid="INV1")])
        self.assertEqual(len(fx.entries()), 2)

    def test_red_before_or_equal_to_invoice_rejected(self):
        def fresh():
            fx = LedgerFixture()
            fx.add(receipt("GR1", 1, "L-A", 2),
                   invoice("INV1", 5, [("1", "L-A", 2, 200)]))
            return fx

        # An earlier business time, an equal time with an id sorting before
        # the invoice, and an equal time with an equal id are all "not after".
        for bad in (red("RD-4", 4, "INV1", "1", 1),
                    red("IN0", 5, "INV1", "1", 1)):
            fx = fresh()
            with self.assertRaises(ap_ledger.LedgerError):
                fx.add(bad)
            self.assertEqual(len(fx.entries()), 1)     # nothing was committed
        # An id sorting after the invoice at equal business time is strictly
        # after and accepted ("INV2" > "INV1" lexicographically).
        fx = fresh()
        fx.add(red("INV2", 5, "INV1", "1", 1))
        self.assertEqual(len(fx.entries()), 2)   # one positive, one negative row

    def test_cumulative_red_above_original_quantity_rejected(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 3),
               invoice("INV1", 2, [("1", "L-A", 3, 300)]),
               red("RD1", 3, "INV1", "1", 2))
        snapshot = self._state_snapshot(fx)
        with self.assertRaises(ap_ledger.LedgerError):
            fx.add(red("RD2", 4, "INV1", "1", 2))   # total 4 > Q=3
        self.assertEqual(self._state_snapshot(fx), snapshot)

    def test_duplicate_voucher_operation_in_batch_rejected(self):
        fx = LedgerFixture()
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([
                {"op": "add", "voucher": receipt("GR1", 1, "L-A", 1)},
                {"op": "add", "voucher": receipt("GR1", 2, "L-A", 1)},
            ])

    def test_add_existing_replace_missing_delete_missing_rejected(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 1))
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([op("add", voucher=receipt("GR1", 1, "L-A", 2))])
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([op("replace", voucher=receipt("NOPE", 1, "L-A", 1))])
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([op("delete", vid="NOPE")])

    def test_replace_cannot_change_type(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 1))
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([op("replace",
                         voucher=invoice("GR1", 1, [("1", "L-A", 1, 10)]))])

    def test_malformed_shapes_rejected(self):
        fx = LedgerFixture()
        bad_batches = [
            {},
            {"operations": "nope"},
            {"operations": [{"op": "explode"}]},
            {"operations": [{"op": "add", "voucher": {"voucher": "X"}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "receipt", "business_time": 1.5,
                "order_line": "L-A", "quantity": 1}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "receipt", "business_time": True,
                "order_line": "L-A", "quantity": 1}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "receipt", "business_time": 1,
                "order_line": "L-A", "quantity": 0}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "invoice", "business_time": 1,
                "lines": []}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "invoice", "business_time": 1,
                "lines": [{"line": "1", "order_line": "L-A",
                           "quantity": 1, "amount_cents": -1}]}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "invoice", "business_time": 1,
                "lines": [{"line": "1", "order_line": "L-A",
                           "quantity": 1, "amount_cents": 1},
                          {"line": "1", "order_line": "L-A",
                           "quantity": 1, "amount_cents": 1}]}}]},
            {"operations": [{"op": "add", "voucher": {
                "voucher": "X", "type": "red", "business_time": 1,
                "original_invoice": "I", "original_line": "1",
                "quantity": 1, "order_line": "L-A"}}]},
            {"operations": [{"op": "delete", "voucher": "X"}]},
        ]
        snapshot = self._state_snapshot(fx)
        for bad in bad_batches:
            with self.assertRaises(ap_ledger.LedgerError):
                ap_ledger.apply_batch(fx.workdir, bad)
        self.assertEqual(self._state_snapshot(fx), snapshot)


# ---------------------------------------------------------------------------
# corrections: replace / delete, order-line re-pointing, stale entries
# ---------------------------------------------------------------------------
class TestCorrections(unittest.TestCase):
    def test_replace_repoints_receipt_to_another_order_line(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]))
        self.assertEqual(len(fx.entries("L-A")), 1)
        fx.apply([op("replace",
                      voucher=receipt("GR1", 1, "L-B", 2))])
        # L-A loses its receipt support: the old entry must be withdrawn.
        self.assertEqual(fx.entries("L-A"), [])
        self.assertEqual(fx.entries("L-B"), [])
        state = fx.state()
        self.assertEqual(state["line_vouchers"]["L-A"], ["INV1"])
        self.assertEqual(state["line_vouchers"]["L-B"], ["GR1"])

    def test_replace_invoice_line_repoint_withdraws_stale_entries(self):
        fx = LedgerFixture()
        fx.add(receipt("GR-A", 1, "L-A", 2),
               receipt("GR-B", 1, "L-B", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]))
        fx.apply([op("replace", voucher=invoice("INV1", 2, [
            ("1", "L-B", 2, 200)]))])
        self.assertEqual(fx.entries("L-A"), [])
        self.assertEqual(entry_signature(fx.entries("L-B")),
                         [("INV1", "INV1", "1", "GR-B", 2, 200)])

    def test_red_follows_repointed_invoice_line(self):
        fx = LedgerFixture()
        fx.add(receipt("GR-A", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]),
               red("RD1", 3, "INV1", "1", 2))
        self.assertEqual(len(fx.entries("L-A")), 2)
        fx.add(receipt("GR-B", 1, "L-B", 2))
        fx.apply([op("replace", voucher=invoice("INV1", 2, [
            ("1", "L-B", 2, 200)]))])
        # The red voucher inherits the new order line; L-A keeps only its
        # receipt, the invoice and red entries are withdrawn from L-A.
        self.assertEqual(fx.entries("L-A"), [])
        self.assertEqual(fx.state()["line_vouchers"]["L-A"], ["GR-A"])
        rows_b = fx.entries("L-B")
        self.assertEqual({r["voucher"] for r in rows_b}, {"INV1", "RD1"})
        self.assertEqual(fx.state()["line_vouchers"]["L-B"],
                         ["GR-B", "INV1", "RD1"])

    def test_delete_withdraws_entries_and_releases_indexes(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]),
               red("RD1", 3, "INV1", "1", 1))
        fx.apply([op("delete", vid="RD1"), op("delete", vid="INV1"),
                  op("delete", vid="GR1")])
        self.assertEqual(fx.entries("L-A"), [])
        state = fx.state()
        self.assertEqual(state["vouchers"], {})
        self.assertEqual(state["red_index"], {})
        self.assertEqual(state["line_vouchers"]["L-A"], [])

    def test_recomputed_entries_match_fresh_rebuild_after_history_change(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 4),
               receipt("GR2", 2, "L-A", 4),
               invoice("INV1", 3, [("1", "L-A", 8, 800)]),
               red("RD1", 4, "INV1", "1", 3))
        before = fx.entries("L-A")
        # A correction that does not touch L-A must leave its entries alone;
        # one that does must equal a rebuild from the corrected voucher set.
        fx.add(receipt("GR-B", 1, "L-B", 1))
        self.assertEqual(fx.entries("L-A"), before)
        fx.apply([op("replace",
                      voucher=receipt("GR2", 2, "L-A", 6))])
        rebuilt = ap_ledger.settle_order_line(
            "L-A", "P1", fx.state()["line_vouchers"]["L-A"],
            fx.state()["vouchers"])
        self.assertEqual(fx.entries("L-A"), rebuilt)

    def test_unrelated_line_entries_keep_their_exact_serialized_form(self):
        import json as _json
        fx = LedgerFixture()
        fx.add(receipt("GR-A", 1, "L-A", 2),
               invoice("INV-A", 2, [("1", "L-A", 2, 200)]),
               receipt("GR-B", 1, "L-B", 2),
               invoice("INV-B", 2, [("1", "L-B", 2, 200)]))
        before = _json.dumps(fx.state()["entries"]["L-B"],
                             sort_keys=True)
        fx.add(receipt("GR-A2", 5, "L-A", 1))
        after = _json.dumps(fx.state()["entries"]["L-B"],
                            sort_keys=True)
        self.assertEqual(before, after)

    def test_replace_red_retargets_reference_and_line(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 8),
               invoice("INV1", 2, [("1", "L-A", 4, 400)]),
               invoice("INV2", 3, [("1", "L-A", 4, 400)]),
               red("RD1", 4, "INV1", "1", 1))
        # Retarget the red voucher from INV1 to the later INV2; both
        # invoices are fully settled, so the retargeted clawback books again.
        fx.apply([op("replace", voucher=red("RD1", 5, "INV2", "1", 1))])
        rows = fx.entries()
        red_rows = [r for r in rows if r["voucher"] == "RD1"]
        self.assertEqual([r["original_invoice"] for r in red_rows], ["INV2"])
        state = fx.state()
        self.assertEqual(state["red_index"], {"INV2": ["RD1"]})
        # The red index must never retain a stale reference to INV1.
        self.assertNotIn("INV1", state["red_index"])


# ---------------------------------------------------------------------------
# incrementality and scale: no re-scan of unrelated lines, no unit expansion
# ---------------------------------------------------------------------------
class TestScale(unittest.TestCase):
    def test_small_batch_does_not_resettle_unrelated_order_lines(self):
        fx = LedgerFixture()
        fx.add(receipt("GR-A", 1, "L-A", 2),
               invoice("INV-A", 2, [("1", "L-A", 2, 200)]),
               receipt("GR-B", 1, "L-B", 2),
               invoice("INV-B", 2, [("1", "L-B", 2, 200)]))
        calls = []
        original = ap_ledger.settle_order_line

        def spy(line, project, member_ids, vouchers):
            calls.append(line)
            return original(line, project, member_ids, vouchers)

        ap_ledger.settle_order_line = spy
        try:
            fx.add(receipt("GR-A2", 5, "L-A", 1))
        finally:
            ap_ledger.settle_order_line = original
        self.assertEqual(calls, ["L-A"])
        # L-B's stored entries are untouched and still served.
        self.assertEqual(len(fx.entries("L-B")), 1)

    def test_huge_quantities_settle_without_unit_expansion(self):
        fx = LedgerFixture()
        big = 10 ** 12
        t0 = time.perf_counter()
        fx.add(receipt("GR1", 1, "L-A", big),
               invoice("INV1", 2, [("1", "L-A", big, 10 ** 18 - 1)]),
               red("RD1", 3, "INV1", "1", big // 2))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0)
        rows = fx.entries()
        # One positive row and one red row, both over the single receipt.
        self.assertEqual(len(rows), 2)
        # Exact floor arithmetic on big integers, never rounded through floats.
        self.assertEqual(rows[0]["quantity"], big)
        self.assertEqual(rows[0]["amount_cents"], 10 ** 18 - 1)
        self.assertEqual(rows[1]["quantity"], -(big // 2))
        half_cents = ap_ledger._interval_cents(
            10 ** 18 - 1, big, big - big // 2, big)
        self.assertEqual(rows[1]["amount_cents"], -half_cents)

    def test_many_vouchers_stay_linearish(self):
        fx = LedgerFixture()
        n = 2000
        vouchers = []
        for i in range(n):
            vouchers.append(receipt(f"GR{i:05d}", i * 2, "L-A", 1))
            vouchers.append(invoice(
                f"INV{i:05d}", i * 2 + 1, [("1", "L-A", 1, 100)]))
        t0 = time.perf_counter()
        fx.add(*vouchers)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 10.0)
        self.assertEqual(len(fx.entries("L-A")), n)


# ---------------------------------------------------------------------------
# naive unit-expanded reference + fuzz
# ---------------------------------------------------------------------------
def _reference_entries(order_line, project, member_ids, vouchers):
    """The brief's rules, literally one unit at a time (small fuzz quantities)."""
    events = sorted((vouchers[v]["business_time"], v, vouchers[v]["type"])
                    for v in member_ids)
    available = []          # FIFO list of unit tags (receipt ids)
    receipts = set()
    inv_state = {}          # (inv,line) -> dict(Q,C,units,W,R)
    out = []

    def cum(C, Q, k):
        return (k * C) // Q

    for _, vid, vtype in events:
        v = vouchers[vid]
        if vtype == "receipt":
            receipts.add(vid)
            available.extend([vid] * v["quantity"])
        elif vtype == "invoice":
            for ln in sorted(
                    (ln for ln in v["lines"] if ln["order_line"] == order_line),
                    key=lambda ln: ln["line"]):
                Q, C = ln["quantity"], ln["amount_cents"]
                units = [None] * Q
                W = 0
                while W < Q and available:
                    units[W] = available.pop(0)
                    W += 1
                # positive entries: contiguous blocks sharing one receipt
                i = 0
                while i < W:
                    j = i + 1
                    while j < W and units[j] == units[i]:
                        j += 1
                    out.append({
                        "order_line": order_line, "project": project,
                        "voucher": vid, "original_invoice": vid,
                        "original_line": ln["line"], "receipt": units[i],
                        "quantity": j - i,
                        "amount_cents": cum(C, Q, j) - cum(C, Q, i)})
                    i = j
                inv_state[(vid, ln["line"])] = {"Q": Q, "C": C,
                                                "units": units, "W": W, "R": 0}
        else:
            st = inv_state[(v["original_invoice"], v["original_line"])]
            Q, C, units, W, R = st["Q"], st["C"], st["units"], st["W"], st["R"]
            remaining = v["quantity"]
            free = min(remaining, (Q - R) - W)
            st["R"] += free
            remaining -= free
            while remaining > 0:
                hi = st["W"]
                lo = hi - 1
                while lo - 1 >= 0 and units[lo - 1] == units[hi - 1] \
                        and remaining > (hi - lo):
                    lo -= 1
                take = min(remaining, hi - lo)
                lo = hi - take
                out.append({
                    "order_line": order_line, "project": project,
                    "voucher": vid,
                    "original_invoice": v["original_invoice"],
                    "original_line": v["original_line"],
                    "receipt": units[hi - 1], "quantity": -take,
                    "amount_cents": -(cum(C, Q, hi) - cum(C, Q, lo))})
                for k in range(lo, hi):
                    available.append(units[k])
                    units[k] = None
                st["W"] = lo
                st["R"] += take
                remaining -= take

    out.sort(key=lambda r: (vouchers[r["voucher"]]["business_time"],
                            r["voucher"], r["receipt"], r["original_line"]))
    return out


class TestFuzzAgainstReference(unittest.TestCase):
    def test_random_batches_match_unit_expanded_reference(self):
        import random
        rng = random.Random(20261124)
        lines = ["L-A", "L-B"]
        for trial in range(12):
            fx = LedgerFixture()
            ref_vouchers = {}          # voucher set after accepted batches
            for batch_no in range(15):
                operations = []
                touched = set()
                for _ in range(rng.randint(1, 6)):
                    existing = [v for v in ref_vouchers if v not in touched]
                    choice = rng.choice(["add", "add", "replace", "delete"]) \
                        if existing else "add"
                    if choice == "add":
                        vid = f"V{len(ref_vouchers):03d}-{rng.randint(0, 99999)}"
                        if vid in ref_vouchers or vid in touched:
                            continue
                        v = self._random_voucher(
                            rng, vid, lines, ref_vouchers)
                        if v is None:
                            continue
                        operations.append(op("add", voucher=v))
                        touched.add(vid)
                    elif choice == "replace":
                        vid = rng.choice(existing)
                        v = self._mutate(rng, ref_vouchers[vid], lines,
                                         ref_vouchers)
                        if v is None:
                            continue
                        operations.append(op("replace", voucher=v))
                        touched.add(vid)
                    else:
                        vid = rng.choice(existing)
                        operations.append(op("delete", vid=vid))
                        touched.add(vid)
                if not operations:
                    continue
                try:
                    ap_ledger.apply_batch(fx.workdir,
                                          {"operations": operations})
                except ap_ledger.LedgerError:
                    # The stored ledger and the reference both keep their
                    # previous voucher sets; nothing to mirror.
                    continue
                for o in operations:
                    if o["op"] == "delete":
                        ref_vouchers.pop(o["voucher"], None)
                    else:
                        ref_vouchers[o["voucher"]["voucher"]] = o["voucher"]
                for line in lines:
                    members = [
                        vid for vid, v in ref_vouchers.items()
                        if line in self._ref_lines(vid, v, ref_vouchers)]
                    expected = _reference_entries(
                        line, "P1" if line == "L-A" else "P2",
                        members, ref_vouchers)
                    got = ap_ledger.export_line(fx.workdir, line)["entries"]
                    self.assertEqual(
                        entry_signature(got), entry_signature(expected),
                        msg=f"trial {trial} batch {batch_no} line {line}")

    # -- fuzz helpers --------------------------------------------------------
    def _random_voucher(self, rng, vid, lines, ref_vouchers):
        kind = rng.choice(["receipt", "receipt", "invoice", "invoice", "red"])
        btime = rng.randint(0, 20)
        if kind == "receipt":
            return receipt(vid, btime, rng.choice(lines), rng.randint(1, 6))
        if kind == "invoice":
            return invoice(vid, btime, [
                (str(no + 1), rng.choice(lines), rng.randint(1, 6),
                 rng.randint(0, 500))
                for no in range(rng.randint(1, 2))])
        invoices = [v for v in ref_vouchers.values()
                    if v["type"] == "invoice"]
        if not invoices:
            return None
        inv = rng.choice(invoices)
        ln = rng.choice(inv["lines"])
        return red(vid, btime, inv["voucher"], ln["line"], rng.randint(1, 6))

    def _mutate(self, rng, old, lines, ref_vouchers):
        if old["type"] == "receipt":
            return receipt(old["voucher"],
                           rng.randint(0, 20), rng.choice(lines),
                           rng.randint(1, 6))
        if old["type"] == "invoice":
            return invoice(old["voucher"], rng.randint(0, 20), [
                (ln["line"], rng.choice(lines), rng.randint(1, 6),
                 rng.randint(0, 500)) for ln in old["lines"]])
        invoices = [v for v in ref_vouchers.values()
                    if v["type"] == "invoice"]
        if not invoices:
            return None
        inv = rng.choice(invoices)
        ln = rng.choice(inv["lines"])
        return red(old["voucher"], rng.randint(0, 20),
                   inv["voucher"], ln["line"], rng.randint(1, 6))

    def _ref_lines(self, vid, v, ref):
        if v["type"] == "receipt":
            return [v["order_line"]]
        if v["type"] == "invoice":
            return [ln["order_line"] for ln in v["lines"]]
        inv = ref.get(v["original_invoice"])
        if inv is None:
            return []
        for ln in inv["lines"]:
            if ln["line"] == v["original_line"]:
                return [ln["order_line"]]
        return []


# ---------------------------------------------------------------------------
# atomicity: vouchers and entries commit as one generation
# ---------------------------------------------------------------------------
class TestAtomicity(unittest.TestCase):
    def test_commit_is_one_os_replace_of_a_single_state_file(self):
        fx = LedgerFixture()
        os.makedirs(fx.workdir, exist_ok=True)
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((os.path.basename(src), os.path.basename(dst)))
            return real_replace(src, dst)

        os.replace = spy
        try:
            fx.add(receipt("GR1", 1, "L-A", 2),
                   invoice("INV1", 2, [("1", "L-A", 2, 200)]),
                   red("RD1", 3, "INV1", "1", 1))
        finally:
            os.replace = real_replace
        # Exactly one state-file replacement per applied batch.
        self.assertEqual(replaced, [("state.json.tmp", "state.json")])
        state = fx.state()
        # Vouchers and their entries are visible together in one generation.
        self.assertEqual(set(state["vouchers"]), {"GR1", "INV1", "RD1"})
        self.assertEqual(len(state["entries"]["L-A"]), 2)
        self.assertFalse(os.path.exists(
            os.path.join(fx.workdir, "state.json.tmp")))

    def test_rejected_batch_leaves_single_unchanged_generation(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 2))
        with open(ap_ledger.state_path(fx.workdir), "rb") as fh:
            before = fh.read()
        with self.assertRaises(ap_ledger.LedgerError):
            fx.apply([
                {"op": "add", "voucher": receipt("OK", 2, "L-A", 1)},
                {"op": "add", "voucher": receipt("BAD", 2, "L-Z", 1)},
            ])
        with open(ap_ledger.state_path(fx.workdir), "rb") as fh:
            self.assertEqual(fh.read(), before)
        # No temp files are left behind for a reader to pick up.
        self.assertEqual(
            [n for n in os.listdir(fx.workdir) if n.endswith(".tmp")], [])

    def test_export_never_observes_partial_generation(self):
        fx = LedgerFixture()
        fx.add(receipt("GR1", 1, "L-A", 2),
               invoice("INV1", 2, [("1", "L-A", 2, 200)]))
        exported = ap_ledger.export_line(fx.workdir, "L-A")
        # The stored entries always resolve against the stored vouchers.
        for row in exported["entries"]:
            self.assertIn(row["voucher"], fx.state()["vouchers"])
            self.assertIn(row["receipt"], fx.state()["vouchers"])


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    def test_full_cli_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "work")
            h = os.path.join(tmp, "handover.json")
            m = os.path.join(tmp, "mapping.json")
            with open(h, "w") as fh:
                json.dump({"projects": [
                    {"project": "P1", "status": "complete"}]}, fh)
            with open(m, "w") as fh:
                json.dump({"order_lines": [
                    {"order_line": "L-A", "project": "P1"}]}, fh)

            def run(*args):
                proc = subprocess.run(
                    [sys.executable, "-I", SOLVER, *args],
                    capture_output=True, text=True)
                return proc

            p = run("init", work, "--handover", h, "--order-lines", m)
            self.assertEqual(p.returncode, 0, p.stderr)

            batch = os.path.join(tmp, "batch.json")
            with open(batch, "w") as fh:
                json.dump({"operations": [
                    {"op": "add", "voucher": receipt("GR1", 1, "L-A", 3)},
                    {"op": "add", "voucher": invoice(
                        "INV1", 2, [("1", "L-A", 3, 300)])},
                ]}, fh)
            p = run("apply", work, batch)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('"status": "accepted"', p.stdout)

            out = os.path.join(tmp, "entries.json")
            p = run("export", work, "L-A", "--out", out)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(out) as fh:
                exported = json.load(fh)
            self.assertEqual(exported["project"], "P1")
            self.assertEqual(len(exported["entries"]), 1)

            p = run("export-all", work)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("entries", p.stdout)

            p = run("status", work)
            self.assertEqual(p.returncode, 0, p.stderr)
            p = run("vouchers", work)
            self.assertEqual(p.returncode, 0, p.stderr)

            # A rejected batch exits 2 and leaves the ledger intact.
            with open(batch, "w") as fh:
                json.dump({"operations": [
                    {"op": "add", "voucher": receipt("BAD", 1, "L-Z", 1)}]},
                    fh)
            p = run("apply", work, batch)
            self.assertEqual(p.returncode, 2)
            self.assertIn("REJECTED", p.stderr)
            p = run("export", work, "L-A")
            self.assertEqual(len(json.loads(p.stdout)["entries"]), 1)

            p = run("export", work, "L-Z")
            self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
