"""Small, fully inspectable handover example.

Builds (and re-builds deterministically) the two inputs of solver/handover.py:

* ``certificate.json`` -- a genuine roster.py rank certificate for five
  projects.  With K=3, P1 and P2 are certain to be shortlisted (P1's exact
  position is certain too), while P3/P4/P5 can only possibly make the cut and
  are therefore not handed over.
* ``ledger.json`` -- a material ledger exercising every interesting rule:

  - originals valid and revoked; a derived document with two independent
    grounds (one short direct chain, one longer alternative);
  - a multi-level evidence chain D3 -> D2 -> D1 -> originals;
  - mutually recognising documents X1/X2 that form a cycle and can never be
    accepted, because neither side reaches a valid original;
  - out-of-window status records and a later correction (revocation) that
    overrides an earlier valid record for the same version;
  - one certain project with every mandatory item present, one that is missing
    an item (incomplete projects hand over no partial materials).

The script then runs the handover rule in-process and asserts the resulting
list, so ``python3 -I examples/handover_demo.py`` is a re-runnable check.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import roster  # noqa: E402
import handover  # noqa: E402

BUSINESS_CUTOFF = 100
INTAKE_CUTOFF = 50

INSTANCE = {
    "candidates": ["P1", "P2", "P3", "P4", "P5"],
    "regions": {
        "R1": {"low": 0, "high": 100,
               "prefix": ["P1", "P2"], "tail": 80, "verified": {}}
    },
    "weights": {"R1": 1},
}

LEDGER = {
    "ledger_id": "demo-ledger-001",
    "required_items": ["business_license", "permit", "financial_report"],
    "catalog": {
        # ---- originals (issued directly) ----
        "L1": {"kind": "original"},
        "L2": {"kind": "original"},
        "L3": {"kind": "original"},
        "L9-REVOKED": {"kind": "original"},
        # ---- derived versions with one or more basis groups ----
        # D1 holds on the short direct group [L1, L2] (depth 1) and also on a
        # longer independent ground [L3, D2] (depth 2): the short one wins.
        "D1": {"kind": "derived", "basis_groups": [
            ["L1", "L2"],
            ["L3", "D2"],
        ]},
        # D2 -> D1 -> originals, so D2 grounds at depth 2.
        "D2": {"kind": "derived", "basis_groups": [["D1"]]},
        # D3 -> D2 -> D1 -> originals: depth 3.
        "D3": {"kind": "derived", "basis_groups": [["D2"]]},
        # Permits: an accepted one and one that was corrected to revoked.
        "Q1": {"kind": "derived", "basis_groups": [["L1"]]},
        "Q2-REVOKED": {"kind": "derived", "basis_groups": [["L2"]]},
        # Pure endorsement cycle: X1 and X2 recognise each other but neither
        # group ever reaches a valid original.  They must never be accepted.
        "X1": {"kind": "derived", "basis_groups": [["X2"]]},
        "X2": {"kind": "derived", "basis_groups": [["X1"]]},
    },
    "projects": {
        # Certain project, every item covered.  Priority is absolute: D3 is a
        # holding version and listed first, so it is kept even though the
        # shallower D1 appears later.
        "P1": {
            "business_license": ["D3", "D1"],
            "permit": ["Q1"],
            "financial_report": ["X1", "D2"],          # X1 is an empty cycle, skip to D2
        },
        # Certain project with a revoked permit: no holding version -> incomplete.
        "P2": {
            "business_license": ["L1"],
            "permit": ["Q2-REVOKED"],
            # financial_report intentionally omitted
        },
        # Only possibly shortlisted: present in the ledger but never handed over.
        "P3": {
            "business_license": ["L1"],
            "permit": ["Q1"],
            "financial_report": ["D1"],
        },
    },
    # The log is deliberately shuffled and carries duplicates, out-of-window
    # records and a same-time contradiction-free correction chain.
    "status_log": [
        {"version": "L1", "effective_at": 10, "received_at": 5, "state": "valid"},
        {"version": "L2", "effective_at": 12, "received_at": 6, "state": "valid"},
        {"version": "L3", "effective_at": 15, "received_at": 7, "state": "valid"},
        {"version": "L3", "effective_at": 15, "received_at": 7, "state": "valid"},  # exact duplicate
        {"version": "D1", "effective_at": 20, "received_at": 10, "state": "valid"},
        {"version": "D2", "effective_at": 25, "received_at": 11, "state": "valid"},
        {"version": "D3", "effective_at": 30, "received_at": 12, "state": "valid"},
        {"version": "Q1", "effective_at": 18, "received_at": 9, "state": "valid"},
        # Q2 was valid, then revoked by a later correction (later effective time).
        {"version": "Q2-REVOKED", "effective_at": 10, "received_at": 4, "state": "valid"},
        {"version": "Q2-REVOKED", "effective_at": 40, "received_at": 20, "state": "revoked"},
        {"version": "L9-REVOKED", "effective_at": 8, "received_at": 3, "state": "valid"},
        {"version": "L9-REVOKED", "effective_at": 9, "received_at": 4, "state": "revoked"},
        # The cycle members are "valid" as records yet have no original ground.
        {"version": "X1", "effective_at": 20, "received_at": 8, "state": "valid"},
        {"version": "X2", "effective_at": 20, "received_at": 8, "state": "valid"},
        # Outside the cutoffs: invisible to this run.
        {"version": "L1", "effective_at": 130, "received_at": 60, "state": "revoked"},
    ],
}


def main():
    cert = roster.certificate(INSTANCE, K=3)
    assert cert["top_k"]["must_be_in_top_k"] == ["P1", "P2"]
    assert cert["top_k"]["may_be_in_top_k"] == ["P3", "P4", "P5"]

    cert_path = os.path.join(HERE, "certificate.json")
    ledger_path = os.path.join(HERE, "ledger.json")
    with open(cert_path, "w", encoding="utf-8") as fh:
        json.dump(cert, fh, indent=2)
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump(LEDGER, fh, indent=2)

    result = handover.handover_from_files(
        cert_path, ledger_path, BUSINESS_CUTOFF, INTAKE_CUTOFF)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    by_project = {p["project"]: p for p in result["projects"]}
    assert sorted(by_project) == ["P1", "P2"]            # possible-only P3/P4/P5 excluded
    assert result["summary"]["possible_only_projects_not_handed_over"] == \
        ["P3", "P4", "P5"]

    p1 = by_project["P1"]
    assert p1["status"] == "complete"
    picks = {item["item"]: item["version"] for item in p1["items"]}
    # D3 is listed first for the license and holds, so priority keeps it.
    assert picks["business_license"] == "D3"
    assert picks["permit"] == "Q1"
    # X1 cannot ground through the X1/X2 cycle, so the next candidate D2 is used.
    assert picks["financial_report"] == "D2"

    p2 = by_project["P2"]
    assert p2["status"] == "incomplete"
    assert p2["items"] == []                            # no partial handover
    assert p2["missing_items"] == ["permit", "financial_report"]

    versions = {v["version"]: v for v in result["versions"]}
    # Each referenced version appears once; the cycle versions never appear.
    assert "X1" not in versions and "X2" not in versions
    assert "Q2-REVOKED" not in versions
    assert "L9-REVOKED" not in versions
    # D1 chose its short direct group (depth 1), not the [L3, D2] alternative.
    assert versions["D1"]["ground_depth"] == 1
    assert versions["D1"]["basis_group"]["group_index"] == 0
    assert versions["D1"]["basis_group"]["basis"] == ["L1", "L2"]
    assert versions["D3"]["ground_depth"] == 3
    # L1 appears once even though several items reach it.
    assert sum(v["version"] == "L1" for v in result["versions"]) == 1
    # The governing record for every used version is the visible correction.
    assert versions["L1"]["status_record"] == \
        {"effective_at": 10, "received_at": 5, "state": "valid"}

    print("\nall demo assertions passed; wrote", cert_path, "and", ledger_path)


if __name__ == "__main__":
    main()
