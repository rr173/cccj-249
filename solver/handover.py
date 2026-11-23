"""Handover lists for projects that are certain to be shortlisted.

The procurement office already holds a rank certificate produced by
``roster.py``.  This module never recomputes a rank and never touches that
certificate's interface: it reads the certificate as given, takes the projects
that are *certain* to be in the top K (``top_k.must_be_in_top_k`` -- projects
whose position is uncertain are included there as well), and decides, for a
caller-chosen business cutoff and intake cutoff, which versions of the
mandatory materials can be accepted.

Ledger model (all rules are stated in the brief)
------------------------------------------------
* ``required_items`` lists the material templates every shortlisted project
  must hand over.
* ``projects`` maps a project id to, per mandatory item, a priority-ordered
  list of candidate version ids (the list may be omitted or empty).  A
  reference always names an exact version; nothing is ever auto-substituted by
  a later submission of the same material.
* ``catalog`` defines each version exactly once.  A version is either an
  original (``"kind": "original"`` -- issued directly) or a derived version
  (``"kind": "derived"``) carrying one or more non-empty *basis groups*.
  Every version in a group must hold; any one group suffices; group order
  breaks ties between equally deep grounds.
* ``status_log`` records, per version, business effective times, intake times
  and ``valid``/``revoked`` states.  A record is usable only when both times
  are within the matching cutoffs.  The most recent effective time wins, and
  at the same effective time the correction with the latest intake time wins
  (ledger order breaks an exact tie).  Identical records count once; if a
  visible record with the same version and the same two times carries the
  opposite state, the run is rejected.  A version without a usable record is
  invalid and cannot ground anything.

Recognition may form cycles, but a cycle alone proves nothing: a derived
version only holds when one of its groups can, through a finite chain of
holding groups, reach valid originals.  When several groups hold, the one with
the shortest longest trace chain is chosen, ties resolved by catalog order.

Scale
-----
The catalog is resolved once, globally, before any project is looked at -- the
real batch shares one multi-level evidence graph across thousands of projects.
Resolution is a least-fixpoint process that only re-examines groups whose
members changed in the previous round; per project it is then just a walk over
its short candidate lists.  No project ever traverses the catalog and the
shared graph is never expanded into per-project paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Dict, List, Optional, Tuple


class HandoverError(Exception):
    """The certificate or ledger is unusable; no partial list may be emitted."""


# ---------------------------------------------------------------------------
# small structural helpers (every malformed input is a HandoverError)
# ---------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    # bool is a subclass of int; a time point must be a genuine integer.
    return isinstance(value, int) and not isinstance(value, bool)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise HandoverError(msg)


def _require_dict(obj: Any, where: str) -> Dict[str, Any]:
    _require(isinstance(obj, dict), f"{where} must be a JSON object")
    return obj


def _require_list(obj: Any, where: str) -> List[Any]:
    _require(isinstance(obj, list), f"{where} must be a JSON array")
    return obj


def _require_str(obj: Any, where: str) -> str:
    _require(isinstance(obj, str) and obj != "", f"{where} must be a non-empty string")
    return obj


def _known_fields(obj: Dict[str, Any], allowed: set, where: str) -> None:
    extra = set(obj) - allowed
    _require(not extra, f"{where}: unknown field(s) {sorted(extra)}")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_certificate(cert: Any) -> Dict[str, Any]:
    """Check the certificate carries the existing roster.py interface."""
    cert = _require_dict(cert, "certificate")
    _known_fields(
        cert,
        {"K", "candidate_count", "per_candidate", "top_k", "witnesses"},
        "certificate",
    )
    for field in ("K", "candidate_count", "per_candidate", "top_k"):
        _require(field in cert, f"certificate: missing field '{field}'")
    _require(_is_int(cert["K"]) and cert["K"] >= 0, "certificate: K must be a non-negative integer")
    _require(_is_int(cert["candidate_count"]) and cert["candidate_count"] >= 0,
             "certificate: candidate_count must be a non-negative integer")
    per = _require_dict(cert["per_candidate"], "certificate.per_candidate")
    top = _require_dict(cert["top_k"], "certificate.top_k")
    _known_fields(top, {"positions", "must_be_in_top_k", "may_be_in_top_k",
                        "cannot_be_in_top_k"}, "certificate.top_k")
    buckets = {}
    for name in ("must_be_in_top_k", "may_be_in_top_k", "cannot_be_in_top_k"):
        lst = _require_list(top.get(name), f"certificate.top_k.{name}")
        ids = [_require_str(c, f"certificate.top_k.{name}[{i}]") for i, c in enumerate(lst)]
        _require(len(set(ids)) == len(ids), f"certificate.top_k.{name} contains duplicates")
        buckets[name] = ids
    positions = _require_list(top.get("positions"), "certificate.top_k.positions")
    _require(len(positions) == cert["K"],
             f"certificate: {len(positions)} positions but K={cert['K']}")
    all_bucket = buckets["must_be_in_top_k"] + buckets["may_be_in_top_k"] + buckets["cannot_be_in_top_k"]
    _require(len(all_bucket) == cert["candidate_count"],
             "certificate: top_k buckets do not partition candidate_count projects")
    _require(set(all_bucket) == set(per),
             "certificate: top_k buckets and per_candidate disagree on project ids")
    _require(len(set(per)) == len(per), "certificate.per_candidate contains duplicate project ids")
    for c, entry in per.items():
        _require_dict(entry, f"certificate.per_candidate.{c}")
        for field in ("best_rank", "worst_rank"):
            _require(field in entry and _is_int(entry[field]) and entry[field] >= 1,
                     f"certificate.per_candidate.{c}.{field} must be a positive integer")
        _require(entry["best_rank"] <= entry["worst_rank"],
                 f"certificate.per_candidate.{c}: best_rank exceeds worst_rank")
    must = set(buckets["must_be_in_top_k"])
    for c in must:
        _require(per[c]["worst_rank"] <= cert["K"],
                 f"certificate: {c} is must-be-in but its worst_rank exceeds K")
    return cert


def validate_ledger(ledger: Any) -> Dict[str, Any]:
    """Structural validation, including every cross-reference."""
    ledger = _require_dict(ledger, "ledger")
    _known_fields(ledger, {"ledger_id", "required_items", "catalog",
                           "projects", "status_log"}, "ledger")
    if "ledger_id" in ledger:
        _require_str(ledger["ledger_id"], "ledger.ledger_id")

    required = _require_list(ledger.get("required_items"), "ledger.required_items")
    required = [_require_str(item, f"ledger.required_items[{i}]")
                for i, item in enumerate(required)]
    _require(len(required) == len(set(required)),
             "ledger.required_items contains duplicate item ids")
    _require(required, "ledger.required_items must not be empty")

    catalog_obj = _require_dict(ledger.get("catalog"), "ledger.catalog")
    catalog: Dict[str, Dict[str, Any]] = {}
    for vid, entry in catalog_obj.items():
        _require_str(vid, "ledger.catalog key")
        entry = _require_dict(entry, f"ledger.catalog.{vid}")
        kind = entry.get("kind")
        _require(kind in ("original", "derived"),
                 f"ledger.catalog.{vid}.kind must be 'original' or 'derived'")
        if kind == "original":
            _known_fields(entry, {"kind"}, f"ledger.catalog.{vid}")
            catalog[vid] = {"kind": "original", "groups": []}
        else:
            _known_fields(entry, {"kind", "basis_groups"}, f"ledger.catalog.{vid}")
            groups_raw = _require_list(entry.get("basis_groups"),
                                       f"ledger.catalog.{vid}.basis_groups")
            _require(groups_raw, f"ledger.catalog.{vid}: a derived version needs at least one basis group")
            groups = []
            seen_groups = set()
            for gi, group in enumerate(groups_raw):
                refs = _require_list(group, f"ledger.catalog.{vid}.basis_groups[{gi}]")
                _require(refs, f"ledger.catalog.{vid}.basis_groups[{gi}] must be non-empty")
                refs = [_require_str(r, f"ledger.catalog.{vid}.basis_groups[{gi}][{k}]")
                        for k, r in enumerate(refs)]
                _require(len(set(refs)) == len(refs),
                         f"ledger.catalog.{vid}.basis_groups[{gi}] contains duplicate versions")
                key = tuple(refs)
                _require(key not in seen_groups,
                         f"ledger.catalog.{vid}.basis_groups[{gi}] duplicates an earlier group")
                seen_groups.add(key)
                groups.append(refs)
            catalog[vid] = {"kind": "derived", "groups": groups}

    projects_obj = _require_dict(ledger.get("projects"), "ledger.projects")
    projects: Dict[str, Dict[str, List[str]]] = {}
    for pid, pentry in projects_obj.items():
        _require_str(pid, "ledger.projects key")
        pentry = _require_dict(pentry, f"ledger.projects.{pid}")
        per_item: Dict[str, List[str]] = {}
        for item, cand in pentry.items():
            _require_str(item, f"ledger.projects.{pid} key")
            lst = _require_list(cand, f"ledger.projects.{pid}.{item}")
            ids = [_require_str(v, f"ledger.projects.{pid}.{item}[{i}]")
                   for i, v in enumerate(lst)]
            _require(len(set(ids)) == len(ids),
                     f"ledger.projects.{pid}.{item} lists the same version twice")
            for v in ids:
                _require(v in catalog,
                         f"ledger.projects.{pid}.{item}: unknown version '{v}'")
            per_item[item] = ids
        projects[pid] = per_item

    log_raw = _require_list(ledger.get("status_log"), "ledger.status_log")
    status_log: List[Dict[str, Any]] = []
    for i, rec in enumerate(log_raw):
        rec = _require_dict(rec, f"ledger.status_log[{i}]")
        _known_fields(rec, {"version", "effective_at", "received_at", "state"},
                      f"ledger.status_log[{i}]")
        vid = _require_str(rec.get("version"), f"ledger.status_log[{i}].version")
        _require(vid in catalog,
                 f"ledger.status_log[{i}]: unknown version '{vid}'")
        eff = rec.get("effective_at")
        rcv = rec.get("received_at")
        _require(_is_int(eff), f"ledger.status_log[{i}].effective_at must be an integer")
        _require(_is_int(rcv), f"ledger.status_log[{i}].received_at must be an integer")
        state = rec.get("state")
        _require(state in ("valid", "revoked"),
                 f"ledger.status_log[{i}].state must be 'valid' or 'revoked'")
        status_log.append({"version": vid, "effective_at": eff,
                           "received_at": rcv, "state": state,
                           "order": i})

    return {
        "ledger_id": ledger.get("ledger_id"),
        "required_items": required,
        "catalog": catalog,
        "projects": projects,
        "status_log": status_log,
    }


# ---------------------------------------------------------------------------
# status records at the two cutoffs
# ---------------------------------------------------------------------------
def resolve_statuses(
    ledger: Dict[str, Any], business_cutoff: int, intake_cutoff: int
) -> Dict[str, Dict[str, Any]]:
    """Pick the one governing usable record per version.

    Usable: ``effective_at <= business_cutoff`` and
    ``received_at <= intake_cutoff``.  Max effective time wins; on a tie the
    max intake time wins; an exact tie keeps the first ledger record so that a
    genuinely identical correction is stable.  Opposite states sharing the
    same (version, effective, received) triple are a contradiction that
    rejects the run.  Versions without a usable record are simply absent from
    the result and therefore invalid.
    """
    log = ledger["status_log"]
    seen: set = set()                       # exact duplicate records count once
    usable: Dict[str, List[Dict[str, Any]]] = {}
    for rec in log:
        key = (rec["version"], rec["effective_at"], rec["received_at"], rec["state"])
        if key in seen:
            continue
        seen.add(key)
        if rec["effective_at"] > business_cutoff:
            continue
        if rec["received_at"] > intake_cutoff:
            continue
        usable.setdefault(rec["version"], []).append(rec)

    chosen: Dict[str, Dict[str, Any]] = {}
    for vid, records in usable.items():
        # Opposite states on the same version and the same two times are
        # visible and contradictory -> refuse to issue a list at all.
        by_time: Dict[Tuple[int, int], set] = {}
        for rec in records:
            by_time.setdefault((rec["effective_at"], rec["received_at"]), set()).add(rec["state"])
        for times, states in by_time.items():
            _require(len(states) == 1,
                     f"status_log: version '{vid}' has opposite states at the same "
                     f"effective/received times {times}; refusing to issue a handover list")
        best = records[0]
        for rec in records[1:]:
            if (rec["effective_at"], rec["received_at"]) > \
               (best["effective_at"], best["received_at"]):
                best = rec
            # equal pair cannot occur with a different state (checked above),
            # so nothing else can overtake the first-seen record here.
        if best["state"] == "valid":
            chosen[vid] = best
    return chosen


# ---------------------------------------------------------------------------
# catalog fixpoint: which derived versions hold, and on which group
# ---------------------------------------------------------------------------
def resolve_catalog(
    ledger: Dict[str, Any], valid_versions: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Resolve every catalog version against the usable status records.

    Returns one entry per version that may be used:

    ``{version: {"kind": ..., "ground_depth": int, "group_index": int|None,
                 "basis": [version, ...]}}``

    ``ground_depth`` is the longest trace chain down to an original under the
    chosen group (originals have depth 0).  A derived version holds only when a
    group is fully grounded; groups are tried in catalog order and the holding
    group with the smallest resulting depth is chosen.  Cycles whose members
    never reach valid originals never enter the fixpoint -- mutual endorsement
    alone cannot establish anything.

    Least fixpoint over rounds: in round d only versions that can ground at
    depth d can appear, so every version is settled at its minimum possible
    depth regardless of catalog ordering.  A group is re-examined only while
    unresolved; groups whose members changed in earlier rounds are the only
    work done in later rounds.
    """
    catalog = ledger["catalog"]
    held: Dict[str, Dict[str, Any]] = {}
    pending = set(catalog)
    for vid, entry in catalog.items():
        if entry["kind"] == "original" and vid in valid_versions:
            held[vid] = {"kind": "original", "ground_depth": 0,
                         "group_index": None, "basis": []}
            pending.discard(vid)
        elif entry["kind"] == "original":
            pending.discard(vid)          # invalid original: can never hold

    while True:
        newly: Dict[str, Dict[str, Any]] = {}
        still = set(pending)
        for vid in sorted(pending):       # deterministic scans; order does not
            entry = catalog[vid]          # affect the minimum-depth result
            if vid not in valid_versions:
                still.discard(vid)        # no usable valid record: permanently out
                continue
            best: Optional[Dict[str, Any]] = None
            for gi, group in enumerate(entry["groups"]):
                if any(m not in held for m in group):
                    continue              # member not grounded yet (or invalid)
                depth = 1 + max(held[m]["ground_depth"] for m in group)
                candidate = {"kind": "derived", "ground_depth": depth,
                             "group_index": gi, "basis": list(group)}
                if best is None or depth < best["ground_depth"]:
                    best = candidate
                # equal depth: keep the earlier group (catalog order wins)
            if best is not None:
                newly[vid] = best
                still.discard(vid)
        if not newly:
            # Anything still pending can never ground: all its groups contain
            # an invalid version or a member stuck in an ungrounded cycle.
            break
        held.update(newly)
        pending = still
    return held


# ---------------------------------------------------------------------------
# handover list
# ---------------------------------------------------------------------------
def _status_record_public(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {"effective_at": rec["effective_at"],
            "received_at": rec["received_at"],
            "state": rec["state"]}


def build_handover(
    cert: Any, ledger: Any, business_cutoff: int, intake_cutoff: int,
    certificate_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate both inputs and produce the handover list, or raise."""
    _require(_is_int(business_cutoff), "business cutoff must be an integer")
    _require(_is_int(intake_cutoff), "intake cutoff must be an integer")
    cert = validate_certificate(cert)
    ledger = validate_ledger(ledger)

    cert_ids = set(cert["per_candidate"])
    unknown = sorted(set(ledger["projects"]) - cert_ids)
    _require(not unknown, f"ledger.projects references project id(s) absent from the certificate: {unknown}")

    valid_records = resolve_statuses(ledger, business_cutoff, intake_cutoff)
    held = resolve_catalog(ledger, valid_records)

    must = cert["top_k"]["must_be_in_top_k"]          # already id-sorted
    may = cert["top_k"]["may_be_in_top_k"]
    required = ledger["required_items"]
    projects = ledger["projects"]

    project_entries: List[Dict[str, Any]] = []
    # Versions used anywhere are detailed exactly once.
    used: Dict[str, Dict[str, Any]] = {}

    def use_version(vid: str) -> None:
        if vid in used:
            return
        info = held[vid]
        used[vid] = {
            "kind": info["kind"],
            "status_record": _status_record_public(valid_records[vid]),
            "ground_depth": info["ground_depth"],
            "basis_group": (
                None if info["kind"] == "original"
                else {"group_index": info["group_index"], "basis": info["basis"]}
            ),
        }
        if info["kind"] == "derived":
            for member in info["basis"]:
                use_version(member)

    for pid in must:
        pentry = projects.get(pid, {})
        unknown_items = sorted(set(pentry) - set(required))
        _require(not unknown_items,
                 f"ledger.projects.{pid} lists item(s) not in required_items: {unknown_items}")
        selected: List[Dict[str, Any]] = []
        missing: List[str] = []
        complete = True
        for item in required:
            choice: Optional[str] = None
            for vid in pentry.get(item, []):   # priority order, first that holds
                if vid in held:
                    choice = vid
                    break
            if choice is None:
                complete = False
                missing.append(item)
            else:
                selected.append({"item": item, "version": choice})
        if complete:
            for sel in selected:
                use_version(sel["version"])
            project_entries.append({
                "project": pid,
                "status": "complete",
                "items": selected,
                "missing_items": [],
            })
        else:
            # Incomplete projects hand over no partial materials.
            project_entries.append({
                "project": pid,
                "status": "incomplete",
                "items": [],
                "missing_items": missing,
            })

    return {
        "certificate_sha256": certificate_sha256,
        "cutoffs": {
            "business_cutoff": business_cutoff,
            "intake_cutoff": intake_cutoff,
        },
        "selection_rule": {
            "projects": "top_k.must_be_in_top_k of the certificate (certain to be "
                        "shortlisted, including projects whose exact rank is uncertain)",
            "per_item": "first holding version in the ledger's candidate priority order",
            "basis_group": "holding group with the shortest longest trace chain; "
                           "ties resolved by catalog group order",
        },
        "summary": {
            "certain_project_count": len(must),
            "complete_project_count": sum(1 for p in project_entries if p["status"] == "complete"),
            "incomplete_project_count": sum(1 for p in project_entries if p["status"] == "incomplete"),
            "possible_only_projects_not_handed_over": list(may),
        },
        "projects": project_entries,
        "versions": [
            dict({"version": vid}, **used[vid]) for vid in sorted(used)
        ],
    }


def handover_from_files(
    certificate_path: str, ledger_path: str,
    business_cutoff: int, intake_cutoff: int,
) -> Dict[str, Any]:
    """Read both JSON files (hashing the exact certificate bytes) and build the list."""
    try:
        with open(certificate_path, "rb") as fh:
            cert_bytes = fh.read()
    except OSError as exc:
        raise HandoverError(f"cannot read certificate {certificate_path}: {exc}")
    try:
        cert_text = cert_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandoverError(f"certificate {certificate_path} is not UTF-8: {exc}")
    try:
        cert = json.loads(cert_text)
    except json.JSONDecodeError as exc:
        raise HandoverError(f"certificate {certificate_path} is not valid JSON: {exc}")
    digest = hashlib.sha256(cert_bytes).hexdigest()

    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            ledger = json.load(fh)
    except OSError as exc:
        raise HandoverError(f"cannot read ledger {ledger_path}: {exc}")
    except json.JSONDecodeError as exc:
        raise HandoverError(f"ledger {ledger_path} is not valid JSON: {exc}")

    return build_handover(cert, ledger, business_cutoff, intake_cutoff, digest)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the handover list for projects certain to be shortlisted")
    ap.add_argument("certificate", help="rank certificate JSON (roster.py output)")
    ap.add_argument("ledger", help="material ledger JSON")
    ap.add_argument("--business-cutoff", type=int, required=True,
                    help="inclusive business effective-time cutoff")
    ap.add_argument("--intake-cutoff", type=int, required=True,
                    help="inclusive intake-time cutoff")
    ap.add_argument("--out", default="", help="write the handover list to this file")
    args = ap.parse_args(argv)
    try:
        result = handover_from_files(
            args.certificate, args.ledger,
            args.business_cutoff, args.intake_cutoff)
    except HandoverError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
