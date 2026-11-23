"""Batch synchronization of review-group project conclusions.

Each working directory owns one fixed batch of projects.  Review groups feed
*stream files* of business records (group streams are 1-based consecutive
positions); a close request names a release number and one inclusive cutoff
position per group.  A successful close atomically publishes a JSON change
package that a data warehouse can apply directly on top of the previous one.

Why a merge step is needed
--------------------------
A project can be handed between groups.  A transfer is two legs carrying the
same transfer id (an ``transfer_out`` at the old group and an ``transfer_in``
at the new group, each occupying one position in its own group's stream), and
it only takes effect when both legs are inside the same close window and an
execution order exists that respects every group's own position order *and*
the business ownership at every step:

* a group may only write a ``pass``/``fail`` conclusion while it owns the
  project, and only the receiving group may write conclusions after the
  ``transfer_in``;
* the ``transfer_out`` is legal only while the group owns the project, moves
  the project into an in-flight state (no owner) and clears its conclusion;
* the ``transfer_in`` completes the handover to the new group.

Records whose business action is impossible at the ownership reachable within
the window reject the whole close -- they are never silently skipped.

Durability model
----------------
Everything needed to resume lives in one ``state.json`` written atomically
(temp file + ``os.replace``) under an exclusive file lock (one writer at a
time).  Published packages are additionally exported under ``packages/`` but
the state is authoritative: a retry of an already published release with the
same positions returns the identical package even if newer streams have since
arrived, and a release number can never be bound to a second set of positions.
A failed or awaiting close consumes no release number and advances no group;
already accepted stream records always remain stored.

Scale
-----
Ingestion only touches the incoming file plus the positions it names; a close
only scans each group's positions between its previously published position
and the requested cutoff.  Already published streams and untouched projects
are never re-scanned for a small incremental close.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

FORMAT_VERSION = 1
STATE_FILENAME = "state.json"
LOCK_FILENAME = "lock"
PACKAGES_DIRNAME = "packages"

CONCLUSION_ACTIONS = ("pass", "fail")
TRANSFER_ACTIONS = ("transfer_out", "transfer_in")
ALL_ACTIONS = CONCLUSION_ACTIONS + TRANSFER_ACTIONS


class SyncError(Exception):
    """A request or input file is rejected; no partial effect is committed."""


# ---------------------------------------------------------------------------
# small structural helpers (every malformed input is a SyncError)
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    # bool is a subclass of int; positions must be genuine integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SyncError(msg)


def _require_dict(obj: Any, where: str) -> Dict[str, Any]:
    _require(isinstance(obj, dict), f"{where} must be a JSON object")
    return obj


def _require_list(obj: Any, where: str) -> List[Any]:
    _require(isinstance(obj, list), f"{where} must be a JSON array")
    return obj


def _require_str(obj: Any, where: str) -> str:
    _require(isinstance(obj, str) and obj != "", f"{where} must be a non-empty string")
    return obj


def _load_json(path: str, what: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SyncError(f"cannot read {what} {path}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError(f"{what} {path} is not valid JSON: {exc}")


# ---------------------------------------------------------------------------
# state file, locking and atomic writes
# ---------------------------------------------------------------------------
def state_path(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILENAME)


def package_path(workdir: str, release: int) -> str:
    return os.path.join(workdir, PACKAGES_DIRNAME, f"release-{release}.json")


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
        raise SyncError(f"working directory {workdir} is not initialized (run 'init' first)")
    state = _load_json(path, "state file")
    state = _require_dict(state, "state file")
    for field in ("format_version", "projects", "groups", "ownership",
                  "conclusions", "records", "transfers", "published"):
        _require(field in state, f"state file: missing field '{field}'")
    _require(state["format_version"] == FORMAT_VERSION,
             f"state file: unsupported format_version {state['format_version']}")
    return state


def _atomic_write_json(path: str, payload: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_state(workdir: str, state: Dict[str, Any]) -> None:
    _atomic_write_json(state_path(workdir), state)


# ---------------------------------------------------------------------------
# initialization
# ---------------------------------------------------------------------------
def init_workdir(workdir: str, handover_path: str,
                 groups_path: str, ownership_path: str) -> Dict[str, Any]:
    """Create a fresh working directory from existing handover material.

    The sync scope is exactly the handover list's ``projects`` entries whose
    ``status`` is ``complete``.  The handover format itself is never modified
    and no shortlist/material qualification is recomputed here.
    """
    handover = _load_json(handover_path, "handover list")
    handover = _require_dict(handover, "handover list")
    entries = _require_list(handover.get("projects"), "handover list.projects")
    projects: List[str] = []
    for i, entry in enumerate(entries):
        entry = _require_dict(entry, f"handover list.projects[{i}]")
        pid = _require_str(entry.get("project"),
                           f"handover list.projects[{i}].project")
        _require("status" in entry,
                 f"handover list.projects[{i}]: missing 'status'")
        if entry["status"] == "complete":
            _require(pid not in projects,
                     f"handover list.projects: project {pid} listed twice")
            projects.append(pid)
    _require(projects, "handover list contains no complete projects; nothing to sync")

    groups_doc = _require_dict(_load_json(groups_path, "groups file"), "groups file")
    groups_raw = _require_list(groups_doc.get("groups"), "groups file.groups")
    groups: List[str] = []
    for i, g in enumerate(groups_raw):
        g = _require_str(g, f"groups file.groups[{i}]")
        _require(g not in groups, f"groups file.groups: group {g} listed twice")
        groups.append(g)
    _require(groups, "groups file.groups must not be empty")

    ownership_doc = _require_dict(
        _load_json(ownership_path, "ownership file"), "ownership file")
    ownership_raw = _require_dict(
        ownership_doc.get("ownership"), "ownership file.ownership")
    _require(set(ownership_raw) == set(projects),
             "ownership file.ownership must name exactly the complete projects "
             f"{sorted(projects)}; got {sorted(ownership_raw)}")
    ownership: Dict[str, str] = {}
    for pid in projects:
        owner = _require_str(ownership_raw[pid],
                             f"ownership file.ownership.{pid}")
        _require(owner in groups,
                 f"ownership file.ownership.{pid}: unknown group '{owner}'")
        ownership[pid] = owner

    os.makedirs(workdir, exist_ok=True)
    with _locked(workdir):
        if os.path.exists(state_path(workdir)):
            raise SyncError(
                f"working directory {workdir} already initialized; refusing to re-initialize")
        state = {
            "format_version": FORMAT_VERSION,
            "projects": projects,
            "groups": groups,
            # Every project starts published at position 0 with an empty conclusion.
            "ownership": ownership,
            "conclusions": {pid: None for pid in projects},
            "records": {g: {} for g in groups},
            # Incremental index of every transfer id ever seen in this batch,
            # so close-time checks never re-scan published streams.  An id
            # names one handover for the whole batch: "consistent" flips to
            # False if the id is ever reused with different handover content.
            "transfers": {},
            "published": {
                "last_release": 0,
                "positions": {g: 0 for g in groups},
                "releases": [],
            },
        }
        save_state(workdir, state)
        os.makedirs(os.path.join(workdir, PACKAGES_DIRNAME), exist_ok=True)
    return {
        "status": "initialized",
        "workdir": workdir,
        "projects": projects,
        "groups": groups,
        "ownership": ownership,
    }


# ---------------------------------------------------------------------------
# record validation and ingestion
# ---------------------------------------------------------------------------
def _canonical_record(raw: Any, index: int,
                      groups: List[str], projects: List[str]) -> Dict[str, Any]:
    """Structurally validate one stream record.

    Business legality (does the owning group actually own the project at this
    point) is deliberately not decided here -- files may arrive out of order,
    so the close is what judges actions against the reachable ownership.
    """
    where = f"record[{index}]"
    rec = _require_dict(raw, where)
    group = _require_str(rec.get("group"), f"{where}.group")
    _require(group in groups, f"{where}: unknown group '{group}'")
    position = rec.get("position")
    _require(_is_int(position) and position >= 1,
             f"{where}.position must be an integer >= 1")
    pid = _require_str(rec.get("project"), f"{where}.project")
    _require(pid in projects, f"{where}: unknown project '{pid}'")
    action = rec.get("action")
    _require(action in ALL_ACTIONS,
             f"{where}.action must be one of {list(ALL_ACTIONS)}")

    canonical: Dict[str, Any] = {
        "group": group, "position": position,
        "project": pid, "action": action,
    }
    if action in TRANSFER_ACTIONS:
        tid = _require_str(rec.get("transfer_id"), f"{where}.transfer_id")
        from_group = _require_str(rec.get("from_group"), f"{where}.from_group")
        to_group = _require_str(rec.get("to_group"), f"{where}.to_group")
        _require(from_group in groups, f"{where}: unknown from_group '{from_group}'")
        _require(to_group in groups, f"{where}: unknown to_group '{to_group}'")
        _require(from_group != to_group,
                 f"{where}: transfer from_group and to_group must differ")
        if action == "transfer_out":
            _require(group == from_group,
                     f"{where}: transfer_out must sit on the from_group's stream")
        else:
            _require(group == to_group,
                     f"{where}: transfer_in must sit on the to_group's stream")
        canonical.update(transfer_id=tid,
                         from_group=from_group, to_group=to_group)
    else:
        extra = set(rec) - {"group", "position", "project", "action"}
        _require(not extra, f"{where}: unexpected field(s) {sorted(extra)}")
    return canonical


def ingest_file(workdir: str, delivery_path: str) -> Dict[str, Any]:
    """Validate and merge one delivery file, all-or-nothing.

    A file with any malformed record, unknown group/project or position
    conflict is rejected as a whole: none of its other new records survive.
    Re-sent identical records at the same group/position are no-ops; a
    different record at an occupied position is a conflict.
    """
    doc = _load_json(delivery_path, "delivery file")
    doc = _require_dict(doc, "delivery file")
    records_raw = _require_list(doc.get("records"), "delivery file.records")

    with _locked(workdir):
        state = load_state(workdir)
        groups, projects = state["groups"], state["projects"]

        incoming: List[Dict[str, Any]] = []
        seen_in_file: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for i, raw in enumerate(records_raw):
            rec = _canonical_record(raw, i, groups, projects)
            key = (rec["group"], rec["position"])
            stored = state["records"][rec["group"]].get(str(rec["position"]))
            earlier = seen_in_file.get(key)
            if earlier is not None:
                _require(earlier == rec,
                         f"record[{i}]: position conflict at {rec['group']} "
                         f"position {rec['position']}: two different records in the same file")
            elif stored is not None:
                _require(stored == rec,
                         f"record[{i}]: position conflict at {rec['group']} "
                         f"position {rec['position']}: a different record is already stored")
            else:
                seen_in_file[key] = rec
                incoming.append(rec)

        # All checks passed first; only now mutate and commit atomically.
        transfers = state.setdefault("transfers", {})
        for rec in incoming:
            state["records"][rec["group"]][str(rec["position"])] = rec
            if rec["action"] not in TRANSFER_ACTIONS:
                continue
            entry = transfers.get(rec["transfer_id"])
            leg_key = (rec["group"], rec["position"])
            signature = (rec["project"], rec["from_group"], rec["to_group"])
            if entry is None:
                transfers[rec["transfer_id"]] = {
                    "project": rec["project"],
                    "from_group": rec["from_group"],
                    "to_group": rec["to_group"],
                    "consistent": True,
                    "leg_keys": [list(leg_key)],
                }
            else:
                if (entry["project"], entry["from_group"],
                        entry["to_group"]) != signature:
                    # The id was already bound to another handover in this batch.
                    entry["consistent"] = False
                if list(leg_key) not in entry["leg_keys"]:
                    entry["leg_keys"].append(list(leg_key))
        save_state(workdir, state)

        return {
            "status": "accepted",
            "new_records": len(incoming),
            "duplicate_records": len(records_raw) - len(incoming),
            "stored_positions": {
                g: max((int(p) for p in state["records"][g]), default=0)
                for g in groups
            },
        }


# ---------------------------------------------------------------------------
# close: window validation, ownership-respecting merge, package build
# ---------------------------------------------------------------------------
def _validate_close_request(raw: Any, groups: List[str]) -> Tuple[int, Dict[str, int]]:
    req = _require_dict(raw, "close request")
    release = req.get("release")
    _require(_is_int(release) and release >= 1,
             "close request.release must be an integer >= 1")
    positions = _require_dict(req.get("positions"), "close request.positions")
    _require(set(positions) == set(groups),
             "close request.positions must name exactly the groups "
             f"{sorted(groups)}; got {sorted(positions)}")
    cutoffs: Dict[str, int] = {}
    for g in groups:
        cutoff = positions[g]
        _require(_is_int(cutoff) and cutoff >= 0,
                 f"close request.positions.{g} must be a non-negative integer")
        cutoffs[g] = cutoff
    return release, cutoffs


def _window_records(state: Dict[str, Any],
                    cutoffs: Dict[str, int]) -> Dict[str, List[Dict[str, Any]]]:
    """Per group, the records strictly after the published position up to cutoff."""
    published = state["published"]["positions"]
    window: Dict[str, List[Dict[str, Any]]] = {}
    for g in state["groups"]:
        stored = state["records"][g]
        window[g] = [
            stored[str(p)]
            for p in range(published[g] + 1, cutoffs[g] + 1)
        ]
    return window


def _find_gaps(state: Dict[str, Any], cutoffs: Dict[str, int]) -> Dict[str, List[int]]:
    published = state["published"]["positions"]
    gaps: Dict[str, List[int]] = {}
    for g in state["groups"]:
        stored = state["records"][g]
        missing = [
            p for p in range(published[g] + 1, cutoffs[g] + 1)
            if str(p) not in stored
        ]
        if missing:
            gaps[g] = missing
    return gaps


def _validate_transfer_pairs(state: Dict[str, Any],
                             window: Dict[str, List[Dict[str, Any]]]) -> None:
    """Both legs of every in-window transfer must be present and agree.

    The other side may not be borrowed from outside the window.  A transfer id
    reused for a different handover anywhere in the batch, a lone leg,
    mismatched legs, or more than two legs on one id all reject the close.
    """
    legs: Dict[str, List[Dict[str, Any]]] = {}
    for recs in window.values():
        for rec in recs:
            if rec["action"] in TRANSFER_ACTIONS:
                legs.setdefault(rec["transfer_id"], []).append(rec)

    transfers = state.get("transfers", {})
    for tid, recs in legs.items():
        # Batch-wide binding: the incremental index covers streams already
        # published in earlier releases without re-scanning them.
        entry = transfers.get(tid)
        _require(entry is not None and entry["consistent"],
                 f"transfer {tid}: the transfer id is bound to more than one "
                 "handover in this batch")
        _require(len(entry["leg_keys"]) == 2,
                 f"transfer {tid}: a transfer must occupy exactly two stream "
                 f"positions in the batch, found {len(entry['leg_keys'])}")
        _require(len(recs) == 2,
                 f"transfer {tid}: expected both legs inside the close window, "
                 f"found {len(recs)} (the other side cannot be borrowed from "
                 "outside the requested positions)")
        out_legs = [r for r in recs if r["action"] == "transfer_out"]
        in_legs = [r for r in recs if r["action"] == "transfer_in"]
        _require(len(out_legs) == 1 and len(in_legs) == 1,
                 f"transfer {tid}: needs exactly one transfer_out and one transfer_in")
        out_rec, in_rec = out_legs[0], in_legs[0]
        for field in ("project", "from_group", "to_group"):
            _require(out_rec[field] == in_rec[field],
                     f"transfer {tid}: legs disagree on {field} "
                     f"({out_rec[field]!r} vs {in_rec[field]!r})")
        _require(out_rec["group"] == out_rec["from_group"]
                 and in_rec["group"] == in_rec["to_group"],
                 f"transfer {tid}: legs are not on the streams of their own groups")


def _merge_window(state: Dict[str, Any],
                  cutoffs: Dict[str, int]) -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
    """Apply the window in an order satisfying stream order and ownership.

    Each group's head (its earliest not-yet-applied position) is the only
    record of that group eligible, so each group's own position order is
    always respected.  A head is either applicable now, still waiting on
    another group's transfer leg, or impossible -- in which case no later
    ownership state can ever make it legal and the close is rejected.  When no
    head can proceed, no valid interleaving exists.
    """
    ownership = dict(state["ownership"])
    conclusions = dict(state["conclusions"])

    # Stacks: last element is the group's current head (earliest position).
    heads: Dict[str, List[Dict[str, Any]]] = {}
    for g in state["groups"]:
        heads[g] = list(reversed(_window_records(state, cutoffs)[g]))

    def describe(rec: Dict[str, Any]) -> str:
        return (f"{rec['group']} position {rec['position']} "
                f"({rec['action']} on {rec['project']})")

    while any(heads.values()):
        progressed = False
        # Deterministic scan; group order does not affect feasibility.
        for g in sorted(heads):
            if not heads[g]:
                continue
            rec = heads[g][-1]
            pid = rec["project"]
            owner = ownership[pid]

            if rec["action"] in CONCLUSION_ACTIONS:
                # Ownership is re-checked at apply time: an earlier head of
                # this group may have been a waiting transfer_in that let
                # other groups move the project in the meantime, so a later
                # conclusion must not slip through once the wait resolves.
                if owner == g:
                    conclusions[pid] = rec["action"]
                elif owner is None:
                    raise SyncError(
                        f"cannot apply {describe(rec)}: {pid} is in transfer "
                        "flight and no earlier record on this group's stream "
                        "can make the group own it again")
                else:
                    raise SyncError(
                        f"cannot apply {describe(rec)}: {pid} is owned by "
                        f"{owner}, not {g}; a conclusion requires current ownership")

            elif rec["action"] == "transfer_out":
                if owner != g:
                    cause = "is in transfer flight" if owner is None \
                        else f"is owned by {owner}"
                    raise SyncError(
                        f"cannot apply {describe(rec)}: group {g} does not own "
                        f"{pid} ({cause})")
                ownership[pid] = None          # in flight; old conclusions die
                conclusions[pid] = None

            else:  # transfer_in
                if owner is None:
                    ownership[pid] = rec["to_group"]
                    conclusions[pid] = None    # receiver starts with empty conclusion
                elif owner == g:
                    raise SyncError(
                        f"cannot apply {describe(rec)}: {pid} is already owned "
                        "by the receiving group")
                elif owner == rec["from_group"]:
                    # The matching transfer_out has not been applied yet; leave
                    # this head in place and let another group make progress.
                    continue
                else:
                    raise SyncError(
                        f"cannot apply {describe(rec)}: {pid} is owned by "
                        f"{owner}, neither the from_group {rec['from_group']} "
                        "nor in flight")

            heads[g].pop()
            progressed = True
            break

        if not progressed:
            pending = [describe(heads[g][-1]) for g in sorted(heads) if heads[g]]
            raise SyncError(
                "no interleaving satisfies both the per-group position order "
                f"and business ownership; stuck at: {pending}")

    return ownership, conclusions


def _build_package(state: Dict[str, Any], release: int,
                   cutoffs: Dict[str, int],
                   ownership: Dict[str, str],
                   conclusions: Dict[str, Optional[str]]) -> Dict[str, Any]:
    previous = dict(state["published"]["positions"])
    changes: List[Dict[str, Any]] = []
    for pid in sorted(state["projects"]):
        old_owner = state["ownership"][pid]
        old_conclusion = state["conclusions"][pid]
        if ownership[pid] != old_owner or conclusions[pid] != old_conclusion:
            # One row per project regardless of how often it changed in-window;
            # an empty conclusion is still emitted explicitly.
            changes.append({
                "project": pid,
                "owner": ownership[pid],
                "conclusion": conclusions[pid],
            })
    return {
        "release": release,
        "previous_positions": previous,
        "positions": dict(cutoffs),
        "changes": changes,
    }


def request_close(workdir: str, request_path: str) -> Tuple[str, Dict[str, Any]]:
    """Run a close request.

    Returns ``("published", package)`` or ``("awaiting", detail)``; raises
    ``SyncError`` on any rejection.  Nothing is persisted unless a package is
    published.
    """
    raw = _load_json(request_path, "close request")
    with _locked(workdir):
        state = load_state(workdir)
        groups = state["groups"]
        release, cutoffs = _validate_close_request(raw, groups)
        published = state["published"]

        # Idempotent retry of an already successful release.
        if release <= published["last_release"]:
            stored = next((r for r in published["releases"]
                           if r["release"] == release), None)
            _require(stored is not None,
                     f"release {release} is below the last published release "
                     f"{published['last_release']}")
            _require(stored["package"]["positions"] == cutoffs,
                     f"release {release} was already published with positions "
                     f"{stored['package']['positions']}; it cannot be rebound to "
                     f"{cutoffs}")
            return "published", stored["package"]

        _require(release == published["last_release"] + 1,
                 f"release {release} skips the expected next release "
                 f"{published['last_release'] + 1}; failed requests do not "
                 "consume release numbers")
        for g in groups:
            _require(cutoffs[g] >= published["positions"][g],
                     f"positions.{g}={cutoffs[g]} would roll back behind the "
                     f"published position {published['positions'][g]}")

        gaps = _find_gaps(state, cutoffs)
        if gaps:
            return "awaiting", {
                "status": "awaiting",
                "release": release,
                "missing_positions": gaps,
            }

        window = _window_records(state, cutoffs)
        _validate_transfer_pairs(state, window)
        ownership, conclusions = _merge_window(state, cutoffs)
        package = _build_package(state, release, cutoffs, ownership, conclusions)

        # Export the package first; state remains the authority, so a crash in
        # between leaves at worst an orphan file that a retry overwrites.
        os.makedirs(os.path.join(workdir, PACKAGES_DIRNAME), exist_ok=True)
        _atomic_write_json(package_path(workdir, release), package)
        state["ownership"] = ownership
        state["conclusions"] = conclusions
        published["positions"] = dict(cutoffs)
        published["last_release"] = release
        published["releases"].append({"release": release, "package": package})
        save_state(workdir, state)
        return "published", package


def fetch_package(workdir: str, release: int) -> Dict[str, Any]:
    """Return a published package, regenerating its export file if needed."""
    _require(_is_int(release) and release >= 1, "release must be an integer >= 1")
    with _locked(workdir):
        state = load_state(workdir)
        stored = next((r for r in state["published"]["releases"]
                       if r["release"] == release), None)
        _require(stored is not None,
                 f"release {release} has not been published")
        package = stored["package"]
        path = package_path(workdir, release)
        if not os.path.exists(path):
            os.makedirs(os.path.join(workdir, PACKAGES_DIRNAME), exist_ok=True)
            _atomic_write_json(path, package)
        return package


def workdir_status(workdir: str) -> Dict[str, Any]:
    with _locked(workdir):
        state = load_state(workdir)
        return {
            "status": "ok",
            "projects": list(state["projects"]),
            "groups": list(state["groups"]),
            "ownership": dict(state["ownership"]),
            "conclusions": dict(state["conclusions"]),
            "published_positions": dict(state["published"]["positions"]),
            "last_release": state["published"]["last_release"],
            "stored_positions": {
                g: max((int(p) for p in state["records"][g]), default=0)
                for g in state["groups"]
            },
            "record_counts": {
                g: len(state["records"][g]) for g in state["groups"]
            },
            "releases": [r["release"] for r in state["published"]["releases"]],
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Batch-sync review-group conclusions into JSON change packages")
    sub = ap.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize a working directory")
    p_init.add_argument("workdir")
    p_init.add_argument("--handover", required=True, help="existing handover list JSON")
    p_init.add_argument("--groups", required=True, help="group roster JSON")
    p_init.add_argument("--ownership", required=True, help="initial ownership JSON")

    p_ingest = sub.add_parser("ingest", help="accept a stream delivery file")
    p_ingest.add_argument("workdir")
    p_ingest.add_argument("delivery", help="delivery JSON with a 'records' array")

    p_close = sub.add_parser("close", help="request a close / publish a package")
    p_close.add_argument("workdir")
    p_close.add_argument("request", help="close request JSON (release + positions)")
    p_close.add_argument("--out", default="", help="also write the pure package here")

    p_fetch = sub.add_parser("fetch", help="re-fetch a published package")
    p_fetch.add_argument("workdir")
    p_fetch.add_argument("release", type=int)
    p_fetch.add_argument("--out", default="")

    sub.add_parser("status", help="show working directory state").add_argument("workdir")

    args = ap.parse_args(argv)
    try:
        if args.command == "init":
            result = init_workdir(args.workdir, args.handover,
                                  args.groups, args.ownership)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "ingest":
            result = ingest_file(args.workdir, args.delivery)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.command == "close":
            outcome, payload = request_close(args.workdir, args.request)
            if outcome == "awaiting":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0
            if args.out:
                _atomic_write_json(args.out, payload)
            print(json.dumps({"status": "published", "package": payload},
                             indent=2, ensure_ascii=False))
            return 0

        if args.command == "fetch":
            package = fetch_package(args.workdir, args.release)
            if args.out:
                _atomic_write_json(args.out, package)
            print(json.dumps(package, indent=2, ensure_ascii=False))
            return 0

        if args.command == "status":
            print(json.dumps(workdir_status(args.workdir),
                             indent=2, ensure_ascii=False))
            return 0
    except SyncError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
