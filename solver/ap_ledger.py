"""Local accounts-payable settlement ledger.

After procurement handover, finance still has to answer two questions about
supplier invoices: which invoice quantities are backed by goods actually
received, and which earlier bookings a red (reversal) voucher cancels.  This
module keeps that answer as a local ledger of JSON *vouchers* and the
*settlement entries* they generate against goods receipts, per order line.

Scope is fixed once, at initialization, and never re-judged here:

* the handover list's ``projects`` entries whose ``status`` is ``complete``
  define the eligible projects (no shortlist or material review is recomputed);
* a separately supplied, fixed order-line -> project table names the order
  lines the ledger accepts.  A voucher on any other order line is unknown and
  rejects its whole correction batch.

Voucher model
-------------
Three voucher types, each with a globally unique voucher id and an integer
business time:

* ``receipt`` -- a goods receipt naming one order line and a positive
  quantity;
* ``invoice`` -- one voucher carrying one or more lines; every line names an
  order line, a positive quantity and a non-negative total amount in cents;
* ``red`` -- names one original invoice line (voucher id + line number) and a
  positive quantity to reverse; it follows that invoice line's order line.

Corrections arrive as batches of ``add`` / ``replace`` / ``delete``
operations.  One voucher id may be operated on at most once per batch, a
replace keeps the id and the type (and may re-point a receipt or invoice line
at another order line), and batch order carries no meaning: the result is
defined by the voucher set after the whole batch applies.  Any malformed
batch, unknown order line, dangling original-invoice reference, red voucher
not strictly after its original invoice, or red cumulative quantity above the
original line quantity rejects the *whole* batch -- the stored vouchers and
entries stay untouched.

Settlement rules (per order line)
---------------------------------
Vouchers are ordered by ``(business_time, voucher id)``.

* An invoice line may only consume receipts preceding it that still hold
  balance, earliest first (FIFO); one invoice may split across several
  receipts and partially settle.  Quantity with no receipt balance produces no
  payable entry and is never auto-settled by receipts arriving later.
* A red voucher must be ordered after its original invoice.  Reversal starts
  at the not-yet-reversed tail of the original line and first cancels
  not-yet-settled units (no entry).  Only when it reaches already-settled
  units does it book negative entries, returning that quantity to the receipt
  that originally carried the segment, so later invoices can use it again.
* Money follows the original line's quantity order.  With original quantity
  ``Q`` and total ``C`` cents, the first ``k`` units book
  ``floor(k*C/Q)`` cents; any segment is the difference of two such
  cumulatives, so a reversal claws back the exact cents the segment originally
  booked -- it is never re-amortized from the reversal quantity.

Entries are exported per order line between every voucher and receipt that
actually settles or reverses quantity.  Zero-amount rows with non-zero
quantity are kept; combinations without movement produce no row.

Durability and scale
--------------------
All state lives in one ``state.json`` written atomically (temp file +
``os.replace``) under an exclusive file lock, so vouchers and their entries
can never be observed as two mixed generations after a crash; one writer is
supported.  A correction batch only re-validates and re-settles the order
lines its touched vouchers belong to -- the history of every other order line
is never re-scanned, and its stored entries stay byte-identical.  Quantities
are handled as interval endpoints and integer cumulative arithmetic, never
expanded per unit, so a single voucher may carry quantities of any size.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set, Tuple

FORMAT_VERSION = 1
STATE_FILENAME = "state.json"
LOCK_FILENAME = "lock"

RECEIPT = "receipt"
INVOICE = "invoice"
RED = "red"
VOUCHER_TYPES = (RECEIPT, INVOICE, RED)
OPERATIONS = ("add", "replace", "delete")


class LedgerError(Exception):
    """An input or batch is rejected; no partial effect is committed."""


# ---------------------------------------------------------------------------
# small structural helpers (every malformed input is a LedgerError)
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    # bool is a subclass of int; times and quantities must be genuine integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise LedgerError(msg)


def _require_dict(obj: Any, where: str) -> Dict[str, Any]:
    _require(isinstance(obj, dict), f"{where} must be a JSON object")
    return obj


def _require_list(obj: Any, where: str) -> List[Any]:
    _require(isinstance(obj, list), f"{where} must be a JSON array")
    return obj


def _require_str(obj: Any, where: str) -> str:
    _require(isinstance(obj, str) and obj != "",
             f"{where} must be a non-empty string")
    return obj


def _load_json(path: str, what: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise LedgerError(f"cannot read {what} {path}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what} {path} is not valid JSON: {exc}")


def _atomic_write_json(path: str, payload: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# state file and locking
# ---------------------------------------------------------------------------
def state_path(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILENAME)


@contextmanager
def _locked(workdir: str):
    """Exclusive lock for the whole command; one writer is supported at a time."""
    os.makedirs(workdir, exist_ok=True)
    lock_fh = open(os.path.join(workdir, LOCK_FILENAME), "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()


def load_state(workdir: str) -> Dict[str, Any]:
    path = state_path(workdir)
    if not os.path.exists(path):
        raise LedgerError(
            f"working directory {workdir} is not initialized (run 'init' first)")
    state = _load_json(path, "state file")
    state = _require_dict(state, "state file")
    for field in ("format_version", "order_line_project", "vouchers",
                  "line_vouchers", "red_index", "entries"):
        _require(field in state, f"state file: missing field '{field}'")
    _require(state["format_version"] == FORMAT_VERSION,
             f"state file: unsupported format_version {state['format_version']}")
    return state


def save_state(workdir: str, state: Dict[str, Any]) -> None:
    _atomic_write_json(state_path(workdir), state)


# ---------------------------------------------------------------------------
# initialization: fixed scope from the existing handover list
# ---------------------------------------------------------------------------
def init_workdir(workdir: str, handover_path: str, mapping_path: str) -> Dict[str, Any]:
    """Create a fresh ledger directory.

    The eligible projects are exactly the handover list's ``projects`` entries
    whose ``status`` is ``complete``.  The fixed order-line table must map
    every one of its lines to such a project; nothing about shortlisting or
    material qualification is decided here.
    """
    handover = _require_dict(_load_json(handover_path, "handover list"),
                             "handover list")
    entries = _require_list(handover.get("projects"),
                            "handover list.projects")
    complete: List[str] = []
    for i, entry in enumerate(entries):
        entry = _require_dict(entry, f"handover list.projects[{i}]")
        pid = _require_str(entry.get("project"),
                           f"handover list.projects[{i}].project")
        _require("status" in entry,
                 f"handover list.projects[{i}]: missing 'status'")
        if entry["status"] == "complete":
            _require(pid not in complete,
                     f"handover list.projects: project {pid} listed twice")
            complete.append(pid)
    _require(complete,
             "handover list contains no complete projects; nothing to ledger")

    mapping_doc = _require_dict(_load_json(mapping_path, "order-line mapping"),
                                "order-line mapping")
    raw_lines = _require_list(mapping_doc.get("order_lines"),
                              "order-line mapping.order_lines")
    order_line_project: Dict[str, str] = {}
    for i, raw in enumerate(raw_lines):
        where = f"order-line mapping.order_lines[{i}]"
        item = _require_dict(raw, where)
        _require(set(item) == {"order_line", "project"},
                 f"{where}: fields must be exactly order_line and project")
        line_id = _require_str(item.get("order_line"), f"{where}.order_line")
        project = _require_str(item.get("project"), f"{where}.project")
        _require(line_id not in order_line_project,
                 f"order-line mapping: order line {line_id} mapped twice")
        _require(project in complete,
                 f"{where}: project {project} is not a complete handover "
                 f"project {sorted(complete)}")
        order_line_project[line_id] = project
    _require(order_line_project,
             "order-line mapping contains no order lines; nothing to ledger")

    os.makedirs(workdir, exist_ok=True)
    with _locked(workdir):
        if os.path.exists(state_path(workdir)):
            raise LedgerError(
                f"working directory {workdir} already initialized; refusing to re-initialize")
        state = {
            "format_version": FORMAT_VERSION,
            "order_line_project": order_line_project,
            "vouchers": {},
            # Per order line, the voucher ids that settle there, sorted.  A red
            # voucher's line is the line of the invoice line it references.
            "line_vouchers": {line: [] for line in order_line_project},
            # invoice voucher id -> the red voucher ids referencing it, sorted
            "red_index": {},
            # order line -> exported entries (absent key means an empty list)
            "entries": {},
        }
        save_state(workdir, state)
        return {
            "status": "initialized",
            "workdir": workdir,
            "complete_projects": complete,
            "order_lines": order_line_project,
        }


# ---------------------------------------------------------------------------
# voucher canonicalization (strict; unknown fields are rejected)
# ---------------------------------------------------------------------------
def _canonical_voucher(raw: Any, known_lines: Dict[str, str],
                       where: str) -> Dict[str, Any]:
    obj = _require_dict(raw, where)
    vid = _require_str(obj.get("voucher"), f"{where}.voucher")
    vtype = obj.get("type")
    _require(vtype in VOUCHER_TYPES,
             f"{where}.type must be one of {list(VOUCHER_TYPES)}")
    btime = obj.get("business_time")
    _require(_is_int(btime), f"{where}.business_time must be an integer")

    if vtype == RECEIPT:
        _require(set(obj) == {"voucher", "type", "business_time",
                              "order_line", "quantity"},
                 f"{where}: unexpected or missing field(s) for a receipt")
        line_id = _require_str(obj.get("order_line"), f"{where}.order_line")
        _require(line_id in known_lines,
                 f"{where}: unknown order line '{line_id}'")
        qty = obj.get("quantity")
        _require(_is_int(qty) and qty >= 1,
                 f"{where}.quantity must be a positive integer")
        return {"voucher": vid, "type": RECEIPT, "business_time": btime,
                "order_line": line_id, "quantity": qty}

    if vtype == INVOICE:
        _require(set(obj) == {"voucher", "type", "business_time", "lines"},
                 f"{where}: unexpected or missing field(s) for an invoice")
        raw_lines = _require_list(obj.get("lines"), f"{where}.lines")
        _require(raw_lines, f"{where}.lines must not be empty")
        lines: List[Dict[str, Any]] = []
        seen_lines: Set[str] = set()
        for i, raw_line in enumerate(raw_lines):
            lwhere = f"{where}.lines[{i}]"
            ln = _require_dict(raw_line, lwhere)
            _require(set(ln) == {"line", "order_line", "quantity",
                                 "amount_cents"},
                     f"{lwhere}: unexpected or missing field(s)")
            line_no = _require_str(ln.get("line"), f"{lwhere}.line")
            _require(line_no not in seen_lines,
                     f"{where}: invoice line {line_no} listed twice")
            seen_lines.add(line_no)
            order_line = _require_str(ln.get("order_line"),
                                      f"{lwhere}.order_line")
            _require(order_line in known_lines,
                     f"{lwhere}: unknown order line '{order_line}'")
            qty = ln.get("quantity")
            _require(_is_int(qty) and qty >= 1,
                     f"{lwhere}.quantity must be a positive integer")
            amount = ln.get("amount_cents")
            _require(_is_int(amount) and amount >= 0,
                     f"{lwhere}.amount_cents must be a non-negative integer")
            lines.append({"line": line_no, "order_line": order_line,
                          "quantity": qty, "amount_cents": amount})
        return {"voucher": vid, "type": INVOICE, "business_time": btime,
                "lines": lines}

    # red voucher: its order line is derived from the original invoice line.
    _require(set(obj) == {"voucher", "type", "business_time",
                          "original_invoice", "original_line", "quantity"},
             f"{where}: unexpected or missing field(s) for a red voucher")
    original_invoice = _require_str(obj.get("original_invoice"),
                                    f"{where}.original_invoice")
    original_line = _require_str(obj.get("original_line"),
                                 f"{where}.original_line")
    qty = obj.get("quantity")
    _require(_is_int(qty) and qty >= 1,
             f"{where}.quantity must be a positive integer")
    return {"voucher": vid, "type": RED, "business_time": btime,
            "original_invoice": original_invoice,
            "original_line": original_line, "quantity": qty}


def _voucher_lines(vid: str, voucher: Dict[str, Any],
                   vouchers: Dict[str, Dict[str, Any]]) -> List[str]:
    """Order lines a voucher settles on, given the surrounding voucher set.

    A red voucher inherits the order line of the invoice line it references.
    Raises if that original invoice line does not exist (dangling reference).
    """
    vtype = voucher["type"]
    if vtype == RECEIPT:
        return [voucher["order_line"]]
    if vtype == INVOICE:
        return [ln["order_line"] for ln in voucher["lines"]]
    invoice = vouchers.get(voucher["original_invoice"])
    if invoice is None or invoice["type"] != INVOICE or not any(
            ln["line"] == voucher["original_line"] for ln in invoice["lines"]):
        raise LedgerError(
            f"red voucher {vid} references missing original invoice line "
            f"{voucher['original_invoice']} / {voucher['original_line']}")
    target = next(ln for ln in invoice["lines"]
                  if ln["line"] == voucher["original_line"])
    return [target["order_line"]]


# ---------------------------------------------------------------------------
# settlement of one order line
# ---------------------------------------------------------------------------
def _cumulative_cents(total_cents: int, quantity: int, k: int) -> int:
    """Cents booked by the first ``k`` units of an invoice line: floor(k*C/Q)."""
    return (k * total_cents) // quantity


def _interval_cents(total_cents: int, quantity: int, start: int, end: int) -> int:
    """Exact cents of the half-open unit interval [start, end)."""
    return (_cumulative_cents(total_cents, quantity, end)
            - _cumulative_cents(total_cents, quantity, start))


def _make_entry(order_line: str, project: str, event_voucher: str,
                original_invoice: str, original_line: str, receipt: str,
                quantity: int, amount_cents: int) -> Dict[str, Any]:
    return {
        "order_line": order_line,
        "project": project,
        "voucher": event_voucher,
        "original_invoice": original_invoice,
        "original_line": original_line,
        "receipt": receipt,
        "quantity": quantity,
        "amount_cents": amount_cents,
    }


def settle_order_line(order_line: str, project: str,
                      member_ids: List[str],
                      vouchers: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recompute every entry of one order line from its voucher set.

    ``member_ids`` are the vouchers belonging to this line (red vouchers
    included), already resolvable.  The function also enforces the line-level
    business rules: red vouchers strictly after their original invoice and
    cumulative red quantity within the original quantity.
    """
    # ---- validation pass: every red reference resolves inside this line ----
    invoice_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for vid in member_ids:
        voucher = vouchers[vid]
        if voucher["type"] == INVOICE:
            for ln in voucher["lines"]:
                if ln["order_line"] == order_line:
                    invoice_lookup[(vid, ln["line"])] = (voucher, ln)
    red_total: Dict[Tuple[str, str], int] = {}
    reds: List[Dict[str, Any]] = []
    for vid in member_ids:
        voucher = vouchers[vid]
        if voucher["type"] != RED:
            continue
        reds.append(voucher)
        key = (voucher["original_invoice"], voucher["original_line"])
        target = invoice_lookup.get(key)
        _require(target is not None,
                 f"red voucher {vid} references missing original invoice line "
                 f"{key[0]} / {key[1]} on order line {order_line}")
        invoice_voucher, invoice_line = target
        _require(
            (voucher["business_time"], vid)
            > (invoice_voucher["business_time"], invoice_voucher["voucher"]),
            f"red voucher {vid} must be ordered after its original invoice "
            f"{invoice_voucher['voucher']} (business time, then voucher id)")
        red_total[key] = red_total.get(key, 0) + voucher["quantity"]
    for key, total in red_total.items():
        _, invoice_line = invoice_lookup[key]
        _require(total <= invoice_line["quantity"],
                 f"red vouchers on {key[0]} / {key[1]} reverse {total} units "
                 f"but the original line has only {invoice_line['quantity']}")

    # ---- settlement pass in (business time, voucher id) order --------------
    events = sorted(
        (vouchers[vid]["business_time"], vid, vouchers[vid]["type"])
        for vid in member_ids)
    # Receipt pool in FIFO order; balances are returned to the originating
    # receipt when a red voucher claws a settled segment back.
    pool: List[Dict[str, Any]] = []
    pool_index: Dict[str, int] = {}
    head = 0          # first pool entry that might still hold balance
    # Invoice-line state keyed by (invoice id, line number): segments are
    # [receipt id, start unit, end unit]; W settled units, R reversed units.
    line_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
    entries: List[Dict[str, Any]] = []

    for _, vid, vtype in events:
        voucher = vouchers[vid]

        if vtype == RECEIPT:
            pool_index[vid] = len(pool)
            pool.append({"id": vid, "balance": voucher["quantity"]})
            continue

        if vtype == INVOICE:
            for ln in sorted(
                    (ln for ln in voucher["lines"]
                     if ln["order_line"] == order_line),
                    key=lambda ln: ln["line"]):
                qty_q, amount_c = ln["quantity"], ln["amount_cents"]
                state = {"Q": qty_q, "C": amount_c,
                         "segments": [], "W": 0, "R": 0}
                line_state[(vid, ln["line"])] = state
                need = qty_q
                while need > 0:
                    while head < len(pool) and pool[head]["balance"] == 0:
                        head += 1
                    if head == len(pool):
                        # Insufficient receipt balance: the rest books nothing
                        # and is never settled by later arrivals.
                        break
                    slot = pool[head]
                    take = min(need, slot["balance"])
                    slot["balance"] -= take
                    start, end = state["W"], state["W"] + take
                    state["segments"].append([slot["id"], start, end])
                    state["W"] = end
                    entries.append(_make_entry(
                        order_line, project, vid, vid, ln["line"], slot["id"],
                        take,
                        _interval_cents(amount_c, qty_q, start, end)))
                    need -= take
            continue

        # red voucher: unwind the original line from its tail
        key = (voucher["original_invoice"], voucher["original_line"])
        state = line_state[key]
        qty_q, amount_c = state["Q"], state["C"]
        remaining = voucher["quantity"]
        # First the not-yet-settled live tail: cancelling it books nothing.
        unwritten = (qty_q - state["R"]) - state["W"]
        free_take = min(remaining, max(unwritten, 0))
        state["R"] += free_take
        remaining -= free_take
        # Then settled units, newest segment first, exact cents per segment.
        # Red units leave at the global tail, so a touched segment keeps its
        # fixed low end and shrinks from its high end; a fully reversed
        # segment disappears and the next segment becomes the tail.
        while remaining > 0:
            receipt_id, seg_start, seg_end = state["segments"][-1]
            live_end = min(seg_end, state["W"])
            take = min(remaining, live_end - seg_start)
            new_end = live_end - take
            amount = _interval_cents(amount_c, qty_q, new_end, live_end)
            entries.append(_make_entry(
                order_line, project, vid,
                voucher["original_invoice"], voucher["original_line"],
                receipt_id, -take, -amount))
            slot = pool[pool_index[receipt_id]]
            slot["balance"] += take
            if pool_index[receipt_id] < head:
                # A returned earlier receipt becomes eligible again, and FIFO
                # must reach it before any later receipt.
                head = pool_index[receipt_id]
            state["R"] += take
            state["W"] = new_end
            remaining -= take
            if new_end == seg_start:
                state["segments"].pop()
            else:
                state["segments"][-1][2] = new_end

    entries.sort(key=lambda row: (
        vouchers[row["voucher"]]["business_time"],
        row["voucher"], row["receipt"], row["original_line"]))
    return entries


# ---------------------------------------------------------------------------
# batch application
# ---------------------------------------------------------------------------
def _canonical_batch(raw: Any,
                     known_lines: Dict[str, str]) -> List[Dict[str, Any]]:
    doc = _require_dict(raw, "correction batch")
    raw_ops = _require_list(doc.get("operations"), "correction batch.operations")
    _require(set(doc) == {"operations"},
             "correction batch: only an 'operations' array is allowed")
    operations: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for i, raw_op in enumerate(raw_ops):
        where = f"correction batch.operations[{i}]"
        op_obj = _require_dict(raw_op, where)
        action = op_obj.get("op")
        _require(action in OPERATIONS,
                 f"{where}.op must be one of {list(OPERATIONS)}")
        if action == "delete":
            _require(set(op_obj) == {"op", "voucher"},
                     f"{where}: a delete takes only op and voucher id")
            vid = _require_str(op_obj.get("voucher"), f"{where}.voucher")
            operations.append({"op": action, "voucher_id": vid})
        else:
            _require(set(op_obj) == {"op", "voucher"},
                     f"{where}: {action} takes op plus a voucher object")
            voucher = _canonical_voucher(
                op_obj.get("voucher"), known_lines, f"{where}.voucher")
            vid = voucher["voucher"]
            operations.append({"op": action, "voucher_id": vid,
                               "voucher": voucher})
        _require(vid not in seen,
                 f"correction batch: voucher {vid} operated on twice")
        seen.add(vid)
    return operations


def apply_batch(workdir: str, batch_doc: Any) -> Dict[str, Any]:
    """Validate and apply one correction batch, all-or-nothing.

    Only the order lines the batch's touched vouchers belong to (before and
    after the batch) are re-validated and re-settled; every other order line
    keeps its stored vouchers and entries untouched.
    """
    with _locked(workdir):
        state = load_state(workdir)
        known_lines = state["order_line_project"]
        operations = _canonical_batch(batch_doc, known_lines)
        stored = state["vouchers"]

        # ---- existence / type rules before anything is mutated --------------
        for op in operations:
            vid = op["voucher_id"]
            if op["op"] == "add":
                _require(vid not in stored,
                         f"add: voucher {vid} already exists; use replace")
            else:
                _require(vid in stored,
                         f"{op['op']}: voucher {vid} does not exist")
                if op["op"] == "replace":
                    _require(op["voucher"]["type"] == stored[vid]["type"],
                             f"replace: voucher {vid} cannot change type "
                             f"({stored[vid]['type']} -> {op['voucher']['type']})")

        # ---- build the candidate voucher set --------------------------------
        new_vouchers = dict(stored)
        for op in operations:
            vid = op["voucher_id"]
            if op["op"] == "add" or op["op"] == "replace":
                new_vouchers[vid] = op["voucher"]
            else:
                del new_vouchers[vid]

        # ---- affected order lines: old and new lines of every touch ---------
        # Red vouchers dragged by a re-pointed invoice are discovered below,
        # after the red index is refreshed.
        affected: Set[str] = set()
        for op in operations:
            vid = op["voucher_id"]
            if vid in stored:
                # The pre-batch set is known consistent, so line derivation
                # cannot dangle here.
                affected.update(_voucher_lines(vid, stored[vid], stored))
            if "voucher" in op:
                affected.update(
                    _voucher_lines(vid, op["voucher"], new_vouchers))

        # ---- reindex only the affected order-line groups --------------------
        line_groups: Dict[str, Set[str]] = {
            line: set(state["line_vouchers"].get(line, []))
            for line in affected}
        for op in operations:
            vid = op["voucher_id"]
            if vid in stored:
                for line in _voucher_lines(vid, stored[vid], stored):
                    line_groups[line].discard(vid)
            if "voucher" in op:
                for line in _voucher_lines(vid, op["voucher"], new_vouchers):
                    line_groups.setdefault(line, set()).add(vid)

        # ---- refresh the red index for the touched red vouchers -------------
        red_index = {k: list(v) for k, v in state["red_index"].items()}
        for op in operations:
            vid = op["voucher_id"]
            old = stored.get(vid)
            if old is not None and old["type"] == RED:
                refs = red_index.setdefault(old["original_invoice"], [])
                refs.remove(vid)
                if not refs:
                    del red_index[old["original_invoice"]]
            new = op.get("voucher")
            if new is not None and new["type"] == RED:
                red_index.setdefault(new["original_invoice"], []).append(vid)

        # A red voucher's order line is its invoice line's line, so an invoice
        # re-pointed at another order line drags every referencing red voucher
        # along, even when the red voucher itself is not in the batch.
        touched_invoices = {
            op["voucher_id"] for op in operations
            if (op["voucher_id"] in stored
                and stored[op["voucher_id"]]["type"] == INVOICE)
            or ("voucher" in op and op["voucher"]["type"] == INVOICE)}
        for invoice_id in touched_invoices:
            for red_id in red_index.get(invoice_id, []):
                if red_id not in new_vouchers:
                    continue
                if red_id in stored:
                    for line in _voucher_lines(red_id, stored[red_id], stored):
                        line_groups.setdefault(line, set()).discard(red_id)
                for line in _voucher_lines(
                        red_id, new_vouchers[red_id], new_vouchers):
                    line_groups.setdefault(line, set()).add(red_id)
                    affected.add(line)

        # ---- re-settle the affected lines; any rule failure rejects all -----
        new_entries: Dict[str, List[Dict[str, Any]]] = {}
        for line in sorted(affected):
            member_ids = sorted(line_groups.get(line, set()))
            entries = settle_order_line(
                line, known_lines[line], member_ids, new_vouchers)
            if entries:
                new_entries[line] = entries

        # ---- commit vouchers, indexes and entries in one atomic write -------
        for line in affected:
            state["line_vouchers"][line] = sorted(line_groups.get(line, set()))
            state["entries"].pop(line, None)
        state["entries"].update(new_entries)
        state["red_index"] = {k: sorted(v) for k, v in red_index.items() if v}
        state["vouchers"] = new_vouchers
        save_state(workdir, state)

        return {
            "status": "accepted",
            "operations": len(operations),
            "affected_order_lines": sorted(affected),
            "voucher_count": len(new_vouchers),
            "entry_counts": {line: len(rows)
                             for line, rows in sorted(state["entries"].items())},
        }


def apply_batch_file(workdir: str, batch_path: str) -> Dict[str, Any]:
    return apply_batch(workdir, _load_json(batch_path, "correction batch"))


# ---------------------------------------------------------------------------
# exports and inspection
# ---------------------------------------------------------------------------
def export_line(workdir: str, order_line: str) -> Dict[str, Any]:
    """Export the settled entries of one order line (finance's JSON view)."""
    with _locked(workdir):
        state = load_state(workdir)
        mapping = state["order_line_project"]
        _require(order_line in mapping,
                 f"unknown order line '{order_line}'")
        return {
            "order_line": order_line,
            "project": mapping[order_line],
            "entries": [dict(row) for row in state["entries"].get(order_line, [])],
        }


def export_all(workdir: str) -> Dict[str, Any]:
    """Export every entry of every order line in one global order."""
    with _locked(workdir):
        state = load_state(workdir)
        vouchers = state["vouchers"]
        rows: List[Dict[str, Any]] = []
        for line in state["order_line_project"]:
            rows.extend(state["entries"].get(line, []))
        rows.sort(key=lambda row: (
            row["order_line"],
            vouchers[row["voucher"]]["business_time"],
            row["voucher"], row["receipt"], row["original_line"]))
        return {"entries": rows}


def ledger_status(workdir: str) -> Dict[str, Any]:
    with _locked(workdir):
        state = load_state(workdir)
        counts = {t: 0 for t in VOUCHER_TYPES}
        for voucher in state["vouchers"].values():
            counts[voucher["type"]] += 1
        return {
            "status": "ok",
            "order_lines": dict(state["order_line_project"]),
            "voucher_counts": counts,
            "entry_counts": {line: len(rows)
                             for line, rows in sorted(state["entries"].items())},
            "total_entries": sum(len(rows)
                                 for rows in state["entries"].values()),
        }


def list_vouchers(workdir: str) -> Dict[str, Any]:
    with _locked(workdir):
        state = load_state(workdir)
        return {"vouchers": [state["vouchers"][vid]
                             for vid in sorted(state["vouchers"])]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Local accounts-payable settlement ledger")
    sub = ap.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize a ledger working directory")
    p_init.add_argument("workdir")
    p_init.add_argument("--handover", required=True,
                        help="existing handover list JSON")
    p_init.add_argument("--order-lines", required=True,
                        help="fixed order-line -> project mapping JSON")

    p_apply = sub.add_parser("apply", help="apply a correction batch (all-or-nothing)")
    p_apply.add_argument("workdir")
    p_apply.add_argument("batch", help="batch JSON with an 'operations' array")

    p_export = sub.add_parser("export", help="export entries of one order line")
    p_export.add_argument("workdir")
    p_export.add_argument("order_line")
    p_export.add_argument("--out", default="")

    p_all = sub.add_parser("export-all", help="export every entry")
    p_all.add_argument("workdir")
    p_all.add_argument("--out", default="")

    sub.add_parser("status", help="show ledger status").add_argument("workdir")
    sub.add_parser("vouchers", help="list stored vouchers").add_argument("workdir")

    args = ap.parse_args(argv)
    try:
        if args.command == "init":
            result = init_workdir(args.workdir, args.handover, args.order_lines)
        elif args.command == "apply":
            result = apply_batch_file(args.workdir, args.batch)
        elif args.command == "export":
            result = export_line(args.workdir, args.order_line)
            if args.out:
                _atomic_write_json(args.out, result)
        elif args.command == "export-all":
            result = export_all(args.workdir)
            if args.out:
                _atomic_write_json(args.out, result)
        elif args.command == "status":
            result = ledger_status(args.workdir)
        else:  # vouchers
            result = list_vouchers(args.workdir)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except LedgerError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
