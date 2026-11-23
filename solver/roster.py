"""Rank certificates over a roster of truncated regional leaderboards.

Model (all constraints are stated in the task):

* The candidate set C is known in full.
* Every region r scores every candidate with one integer s[r][c] in
  [lo_r, hi_r].  A region may publish an ordered prefix of m_r candidates
  (descending score, ties by ascending project code) and a tail score t_r equal
  to the last published score.  Candidates outside the prefix score <= t_r;
  a score equal to t_r is legal only when the candidate's id sorts after the
  last published id (that is exactly the leaderboard tie rule).  A region with
  no published prefix carries no tail constraint at all.
* A sparse set of manually verified region scores may pin cells.  Verified
  cells must agree with the published prefix/tail, otherwise the whole request
  is rejected.
* Composite score S_c = sum_r w_r s[r][c] with non-negative integer weights;
  composite ties break by ascending project code.

Structure used for the certificate
-----------------------------------
All region constraints are *independent*: no region pairs, quotas or shared
budgets.  Therefore:

1. Every cell has an exact feasible interval (``cell_bounds``).
2. When two candidates share a published prefix their *difference* is coupled
   by the mandatory strict steps of the prefix.  Everything else couples only
   through the two marginal intervals.
3. A candidate can be pushed to its regional maximum while every other
   candidate is pushed to the conditional minimum (and symmetrically for the
   minimum), so the per-pair difference extremes are attained simultaneously.
   That makes the rank extremes exact and makes a single constructed assignment
   a witness for each extreme.

Complexity
----------
Bounds: O(|C| + |prefix| + |verified|) per region.
Rank extremes: the marginal part is counted with sorted arrays (binary
search), and only pairs sharing a prefix are corrected individually.  Total
O(|C| log |C| + sum_r m_r^2) -- never |C|^2 unless the published prefixes are
that long.  No score matrix is materialised; witnesses are built on demand.
"""

from __future__ import annotations

import bisect
import json
from typing import Dict, List, Optional, Tuple


class Contradiction(Exception):
    """Region material is inconsistent; the request must be rejected."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _code(c: str) -> str:
    """The tie-break key: project codes compare as strings, ascending.

    The published boards are ordered by descending score and, on equal score,
    by ascending project code; composite ties use the same ascending code.  A
    plain string comparison is exactly that rule for any code scheme, so no
    numeric parsing is imposed on the ids.
    """
    return c


def _pref_floor(pref: List[str]) -> List[int]:
    """D[j]: strict steps forced between prefix position 0 and position j.

    Position i must satisfy s_i >= s_{i+1} + d_i, with d_i = 1 exactly when the
    codes descend (equal scores need ascending codes).
    """
    ids = [_code(c) for c in pref]
    D = [0] * len(pref)
    for i in range(1, len(pref)):
        D[i] = D[i - 1] + (1 if ids[i - 1] > ids[i] else 0)
    return D


# ---------------------------------------------------------------------------
# region level
# ---------------------------------------------------------------------------
def check_region(reg: dict, C: List[str]) -> None:
    """Validate one region or raise :class:`Contradiction`."""
    low, high = reg["low"], reg["high"]
    pref = reg.get("prefix") or []
    tail = reg.get("tail")
    ver = reg.get("verified") or {}
    if not isinstance(low, int) or not isinstance(high, int):
        raise Contradiction("region bounds must be integers")
    if low > high:
        raise Contradiction(f"empty score range [{low},{high}]")
    for c, v in ver.items():
        if c not in C:
            raise Contradiction(f"verified score for unknown candidate {c}")
        if not isinstance(v, int) or not (low <= v <= high):
            raise Contradiction(f"verified score {v} for {c} outside [{low},{high}]")
    if not pref:
        if tail is not None:
            raise Contradiction("tail score supplied without a published prefix")
        return
    if tail is None:
        raise Contradiction("published prefix without a tail score")
    if not isinstance(tail, int) or not (low <= tail <= high):
        raise Contradiction(f"tail {tail} outside [{low},{high}]")
    if len(set(pref)) != len(pref):
        raise Contradiction("duplicate candidate in published prefix")
    for c in pref:
        if c not in C:
            raise Contradiction(f"unknown candidate {c} in prefix")
    D = _pref_floor(pref)
    # The top of the prefix needs tail + D[-1] <= high; the tail itself already
    # sits inside [low, high] (checked above), so no lower-bound issue remains.
    if tail + D[-1] > high:
        raise Contradiction(
            f"tail {tail} leaves no room for the {D[-1]} strict step(s) "
            f"forced by the prefix below the region ceiling {high}")
    last = pref[-1]
    if last in ver and ver[last] != tail:
        raise Contradiction(
            f"verified {last}={ver[last]} contradicts the tail score {tail}, "
            f"which is the score of the last published candidate")
    for c, v in ver.items():
        if c in pref:
            i = pref.index(c)
            floor = tail + (D[-1] - D[i])
            if v < floor:
                raise Contradiction(
                    f"verified {c}={v} contradicts its prefix position "
                    f"(needs >= {floor} to stay above tail {tail})")
        else:
            cap = tail if _code(c) > _code(last) else tail - 1
            if v > cap:
                raise Contradiction(
                    f"verified {c}={v} contradicts the tail {tail} and the "
                    f"tie rule (cap {cap})")
    # Structural checks pass; also require that every cell can actually be
    # filled (e.g. an unlisted candidate whose cap falls below the floor).
    _cell_bounds_unchecked(reg, C)


def _check_fixed_chain(fixed: Dict[int, int], D: List[int]) -> None:
    """Every pair of pinned prefix positions must respect the forced steps."""
    items = sorted(fixed.items())
    for a in range(len(items)):
        i, fi = items[a]
        for b in range(a + 1, len(items)):
            j, fj = items[b]
            if fi < fj + (D[j] - D[i]):
                raise Contradiction(
                    f"fixed prefix scores at positions {i} and {j} violate the "
                    f"published order (need {fi} >= {fj + (D[j] - D[i])})")


def cell_bounds(reg: dict, C: List[str]) -> Dict[str, Tuple[int, int]]:
    """Exact feasible [min, max] score per candidate in one region."""
    check_region(reg, C)
    return _cell_bounds_unchecked(reg, C)


def _cell_bounds_unchecked(reg: dict, C: List[str]) -> Dict[str, Tuple[int, int]]:
    low, high = reg["low"], reg["high"]
    pref = reg.get("prefix") or []
    tail = reg.get("tail")
    ver = reg.get("verified") or {}
    out: Dict[str, Tuple[int, int]] = {}

    if not pref:
        for c in C:
            out[c] = (ver[c], ver[c]) if c in ver else (low, high)
        return out

    D = _pref_floor(pref)
    m = len(pref)
    pos = {c: i for i, c in enumerate(pref)}
    fixed = {m - 1: tail}
    for c, v in ver.items():
        if c in pos:
            if pos[c] in fixed and fixed[pos[c]] != v:
                raise Contradiction(
                    f"verified {c}={v} conflicts the tail score {tail}")
            fixed[pos[c]] = v
    _check_fixed_chain(fixed, D)

    for c in C:
        if c in pos:
            j = pos[c]
            lo = low + (D[m - 1] - D[j])
            hi = high - D[j]
            for k, fk in fixed.items():
                if k > j:
                    lo = max(lo, fk + (D[k] - D[j]))
                elif k < j:
                    hi = min(hi, fk - (D[j] - D[k]))
            if j in fixed:
                lo = hi = fixed[j]
            if lo > hi:
                raise Contradiction(f"no legal score for prefix entry {c}")
            out[c] = (lo, hi)
        else:
            cap = tail if _code(c) > _code(pref[-1]) else tail - 1
            lo, hi = low, min(high, cap)
            if c in ver:
                lo = hi = ver[c]
            if lo > hi:
                raise Contradiction(f"no legal score for unlisted {c}")
            out[c] = (lo, hi)
    return out


def _prefix_bounds(reg: dict, extra_fixed: Optional[Dict[int, int]] = None):
    """Per prefix position [min, max] score after propagating the chain."""
    pref = reg["prefix"]
    low, high = reg["low"], reg["high"]
    tail = reg["tail"]
    ver = reg.get("verified") or {}
    D = _pref_floor(pref)
    m = len(pref)
    pos = {c: i for i, c in enumerate(pref)}
    fixed = {m - 1: tail}
    for c, v in ver.items():
        if c in pos:
            fixed[pos[c]] = v
    for j, v in (extra_fixed or {}).items():
        if j in fixed and fixed[j] != v:
            raise Contradiction("pinned prefix cell conflicts region material")
        fixed[j] = v
    lb, ub = {}, {}
    for j in range(m):
        lb[j] = low + (D[m - 1] - D[j])
        ub[j] = high - D[j]
        for k, fk in fixed.items():
            if k > j:
                lb[j] = max(lb[j], fk + (D[k] - D[j]))
            elif k < j:
                ub[j] = min(ub[j], fk - (D[j] - D[k]))
        if j in fixed:
            lb[j] = ub[j] = fixed[j]
        if lb[j] > ub[j]:
            raise Contradiction(f"prefix position {j} cannot be filled")
    return lb, ub, fixed, D


def _prefix_values(reg: dict, pin: Optional[Tuple[int, int]] = None,
                   direction: str = "low") -> Dict[str, int]:
    """Legal prefix scores with the unpinned positions pushed to one side.

    ``direction='low'``  : unpinned positions as small as possible (keep the
    prefix cheap); ``direction='high'`` : as large as possible (push rivals up
    while the pinned candidate stays put).
    """
    pref = reg["prefix"]
    m = len(pref)
    extra = {pin[0]: pin[1]} if pin else None
    lb, ub, fixed, D = _prefix_bounds(reg, extra)
    vals = [0] * m
    if direction == "low":
        # last position first, then walk upwards: s_j >= s_{j+1} + d_j
        for j in range(m - 1, -1, -1):
            if j in fixed:
                vals[j] = fixed[j]
            elif j == m - 1:
                vals[j] = lb[j]
            else:
                d = 1 if _code(pref[j]) > _code(pref[j + 1]) else 0
                vals[j] = max(lb[j], vals[j + 1] + d)
            if vals[j] < lb[j] or vals[j] > ub[j]:
                raise Contradiction("cannot fill prefix from below")
    elif direction == "high":
        # first position first, then walk downwards: s_j <= s_{j-1} - d_{j-1}
        for j in range(m):
            if j in fixed:
                vals[j] = fixed[j]
            elif j == 0:
                vals[j] = ub[j]
            else:
                d = 1 if _code(pref[j - 1]) > _code(pref[j]) else 0
                vals[j] = min(ub[j], vals[j - 1] - d)
            if vals[j] < lb[j] or vals[j] > ub[j]:
                raise Contradiction("cannot fill prefix from above")
    else:
        raise ValueError(direction)
    for j in range(m - 1):
        a, b = pref[j], pref[j + 1]
        if vals[j] < vals[j + 1] + (1 if _code(a) > _code(b) else 0):
            raise Contradiction("constructed prefix violates the ranking order")
    return {pref[j]: vals[j] for j in range(m)}


def region_witness(reg: dict, C: List[str], mode: str, c: str) -> Dict[str, int]:
    """Full legal region assignment with ``c`` at one extreme.

    ``mode='high'`` puts c at its maximum and every other candidate at the
    conditional minimum; ``mode='low'`` is the mirror image.  The construction
    is direct (no search) and stays legal by construction of the prefix floors.
    """
    b = cell_bounds(reg, C)
    pref = reg.get("prefix") or []
    ver = reg.get("verified") or {}
    low, high = reg["low"], reg["high"]
    want = b[c][1] if mode == "high" else b[c][0]
    pin = (pref.index(c), want) if (pref and c in pref) else None
    vals: Dict[str, int] = {}
    if pref:
        # push everybody else away from the pinned candidate
        vals.update(_prefix_values(reg, pin, "low" if mode == "high" else "high"))
    for x in C:
        if x in vals:
            continue
        if x == c:
            vals[x] = want
        elif x in ver:
            vals[x] = ver[x]
        elif not pref:
            vals[x] = low if mode == "high" else high
        else:
            cap = reg["tail"] if _code(x) > _code(pref[-1]) else reg["tail"] - 1
            vals[x] = low if mode == "high" else min(high, cap)
    return vals


def pair_diff(reg: dict, C: List[str], d: str, c: str,
              b: Dict[str, Tuple[int, int]]) -> Tuple[int, int]:
    """Exact [min, max] of ``s_d - s_c`` attainable in this region."""
    pref = reg.get("prefix") or []
    lc, hc = b[c]
    ld, hd = b[d]
    if not pref:
        return ld - hc, hd - lc
    D = _pref_floor(pref)
    pos = {x: i for i, x in enumerate(pref)}
    if d in pos and c in pos:
        p, q = pos[d], pos[c]
        if p < q:                       # d published above c
            gap = D[q] - D[p]
            return max(gap, ld - hc), hd - lc
        gap = D[p] - D[q]               # d published below c
        return ld - hc, min(-gap, hd - lc)
    return ld - hc, hd - lc             # at most one sits in the prefix


# ---------------------------------------------------------------------------
# instance level
# ---------------------------------------------------------------------------
def validate(inst: dict) -> Dict[str, Dict[str, Tuple[int, int]]]:
    C = inst["candidates"]
    if len(set(C)) != len(C):
        raise Contradiction("duplicate candidate id")
    for c in C:
        _code(c)
    regions = inst["regions"]
    weights = inst["weights"]
    for r in regions:
        if r not in weights:
            raise Contradiction(f"missing weight for region {r}")
        w = weights[r]
        if not isinstance(w, int) or w < 0:
            raise Contradiction(f"weight for {r} must be a non-negative integer")
    extra = set(weights) - set(regions)
    if extra:
        raise Contradiction(f"weights for unknown regions: {sorted(extra)}")
    return {r: cell_bounds(reg, C) for r, reg in regions.items()}


def _composite_bounds(inst, bnds):
    C = inst["candidates"]
    W = inst["weights"]
    R = list(inst["regions"])
    lo = {c: sum(W[r] * bnds[r][c][0] for r in R) for c in C}
    hi = {c: sum(W[r] * bnds[r][c][1] for r in R) for c in C}
    return lo, hi


def _pair_slack(inst, bnds):
    """Per ordered pair corrections caused by shared published prefixes.

    ``mn[(d, c)]`` is added to the marginal minimum of S_d - S_c and
    ``mx[(d, c)]`` to the marginal maximum.  Only pairs that share a prefix in
    some region appear here.
    """
    C = inst["candidates"]
    W = inst["weights"]
    mn: Dict[Tuple[str, str], int] = {}
    mx: Dict[Tuple[str, str], int] = {}

    def bump(table, key, val):
        if val:
            table[key] = table.get(key, 0) + val

    for r, reg in inst["regions"].items():
        w = W[r]
        pref = reg.get("prefix") or []
        if w == 0 or len(pref) < 2:
            continue
        b = bnds[r]
        D = _pref_floor(pref)
        for i in range(len(pref)):
            di = pref[i]
            for j in range(i + 1, len(pref)):
                cj = pref[j]
                gap = D[j] - D[i]
                base_min = b[di][0] - b[cj][1]
                base_max = b[cj][1] - b[di][0]
                actual_min = max(gap, base_min)
                actual_max = min(-gap, base_max)
                bump(mn, (di, cj), w * (actual_min - base_min))
                bump(mx, (cj, di), w * (actual_max - base_max))
    return mn, mx


def _base_counts(lo, hi, C):
    """Marginal counts for best/worst rank, via sorted arrays.

    best_base[c] = 1 + #{d : Lo_d > Hi_c} + #{d : Lo_d == Hi_c, id_d < id_c}
    worst_base[c] = 1 + #{d : Hi_d > Lo_c} + #{d : Hi_d == Lo_c, id_d < id_c}
    """
    lo_vals = sorted(lo.values())
    hi_vals = sorted(hi.values())
    lo_groups: Dict[int, List[int]] = {}
    hi_groups: Dict[int, List[int]] = {}
    for c in C:
        lo_groups.setdefault(lo[c], []).append(_code(c))
        hi_groups.setdefault(hi[c], []).append(_code(c))
    for v in lo_groups.values():
        v.sort()
    for v in hi_groups.values():
        v.sort()

    best, worst = {}, {}
    for c in C:
        n = len(C)
        ldc, hic = lo[c], hi[c]
        # Strictly above: Lo_d > Hi_c.  c itself cannot be in this bucket
        # because Lo_c <= Hi_c.
        gt = n - bisect.bisect_right(lo_vals, hic)
        eq = lo_groups.get(hic)
        tie = bisect.bisect_left(eq, _code(c)) if eq else 0
        best[c] = 1 + gt + tie

        gt = n - bisect.bisect_right(hi_vals, ldc)
        if hi[c] > ldc:          # the candidate itself is in the '>' bucket
            gt -= 1
        eq = hi_groups.get(ldc)
        tie = bisect.bisect_left(eq, _code(c)) if eq else 0
        worst[c] = 1 + gt + tie
    return best, worst


def rank_bounds(inst, bnds=None):
    """Exact (best, worst) rank per candidate.

    Fast path: O(|C| log |C| + sum m_r^2).  ``rank_bounds_reference`` is the
    straightforward O(|C|^2 * |R|) version used by the tests.
    """
    C = inst["candidates"]
    if bnds is None:
        bnds = validate(inst)
    lo, hi = _composite_bounds(inst, bnds)
    best, worst = _base_counts(lo, hi, C)
    mn_corr, mx_corr = _pair_slack(inst, bnds)

    def ahead(base, d, c):
        # d is ahead of c when its lead is positive, or zero and its id is
        # smaller (composite ties break by ascending code).
        return base > 0 or (base == 0 and _code(d) < _code(c))

    for (d, c), corr in mn_corr.items():
        base = lo[d] - hi[c]
        if ahead(base, d, c) != ahead(base + corr, d, c):
            best[c] += 1 if ahead(base + corr, d, c) else -1
    for (d, c), corr in mx_corr.items():
        base = hi[d] - lo[c]
        if ahead(base, d, c) != ahead(base + corr, d, c):
            worst[c] += 1 if ahead(base + corr, d, c) else -1
    return best, worst, bnds, lo, hi


def rank_bounds_reference(inst, bnds=None):
    """O(|C|^2 * |R|) reference implementation (used for testing)."""
    C = inst["candidates"]
    if bnds is None:
        bnds = validate(inst)
    lo, hi = _composite_bounds(inst, bnds)
    best, worst = {}, {}
    for c in C:
        b = w = 1
        for d in C:
            if d == c:
                continue
            mn = mx = 0
            for r, reg in inst["regions"].items():
                weight = inst["weights"][r]
                if weight == 0:
                    continue
                a, z = pair_diff(reg, C, d, c, bnds[r])
                mn += weight * a
                mx += weight * z
            if mn > 0 or (mn == 0 and _code(d) < _code(c)):
                b += 1
            if mx > 0 or (mx == 0 and _code(d) < _code(c)):
                w += 1
        best[c], worst[c] = b, w
    return best, worst, bnds, lo, hi


# ---------------------------------------------------------------------------
# witnesses and certificate
# ---------------------------------------------------------------------------
def composite_ranks(inst, assignment: Dict[Tuple[str, str], int]) -> Dict[str, int]:
    """Recompute every composite score and rank from a full assignment."""
    C = inst["candidates"]
    W = inst["weights"]
    scores = {c: sum(W[r] * assignment[(r, c)] for r in inst["regions"]) for c in C}
    order = sorted(C, key=lambda c: (-scores[c], _code(c)))
    return {c: order.index(c) + 1 for c in C}, scores


def witness(inst, c: str, which: str, bnds=None):
    """Complete legal assignment where ``c`` attains its best/worst rank.

    Returns a self-contained record: every region score, the composite scores,
    the recomputed rank order and the recomputed rank of ``c``.
    """
    C = inst["candidates"]
    if bnds is None:
        bnds = validate(inst)
    if which not in ("best", "worst"):
        raise ValueError("which must be 'best' or 'worst'")
    assignment: Dict[Tuple[str, str], int] = {}
    mode = "high" if which == "best" else "low"
    for r, reg in inst["regions"].items():
        vals = region_witness(reg, C, mode, c)
        _assert_region_legal(reg, C, vals)
        for x in C:
            assignment[(r, x)] = vals[x]
    ranks, scores = composite_ranks(inst, assignment)
    order = sorted(C, key=lambda x: (-scores[x], _code(x)))
    return {
        "candidate": c,
        "extremum": which,
        "region_scores": {r: {x: assignment[(r, x)] for x in C} for r in inst["regions"]},
        "composite_scores": {x: scores[x] for x in C},
        "order": order,
        "rank": ranks[c],
    }


def _assert_region_legal(reg, C, vals):
    """Defensive re-check of a constructed region assignment."""
    low, high = reg["low"], reg["high"]
    pref = reg.get("prefix") or []
    tail = reg.get("tail")
    ver = reg.get("verified") or {}
    for x in C:
        if not (low <= vals[x] <= high):
            raise Contradiction(f"witness value for {x} out of region range")
    for x, v in ver.items():
        if vals[x] != v:
            raise Contradiction(f"witness disagrees with verified {x}={v}")
    if not pref:
        return
    if vals[pref[-1]] != tail:
        raise Contradiction("witness tail mismatch")
    for i in range(len(pref) - 1):
        a, b = pref[i], pref[i + 1]
        if vals[a] < vals[b] or (vals[a] == vals[b] and _code(a) > _code(b)):
            raise Contradiction("witness prefix order violated")
    for x in C:
        if x in pref:
            continue
        if vals[x] > tail or (vals[x] == tail and _code(x) < _code(pref[-1])):
            raise Contradiction("witness unlisted cell violates the tail rule")


def certificate(inst, K: int, witness_for: Optional[List[str]] = None) -> dict:
    """Build the rank certificate for one request."""
    C = inst["candidates"]
    bnds = validate(inst)
    best, worst, bnds, lo, hi = rank_bounds(inst, bnds)
    if not isinstance(K, int) or K < 0:
        raise Contradiction("K must be a non-negative integer")
    K = min(K, len(C))

    # One pass: a position is certain only when some candidate has both
    # extremes equal to it.  (Two candidates cannot share a rank, so at most
    # one candidate can ever be forced into a given position.)
    forced_at: Dict[int, str] = {}
    for c in C:
        if best[c] == worst[c] <= K:
            forced_at[best[c]] = c
    positions = [
        {
            "position": p,
            "certain_holder": forced_at.get(p),
            "candidates_forced_to_this_position":
                [forced_at[p]] if p in forced_at else [],
        }
        for p in range(1, K + 1)
    ]
    must_in = sorted([c for c in C if worst[c] <= K], key=_code)
    may_in = sorted([c for c in C if best[c] <= K < worst[c]], key=_code)
    cannot_in = sorted([c for c in C if best[c] > K], key=_code)

    cert = {
        "K": K,
        "candidate_count": len(C),
        "per_candidate": {
            c: {
                "best_rank": best[c],
                "worst_rank": worst[c],
                "composite_min_possible": lo[c],
                "composite_max_possible": hi[c],
            }
            for c in sorted(C, key=_code)
        },
        "top_k": {
            "positions": positions,
            "must_be_in_top_k": must_in,
            "may_be_in_top_k": may_in,
            "cannot_be_in_top_k": cannot_in,
        },
        "witnesses": {},
    }
    for c in (witness_for or []):
        if c not in best:
            raise Contradiction(f"witness requested for unknown candidate {c}")
        wb = witness(inst, c, "best", bnds)
        ww = witness(inst, c, "worst", bnds)
        if wb["rank"] != best[c] or ww["rank"] != worst[c]:
            raise AssertionError(
                f"constructed witness for {c} does not reach the bounds: "
                f"{wb['rank']}/{ww['rank']} vs {best[c]}/{worst[c]}")
        cert["witnesses"][c] = {
            "best": wb,
            "worst": ww,
        }
    return cert


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv=None):
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="roster rank certificate")
    ap.add_argument("instance", help="JSON instance file")
    ap.add_argument("--k", type=int, required=True, help="cut-off K")
    ap.add_argument("--witness", default="", help="comma separated candidate ids")
    ap.add_argument("--out", default="", help="write certificate to this file")
    args = ap.parse_args(argv)
    with open(args.instance) as fh:
        inst = json.load(fh)
    wanted = [c.strip() for c in args.witness.split(",") if c.strip()]
    try:
        cert = certificate(inst, args.k, wanted)
    except Contradiction as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(cert, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
