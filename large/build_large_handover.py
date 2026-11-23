"""Build a large handover batch and check only the hand-derivable facts.

The real batch has many projects sharing one multi-level evidence catalog.
This generator builds

* a roster instance (reusing the roster.py certificate interface, untouched),
* a shared catalog of ``chain_depth`` levels with several basis groups per
  derived version, plus a pure endorsement cycle that must never hold,
* a status log mixing visible/corrected/out-of-window records,
* per-project candidate lists that point into the shared catalog.

It then runs the handover builder and asserts only facts derivable by hand:
cutoff handling, the cycle exclusion, the shared-graph resolution count, and
the complete/incomplete partition.  The catalog is resolved once regardless of
the number of projects; no per-project catalog traversal is performed.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import roster  # noqa: E402
import handover  # noqa: E402

BUSINESS_CUTOFF = 1000
INTAKE_CUTOFF = 500
CHAIN_DEPTH = 12
N_PROJECTS = 8000
PREFIX_LEN = 40
TOP_K = 30


def build(n_projects=N_PROJECTS, chain_depth=CHAIN_DEPTH):
    rng_seed = 20261123
    import random
    rng = random.Random(rng_seed)

    # ---- roster: one region whose wide score bands still fix a long prefix.
    # The prefix projects get high verified-ish scores via large quality gap so
    # the certificate marks the whole fixed prefix as certain to be in top K.
    C = [f"P{i:05d}" for i in range(n_projects)]
    s = {c: rng.randint(0, 100) for c in C}
    order = sorted(C, key=lambda c: (-s[c], c))
    pref = order[:PREFIX_LEN]
    # Push the prefix clearly above the rest so the tail cannot be caught.
    for rank, c in enumerate(pref):
        s[c] = 1000 - rank
    low, high = 0, max(s.values())
    regions = {"R0": {"low": low, "high": high, "prefix": pref,
                      "tail": s[pref[-1]], "verified": {}}}
    inst = {"candidates": C, "regions": regions, "weights": {"R0": 1}}
    cert = roster.certificate(inst, K=TOP_K)
    must = cert["top_k"]["must_be_in_top_k"]
    may = cert["top_k"]["may_be_in_top_k"]
    assert len(must) >= PREFIX_LEN // 2, \
        ("test design needs a sizeable certain set", len(must))
    # This construction fixes the prefix; possible-only projects are covered by
    # the small demo and the unit tests instead.

    # ---- shared catalog: L originals, then D1..Dn chains with two groups each ----
    catalog = {f"L{i}": {"kind": "original"} for i in range(4)}
    records = []
    for vid in catalog:
        records.append({"version": vid, "effective_at": 10,
                        "received_at": 5, "state": "valid"})
    # L3 is revoked after the cutoff window: dependents must route around it.
    records.append({"version": "L3", "effective_at": 900,
                    "received_at": 450, "state": "revoked"})

    for level in range(1, chain_depth + 1):
        vid = f"D{level}"
        if level == 1:
            groups = [["L0", "L1"], ["L2", "L3"]]      # second group blocked by L3
        else:
            prev = f"D{level - 1}"
            # short direct ground to an original, plus the deep chain ground
            groups = [["L0"], [prev]]
        catalog[vid] = {"kind": "derived", "basis_groups": groups}
        records.append({"version": vid, "effective_at": 10 + level,
                        "received_at": 5 + level, "state": "valid"})

    # A pure endorsement cycle: valid records, no original ground anywhere.
    catalog["CY1"] = {"kind": "derived", "basis_groups": [["CY2"]]}
    catalog["CY2"] = {"kind": "derived", "basis_groups": [["CY1"]]}
    records += [
        {"version": "CY1", "effective_at": 20, "received_at": 8, "state": "valid"},
        {"version": "CY2", "effective_at": 20, "received_at": 8, "state": "valid"},
    ]
    # Out-of-window noise that must be invisible.
    records += [
        {"version": "L0", "effective_at": 1500, "received_at": 800, "state": "revoked"},
        {"version": f"D{chain_depth}", "effective_at": 1500,
         "received_at": 800, "state": "revoked"},
    ]

    required = ["business_license", "permit", "financial_report"]
    projects = {}
    # Every certain project references deep shared versions; a controlled
    # fraction is made incomplete by naming the cycle versions first.
    for i, pid in enumerate(must):
        if i % 3 == 0:
            # Deliberately incomplete: this item only names cycle versions that
            # can never ground; no holding candidate exists.
            projects[pid] = {
                "business_license": ["CY1", "CY2"],
                "permit": ["D1"],
                "financial_report": ["D2"],
            }
        else:
            projects[pid] = {
                "business_license": [f"D{chain_depth}", "D1"],
                "permit": ["D1", "D2"],
                "financial_report": ["D2", f"D{chain_depth}"],
            }
    # A non-certain project with perfect materials: must still be excluded.
    outside = [c for c in cert["per_candidate"] if c not in must][:1]
    if outside:
        projects[outside[0]] = {item: ["D1"] for item in required}

    ledger = {"ledger_id": "large-shared", "required_items": required,
              "catalog": catalog, "projects": projects, "status_log": records}
    return cert, ledger, must, may, outside


def main():
    t0 = time.time()
    cert, ledger, must, may, outside = build()
    print(f"projects={len(cert['per_candidate'])} certain={len(must)} "
          f"possible_only={len(may)} catalog_versions={len(ledger['catalog'])} "
          f"status_records={len(ledger['status_log'])}")

    t1 = time.time()
    valid = handover.resolve_statuses(ledger, BUSINESS_CUTOFF, INTAKE_CUTOFF)
    held = handover.resolve_catalog(handover.validate_ledger(ledger), valid)
    t2 = time.time()
    print(f"shared catalog resolved once in {t2 - t1:.3f}s; "
          f"holding versions={len(held)}")

    # Hand-derivable facts about the shared graph.
    assert "CY1" not in held and "CY2" not in held, "endorsement cycle must not hold"
    assert "L3" not in valid, "L3's governing visible record is the revocation"
    # D1 must use its first group (L0, L1) because the L3 route is blocked.
    assert held["D1"]["group_index"] == 0
    assert held["D1"]["ground_depth"] == 1
    # Every deeper D-level has the one-hop L0 group, so it grounds at depth 1.
    for level in range(2, CHAIN_DEPTH + 1):
        assert held[f"D{level}"]["ground_depth"] == 1
        assert held[f"D{level}"]["group_index"] == 0

    result = handover.build_handover(cert, ledger, BUSINESS_CUTOFF, INTAKE_CUTOFF,
                                     certificate_sha256="(large-run-digest)")
    t3 = time.time()
    print(f"handover list built in {t3 - t2:.3f}s; total {t3 - t0:.3f}s")

    complete = [p for p in result["projects"] if p["status"] == "complete"]
    incomplete = [p for p in result["projects"] if p["status"] == "incomplete"]
    print(f"handed projects={len(result['projects'])} complete={len(complete)} "
          f"incomplete={len(incomplete)}")
    assert len(result["projects"]) == len(must)
    # Only the deliberately broken every-third project is incomplete.
    broken = sum(1 for i in range(len(must)) if i % 3 == 0)
    assert len(incomplete) == broken
    # The non-certain project is absent even with all materials present.
    outside = [c for c in cert["per_candidate"] if c not in must][:1]
    if outside:
        assert outside[0] not in {p["project"] for p in result["projects"]}
    # Versions are detailed once: the shared graph is not expanded per project.
    vids = [v["version"] for v in result["versions"]]
    assert len(vids) == len(set(vids))
    assert "CY1" not in vids and "CY2" not in vids
    # Detail size stays bounded by the shared catalog, not by project count.
    assert len(vids) <= len(ledger["catalog"])
    print("shared detail versions listed:", len(vids))

    out = os.path.join(HERE, "large_handover_inputs.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"business_cutoff": BUSINESS_CUTOFF,
                   "intake_cutoff": INTAKE_CUTOFF,
                   "certificate": cert, "ledger": ledger}, fh)
    print("wrote", out)


if __name__ == "__main__":
    main()
