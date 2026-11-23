"""Example 2: manually verified cells strengthen evidence outside the prefix.

R0 publishes C02 > C01 with tail 1, so C01 == 1 and C02 >= 2; C03 is unlisted
and can tie the tail only because its id sorts after C01's id.  R1 publishes
nothing, so it contributes a pure interval [1, 2] to everybody.

Without the manual check every candidate is tied at the extremes (C01 and C03
both range over ranks 2..3).  A single verified R1 cell pins the tail of the
table and collapses the uncertainty -- and it never contradicts the R0 board.
"""
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "solver"))
import roster  # noqa: E402

BASE = {
    "candidates": ["C01", "C02", "C03"],
    "regions": {
        "R0": {"low": 0, "high": 2, "prefix": ["C02", "C01"], "tail": 1, "verified": {}},
        "R1": {"low": 1, "high": 2, "prefix": [], "tail": None, "verified": {}},
    },
    "weights": {"R0": 2, "R1": 1},
}

# Manual audit in R1: C01 really scored 2 (legal: R1's whole range is [1, 2]).
WITH_MANUAL = json.loads(json.dumps(BASE))
WITH_MANUAL["regions"]["R1"]["verified"] = {"C01": 2}

# A contradictory manual claim: R0 says C01 is the tail at 1.
CONTRA = json.loads(json.dumps(BASE))
CONTRA["regions"]["R0"]["verified"] = {"C01": 2}


def enumerate_all(inst):
    C = inst["candidates"]
    regions = inst["regions"]
    per_region = {}
    for name, reg in regions.items():
        low, high = reg["low"], reg["high"]
        pref = reg.get("prefix") or []
        tail = reg.get("tail")
        ver = reg.get("verified") or {}
        rows = []
        for vals in itertools.product(range(low, high + 1), repeat=len(C)):
            s = dict(zip(C, vals))
            if any(s[c] != v for c, v in ver.items()):
                continue
            if pref:
                if s[pref[-1]] != tail:
                    continue
                ok = True
                for i in range(len(pref) - 1):
                    a, b = pref[i], pref[i + 1]
                    if s[a] < s[b] or (s[a] == s[b] and a > b):
                        ok = False
                        break
                if ok:
                    for c in C:
                        if c in pref:
                            continue
                        if s[c] > tail or (s[c] == tail and c < pref[-1]):
                            ok = False
                            break
                if not ok:
                    continue
            rows.append(s)
        per_region[name] = rows
    best = {c: 10**9 for c in C}
    worst = {c: -1 for c in C}
    for combo in itertools.product(*per_region.values()):
        scores = {
            c: sum(inst["weights"][r] * row[c] for r, row in zip(regions, combo))
            for c in C
        }
        order = sorted(C, key=lambda c: (-scores[c], c))
        for c in C:
            r = order.index(c) + 1
            best[c] = min(best[c], r)
            worst[c] = max(worst[c], r)
    return best, worst


def extremes(inst):
    best, worst, _, _, _ = roster.rank_bounds(inst)
    ref = roster.rank_bounds_reference(inst)
    assert (best, worst) == (ref[0], ref[1])
    assert (best, worst) == enumerate_all(inst)
    return best, worst


def main():
    b0, w0 = extremes(BASE)
    b1, w1 = extremes(WITH_MANUAL)
    print("without manual evidence:", {c: (b0[c], w0[c]) for c in sorted(BASE['candidates'])})
    print("with manual evidence:   ", {c: (b1[c], w1[c]) for c in sorted(BASE['candidates'])})

    # Before: no unique holder anywhere, 2 uncertain ranks.  After: order fixed.
    assert (b0, w0) == ({"C01": 2, "C02": 1, "C03": 2}, {"C01": 3, "C02": 1, "C03": 3})
    assert (b1, w1) == ({"C01": 2, "C02": 1, "C03": 3}, {"C01": 2, "C02": 1, "C03": 3})

    cert0 = roster.certificate(BASE, K=2)
    cert1 = roster.certificate(WITH_MANUAL, K=2)
    print("top-2 without:", json.dumps(cert0["top_k"]))
    print("top-2 with:   ", json.dumps(cert1["top_k"]))
    assert cert1["top_k"]["positions"][0]["certain_holder"] == "C02"
    assert cert1["top_k"]["positions"][1]["certain_holder"] == "C01"

    # The contradictory manual claim is rejected instead of being "averaged in".
    try:
        roster.certificate(CONTRA, K=2)
    except roster.Contradiction as exc:
        print("contradiction rejected:", exc)
    else:
        raise AssertionError("contradictory verified cell was accepted")


if __name__ == "__main__":
    main()
