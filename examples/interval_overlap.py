"""Example 3: overlapping intervals force a "may be in top K" bucket.

R0 publishes just C02 with tail 3, so C02 == 3 and every unlisted candidate is
capped at 3 (a tie at 3 needs an id above C02's id 2, which none of C01/C03/C04
has).  R1 publishes C03 > C01 > C02 with tail 1: C02 == 1, and the published
ids descend at both steps, so C01 >= 2 and C03 >= C01 + 1.

Composite weights (R0=2, R1=1) leave three candidates overlapping around the
cut: C01 5..6, C02 7..7, C03 6..9, C04 5..7.  For K=2 both positions are
uncertain, C02 must be in, C01 never is, and C03/C04 are genuine "may be"
cases -- without inventing any unobserved score.
"""
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "solver"))
import roster  # noqa: E402

INSTANCE = {
    "candidates": ["C01", "C02", "C03", "C04"],
    "regions": {
        "R0": {"low": 2, "high": 3, "prefix": ["C02"], "tail": 3, "verified": {}},
        "R1": {"low": 1, "high": 3, "prefix": ["C03", "C01", "C02"], "tail": 1, "verified": {}},
    },
    "weights": {"R0": 2, "R1": 1},
}


def enumerate_ranks(inst):
    C = inst["candidates"]
    regions = inst["regions"]
    per_region = {}
    for name, reg in regions.items():
        low, high = reg["low"], reg["high"]
        pref = reg.get("prefix") or []
        tail = reg.get("tail")
        rows = []
        for vals in itertools.product(range(low, high + 1), repeat=len(C)):
            s = dict(zip(C, vals))
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


def main():
    inst = INSTANCE
    C = inst["candidates"]
    bounds = roster.validate(inst)
    # Hand derivation.  R0: C01 == 3 (tail, id 1), C02 <= 2 (id 2 > id 1 can
    # tie 3), C03 <= 2, C04 <= 3.  R1: tail 1 gives C02 == 1, C01 and C03
    # forced above it (ids descend), C04 <= 1 (id 4 > id 2 may tie 1).
    expected = {
        "R0": {"C01": (2, 2), "C02": (3, 3), "C03": (2, 3), "C04": (2, 3)},
        "R1": {"C01": (1, 2), "C02": (1, 1), "C03": (2, 3), "C04": (1, 1)},
    }
    assert bounds == expected, (bounds, expected)

    best, worst, _, _, _ = roster.rank_bounds(inst)
    assert (best, worst) == enumerate_ranks(inst)
    print("rank extremes:", {c: (best[c], worst[c]) for c in sorted(C)})
    assert {c: (best[c], worst[c]) for c in C} == {
        "C01": (3, 4), "C02": (1, 2), "C03": (1, 3), "C04": (2, 4)
    }

    cert = roster.certificate(inst, K=2, witness_for=["C03", "C04"])
    top = cert["top_k"]
    print(json.dumps(top, indent=2))
    assert top["positions"][0]["certain_holder"] is None
    assert top["positions"][1]["certain_holder"] is None
    assert top["must_be_in_top_k"] == ["C02"]
    assert top["may_be_in_top_k"] == ["C03", "C04"]
    assert top["cannot_be_in_top_k"] == ["C01"]

    # C02 is certain to appear in the top two but its *position* is not fixed
    # (it can be 1 or 2): the "definitely in, position unknown" case.
    assert best["C02"] == 1 and worst["C02"] == 2

    # Two independently recomputable witnesses for each ambiguous candidate.
    wb = cert["witnesses"]["C03"]["best"]
    ww = cert["witnesses"]["C03"]["worst"]
    assert wb["rank"] == 1 and ww["rank"] == 3
    print("C03 best row:  ", wb["region_scores"], "->", wb["composite_scores"])
    print("C03 worst row: ", ww["region_scores"], "->", ww["composite_scores"])
    wb = cert["witnesses"]["C04"]["best"]
    ww = cert["witnesses"]["C04"]["worst"]
    assert wb["rank"] == 2 and ww["rank"] == 4
    print("C04 best row:  ", wb["region_scores"], "->", wb["composite_scores"])
    print("C04 worst row: ", ww["region_scores"], "->", ww["composite_scores"])


if __name__ == "__main__":
    main()
