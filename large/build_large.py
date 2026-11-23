"""Build a large synthetic roster and derive only the key endpoints by hand.

The roster has many candidates and a handful of manually verified cells; the
solver never enumerates the space and this script never does either.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "solver"))
import roster  # noqa: E402


def build(n_candidates=400, n_regions=5, prefix_len=80, noise=60, seed=20261123):
    """Hidden quality plus per-region noise; prefix_len candidates per region."""
    rng = random.Random(seed)
    C = [f"C{i:03d}" for i in range(n_candidates)]
    quality = {c: rng.randint(0, 1000) for c in C}
    regions = {}
    for ri in range(n_regions):
        s = {c: quality[c] + rng.randint(-noise, noise) for c in C}
        low = min(s.values())
        high = max(s.values())
        order = sorted(C, key=lambda c: (-s[c], int(c[1:])))
        pref = order[:prefix_len]
        tail = s[pref[-1]]
        regions[f"R{ri}"] = {
            "low": low, "high": high, "prefix": pref, "tail": tail, "verified": {}
        }
    weights = {f"R{ri}": rng.randint(1, 3) for ri in range(n_regions)}
    inst = {"candidates": C, "regions": regions, "weights": weights}

    # Sparse manual audit: a few legal cells.
    bnds = roster.validate(inst)
    for _ in range(4):
        r = rng.choice(list(regions))
        c = rng.choice(C)
        lo, hi = bnds[r][c]
        if lo < hi:
            regions[r].setdefault("verified", {})[c] = rng.randint(lo, hi)
    return inst


def check_hand_derived(inst):
    """Hand-derivable facts only: counts and forced prefix endpoints."""
    C = inst["candidates"]
    bnds = roster.validate(inst)
    print(f"candidates={len(C)} regions={len(inst['regions'])} "
          f"cells={len(C) * len(inst['regions'])}")
    for r, reg in inst["regions"].items():
        pref = reg["prefix"]
        last = pref[-1]
        # The last published candidate is pinned exactly at the tail.
        assert bnds[r][last] == (reg["tail"], reg["tail"])
        for i in range(len(pref) - 1):
            a, b = pref[i], pref[i + 1]
            if int(a[1:]) > int(b[1:]):
                # descending ids: the board forces s_a >= s_b + 1
                assert bnds[r][a][0] >= bnds[r][b][0] + 1
                assert bnds[r][b][1] <= bnds[r][a][1] - 1
        for c in C:
            if c in pref:
                continue
            cap = reg["tail"] if int(c[1:]) > int(last[1:]) else reg["tail"] - 1
            lo, hi = bnds[r][c]
            assert hi <= cap
    print("forced prefix endpoints and unlisted caps hold by hand")


def main():
    inst = build()
    check_hand_derived(inst)
    C = inst["candidates"]
    best, worst, _, lo, hi = roster.rank_bounds(inst)
    K = 25
    must = [c for c in C if worst[c] <= K]
    cannot = [c for c in C if best[c] > K]
    may = [c for c in C if best[c] <= K < worst[c]]
    print(f"K={K}: must={len(must)} may={len(may)} cannot={len(cannot)}")
    for p in range(1, 6):
        holders = [c for c in C if best[c] == p and worst[c] == p]
        print(f"  position {p}: sure={holders}")
    # Hand-derived boundary fact: every candidate is inside the region bands,
    # so best <= worst <= |C| always holds, and must + may + cannot == |C|.
    assert len(must) + len(may) + len(cannot) == len(C)
    # Spot-check the extreme witnesses for the top three candidates only; the
    # rest is deliberately left unexpanded.
    for c in must[:2]:
        wb = roster.witness(inst, c, "best", None)
        ww = roster.witness(inst, c, "worst", None)
        assert wb["rank"] == best[c] and ww["rank"] == worst[c]
    print("spot-checked witnesses:", must[:2])

    out = os.path.join(os.path.dirname(__file__), "large_instance.json")
    with open(out, "w") as fh:
        json.dump(inst, fh)
    print("wrote", out)


if __name__ == "__main__":
    main()
