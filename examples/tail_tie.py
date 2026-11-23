"""Example 1: the tail-tie rule depends on candidate ids.

Two candidates sit right at the region's tail score 5.  C07 may only share the
tail with the published last candidate if its id sorts after that candidate's
id; C02 may not.  The board is small enough to enumerate by hand, and every
claim is cross-checked against the solver and against a full enumeration.
"""
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "solver"))
import roster  # noqa: E402

# Region R0 publishes C04 then C03 with tail 4: the last published score is
# C03 == 4, and the published order C04 > C03 (ids descend) forces C04 >= 5.
# Unlisted candidates score <= 4; score 4 only if their id is above C03's id.
INSTANCE = {
    "candidates": ["C02", "C03", "C04", "C06", "C07"],
    "regions": {
        "R0": {
            "low": 0,
            "high": 5,
            "prefix": ["C04", "C03"],
            "tail": 4,
            "verified": {},
        }
    },
    "weights": {"R0": 1},
}


def enumerate_region(reg, C):
    low, high = reg["low"], reg["high"]
    pref = reg.get("prefix") or []
    tail = reg.get("tail")
    ver = reg.get("verified") or {}
    out = []
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
        out.append(s)
    return out


def main():
    inst = INSTANCE
    C = inst["candidates"]
    reg = inst["regions"]["R0"]
    bounds = roster.cell_bounds(reg, C)

    # Hand derivation: C03 == 4 (tail, last published); C04 sits above it and
    # ids descend C04 -> C03, so a strict step is forced: C04 == 5.  Unlisted
    # candidates are capped at 4, but a tie at 4 needs an id above C03's id 3:
    # C02 <= 3, while C06 and C07 may tie 4.
    expected = {
        "C03": (4, 4),
        "C04": (5, 5),
        "C02": (0, 3),
        "C06": (0, 4),
        "C07": (0, 4),
    }
    assert bounds == expected, (bounds, expected)

    best, worst, _, _, _ = roster.rank_bounds(inst)
    # Composite == region score, no ties in practice?  C06 and C07 both can be 4.
    print("per-candidate bounds:", json.dumps(bounds, sort_keys=True))
    print("rank extremes:", {c: (best[c], worst[c]) for c in sorted(C, key=str)})

    cert = roster.certificate(inst, K=3, witness_for=["C02", "C07"])
    print(json.dumps(cert["top_k"], indent=2))

    # Enumeration cross-check of the extremes.
    sols = enumerate_region(reg, C)

    def rank(s, c):
        order = sorted(C, key=lambda x: (-s[x], x))
        return order.index(c) + 1

    for c in C:
        rs = [rank(s, c) for s in sols]
        assert (min(rs), max(rs)) == (best[c], worst[c]), c

    # C07 can tie the tail, C02 cannot: exhibit both extremes as concrete rows.
    w_best = cert["witnesses"]["C07"]["best"]
    assert w_best["region_scores"]["R0"]["C07"] == 4
    w_c02 = cert["witnesses"]["C02"]["worst"]
    assert w_c02["region_scores"]["R0"]["C02"] <= 3
    print("witness C07 best row:", w_best["region_scores"]["R0"])
    print("witness C02 worst row:", w_c02["region_scores"]["R0"])

    # The mandatory step above the tail is honoured in every legal row.
    assert all(s["C04"] >= s["C03"] + 1 for s in sols)
    print("legal rows enumerated:", len(sols))


if __name__ == "__main__":
    main()
