"""Test suite: uses plain unittest (pytest-style discovery also works)."""
import itertools
import json
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import roster  # noqa: E402


# ---------------------------------------------------------------------------
# independent brute-force model (deliberately naive)
# ---------------------------------------------------------------------------
def _legal(reg, C, s):
    low, high = reg["low"], reg["high"]
    pref = reg.get("prefix") or []
    tail = reg.get("tail")
    for c in C:
        if not (low <= s[c] <= high):
            return False
    for c, v in (reg.get("verified") or {}).items():
        if s[c] != v:
            return False
    if pref:
        if s[pref[-1]] != tail:
            return False
        for i in range(len(pref) - 1):
            a, b = pref[i], pref[i + 1]
            if s[a] < s[b] or (s[a] == s[b] and a >= b):
                return False
        for c in C:
            if c in pref:
                continue
            if s[c] > tail or (s[c] == tail and c <= pref[-1]):
                return False
    return True


def _region_rows(reg, C):
    for vals in itertools.product(range(reg["low"], reg["high"] + 1), repeat=len(C)):
        s = dict(zip(C, vals))
        if _legal(reg, C, s):
            yield s


def brute_ranks(inst):
    C = inst["candidates"]
    regions = inst["regions"]
    per = {r: list(_region_rows(reg, C)) for r, reg in regions.items()}
    best = {c: 10**9 for c in C}
    worst = {c: -1 for c in C}
    for combo in itertools.product(*per.values()):
        scores = {
            c: sum(inst["weights"][r] * row[c] for r, row in zip(regions, combo))
            for c in C
        }
        order = sorted(C, key=lambda c: (-scores[c], c))
        for c in C:
            k = order.index(c) + 1
            best[c] = min(best[c], k)
            worst[c] = max(worst[c], k)
    return best, worst


def random_instance(rng):
    n = rng.randint(1, 4)
    C = [f"C{i:02d}" for i in range(n)]
    regions = {}
    for ri in range(rng.randint(1, 2)):
        low = rng.randint(0, 2)
        high = low + rng.randint(0, 3)
        pref, tail, ver = [], None, {}
        if rng.random() < 0.8:
            pref = rng.sample(C, rng.randint(1, n))
            tail = rng.randint(low, high)
            if roster._pref_floor(pref)[-1] > high - tail:
                pref, tail = [], None
            else:
                D = roster._pref_floor(pref)
                for c in rng.sample(C, rng.randint(0, max(1, n // 3))):
                    if c in pref:
                        j = pref.index(c)
                        lo = tail + (D[-1] - D[j])
                        if lo <= high:
                            ver[c] = rng.randint(lo, high)
                    else:
                        cap = tail if c > pref[-1] else tail - 1
                        if low <= cap:
                            ver[c] = rng.randint(low, cap)
        regions[f"R{ri}"] = {
            "low": low, "high": high, "prefix": pref, "tail": tail, "verified": ver
        }
    return {
        "candidates": C,
        "regions": regions,
        "weights": {r: rng.randint(0, 3) for r in regions},
    }


# ---------------------------------------------------------------------------
class TestRegionLevel(unittest.TestCase):
    def test_cell_bounds_and_pair_diffs_match_enumeration(self):
        rng = random.Random(1)
        checked = 0
        for _ in range(3000):
            n = rng.randint(1, 4)
            C = [f"C{i:02d}" for i in range(n)]
            low = rng.randint(0, 3)
            high = low + rng.randint(0, 4)
            pref = rng.sample(C, rng.randint(1, n)) if rng.random() < 0.8 else []
            tail = rng.randint(low, high) if pref else None
            ver = {}
            for c in C:
                if rng.random() < 0.25:
                    ver[c] = rng.randint(low - 1, high + 1)
            reg = {"low": low, "high": high, "prefix": pref, "tail": tail,
                   "verified": ver}
            rows = list(_region_rows(reg, C)) if low <= high else []
            try:
                bounds = roster.cell_bounds(reg, C)
                accepted = True
            except roster.Contradiction:
                accepted = False
            self.assertEqual(accepted, bool(rows), reg)
            if not accepted:
                continue
            checked += 1
            expected = {
                c: (min(r[c] for r in rows), max(r[c] for r in rows)) for c in C
            }
            self.assertEqual(bounds, expected, reg)
            for d in C:
                for c in C:
                    if c == d:
                        continue
                    lo = min(r[d] - r[c] for r in rows)
                    hi = max(r[d] - r[c] for r in rows)
                    self.assertEqual(roster.pair_diff(reg, C, d, c, bounds), (lo, hi))
        self.assertGreater(checked, 500)

    def test_witness_extremes_reach_bounds(self):
        rng = random.Random(2)
        for _ in range(2000):
            inst = random_instance(rng)
            try:
                bnds = roster.validate(inst)
            except roster.Contradiction:
                continue
            for r, reg in inst["regions"].items():
                for c in inst["candidates"]:
                    for mode, idx in (("high", 1), ("low", 0)):
                        row = roster.region_witness(reg, inst["candidates"], mode, c)
                        self.assertTrue(_legal(reg, inst["candidates"], row))
                        self.assertEqual(row[c], bnds[r][c][idx])


class TestRankBounds(unittest.TestCase):
    def test_fast_matches_reference_and_brute(self):
        rng = random.Random(3)
        checked = 0
        for _ in range(4000):
            inst = random_instance(rng)
            size = 1
            for reg in inst["regions"].values():
                size *= (reg["high"] - reg["low"] + 1) ** len(inst["candidates"])
            if size > 40000:
                continue
            try:
                bnds = roster.validate(inst)
            except roster.Contradiction:
                continue
            per = {r: list(_region_rows(reg, inst["candidates"]))
                   for r, reg in inst["regions"].items()}
            if any(not rows for rows in per.values()):
                continue
            fast = roster.rank_bounds(inst, bnds)
            ref = roster.rank_bounds_reference(inst, bnds)
            brute = brute_ranks(inst)
            self.assertEqual((fast[0], fast[1]), brute, inst)
            self.assertEqual((ref[0], ref[1]), brute, inst)
            for c in inst["candidates"]:
                wb = roster.witness(inst, c, "best", bnds)
                ww = roster.witness(inst, c, "worst", bnds)
                self.assertEqual(wb["rank"], brute[0][c])
                self.assertEqual(ww["rank"], brute[1][c])
            checked += 1
        self.assertGreater(checked, 300)

    def test_larger_random_fast_matches_reference(self):
        rng = random.Random(4)
        for _ in range(300):
            n = rng.randint(5, 12)
            C = [f"C{i:02d}" for i in range(n)]
            regions = {}
            for ri in range(rng.randint(1, 4)):
                low = rng.randint(0, 3)
                high = low + rng.randint(1, 6)
                pref = rng.sample(C, rng.randint(1, n))
                tail = rng.randint(low, high)
                D = roster._pref_floor(pref)
                if D[-1] > high - tail:
                    pref, tail = [], None
                regions[f"R{ri}"] = {
                    "low": low, "high": high, "prefix": pref, "tail": tail,
                    "verified": {},
                }
            inst = {
                "candidates": C,
                "regions": regions,
                "weights": {r: rng.randint(0, 4) for r in regions},
            }
            try:
                bnds = roster.validate(inst)
            except roster.Contradiction:
                continue
            self.assertEqual(
                roster.rank_bounds(inst, bnds)[:2],
                roster.rank_bounds_reference(inst, bnds)[:2],
            )


class TestWitnessContract(unittest.TestCase):
    def test_witness_is_self_contained_and_recomputable(self):
        rng = random.Random(5)
        for _ in range(200):
            inst = random_instance(rng)
            try:
                bnds = roster.validate(inst)
            except roster.Contradiction:
                continue
            best, worst, _, _, _ = roster.rank_bounds(inst, bnds)
            c = rng.choice(inst["candidates"])
            for which, want in (("best", best[c]), ("worst", worst[c])):
                rec = roster.witness(inst, c, which, bnds)
                assignment = {
                    (r, x): v for r, row in rec["region_scores"].items()
                    for x, v in row.items()
                }
                # recompute from scratch: region legality, composite, rank
                for r, reg in inst["regions"].items():
                    row = {x: assignment[(r, x)] for x in inst["candidates"]}
                    self.assertTrue(_legal(reg, inst["candidates"], row))
                ranks, scores = roster.composite_ranks(inst, assignment)
                self.assertEqual(scores, rec["composite_scores"])
                self.assertEqual(ranks[c], want)
                self.assertEqual(rec["rank"], want)
                # the returned order really is sorted by (-score, id)
                self.assertEqual(
                    rec["order"],
                    sorted(inst["candidates"],
                           key=lambda x: (-scores[x], x)))

    def test_unknown_witness_candidate_rejected(self):
        inst = random_instance(random.Random(6))
        with self.assertRaises(roster.Contradiction):
            roster.certificate(inst, 1, ["CZ99"])


class TestCertificate(unittest.TestCase):
    def test_top_k_partition_and_positions(self):
        rng = random.Random(7)
        for _ in range(300):
            inst = random_instance(rng)
            try:
                bnds = roster.validate(inst)
            except roster.Contradiction:
                continue
            C = inst["candidates"]
            K = rng.randint(0, len(C))
            cert = roster.certificate(inst, K)
            best, worst, _, _, _ = roster.rank_bounds(inst, bnds)
            tk = cert["top_k"]
            # partition
            parts = (set(tk["must_be_in_top_k"]), set(tk["may_be_in_top_k"]),
                     set(tk["cannot_be_in_top_k"]))
            self.assertEqual(set().union(*parts), set(C))
            self.assertFalse(parts[0] & parts[1] or parts[0] & parts[2]
                             or parts[1] & parts[2])
            self.assertEqual(tk["must_be_in_top_k"],
                             sorted([c for c in C if worst[c] <= K], key=str))
            self.assertEqual(tk["cannot_be_in_top_k"],
                             sorted([c for c in C if best[c] > K], key=str))
            # positions
            self.assertEqual(len(tk["positions"]), K)
            for p in range(1, K + 1):
                entry = tk["positions"][p - 1]
                holders = [c for c in C if best[c] == p and worst[c] == p]
                self.assertEqual(entry["candidates_forced_to_this_position"],
                                 sorted(holders, key=str))
                self.assertEqual(entry["certain_holder"],
                                 holders[0] if len(holders) == 1 else None)
            # every forced holder is necessarily in the top K
            forced = {c for p in tk["positions"]
                      for c in p["candidates_forced_to_this_position"]}
            self.assertTrue(forced <= set(tk["must_be_in_top_k"]))

    def test_no_cross_region_enumeration_needed_for_prefix_only(self):
        # Big roster, short prefixes: the fast path stays linear-ish.
        n = 3000
        C = [f"C{i:04d}" for i in range(n)]
        regions = {
            "A": {"low": 0, "high": 9, "prefix": C[:2], "tail": 8, "verified": {}},
            "B": {"low": 0, "high": 9, "prefix": C[2:4], "tail": 8, "verified": {}},
        }
        inst = {"candidates": C, "regions": regions, "weights": {"A": 1, "B": 1}}
        best, worst, _, _, _ = roster.rank_bounds(inst)
        self.assertEqual(best[C[0]], 1)
        self.assertEqual(worst[C[-1]], n)


class TestRejections(unittest.TestCase):
    def _reject(self, inst):
        with self.assertRaises(roster.Contradiction):
            roster.certificate(inst, 1)

    def test_structural_contradictions(self):
        C = ["C01", "C02"]
        self._reject({"candidates": C, "regions": {"R": {
            "low": 1, "high": 0, "prefix": [], "tail": None, "verified": {}}},
            "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": ["C01"], "tail": None, "verified": {}}},
            "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": [], "tail": 3, "verified": {}}},
            "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": ["C01", "C01"], "tail": 2, "verified": {}}},
            "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": ["C09"], "tail": 2, "verified": {}}},
            "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": [], "tail": None,
            "verified": {"C01": 9}}}, "weights": {"R": 1}})
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 5, "prefix": [], "tail": None, "verified": {}}},
            "weights": {"R": -1}})

    def test_lower_bound_already_on_tail(self):
        # ids C02 -> C01 descend, so the prefix needs a strict step; [4,4] and
        # tail 4 leave no room.  This is the "valid lower bound already at the
        # tail" control case from the brief.
        inst = {"candidates": ["C01", "C02", "C03"], "regions": {"R0": {
            "low": 4, "high": 4, "prefix": ["C02", "C01"], "tail": 4,
            "verified": {}}}, "weights": {"R0": 1}}
        self._reject(inst)

    def test_verified_vs_prefix_and_tail(self):
        C = ["C01", "C02", "C03"]
        # verified cell sits below the floor its prefix position needs
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 9, "prefix": ["C03", "C02", "C01"], "tail": 2,
            "verified": {"C03": 3}}}, "weights": {"R": 1}})
        # unlisted cell tied with the tail but with a lower id
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 9, "prefix": ["C02"], "tail": 3,
            "verified": {"C00": 3}}}, "weights": {"R": 1}})
        # verified contradicts the last published score
        self._reject({"candidates": C, "regions": {"R": {
            "low": 0, "high": 9, "prefix": ["C02"], "tail": 3,
            "verified": {"C02": 4}}}, "weights": {"R": 1}})

    def test_feasible_neighbour_is_accepted(self):
        inst = {"candidates": ["C01", "C02", "C03"], "regions": {"R0": {
            "low": 4, "high": 5, "prefix": ["C02", "C01"], "tail": 4,
            "verified": {}}}, "weights": {"R0": 1}}
        bounds = roster.validate(inst)
        self.assertEqual(bounds["R0"], {"C01": (4, 4), "C02": (5, 5), "C03": (4, 4)})


class TestExamples(unittest.TestCase):
    def test_examples_run(self):
        import runpy
        for name in ("tail_tie", "verified_evidence", "interval_overlap",
                     "illegal_lower_bound"):
            runpy.run_path(
                os.path.join(HERE, "..", "examples", name + ".py"),
                run_name="__main__")


if __name__ == "__main__":
    unittest.main()


class TestScale(unittest.TestCase):
    def test_short_prefixes_scale_without_pairwise(self):
        import time
        n = 6000
        C = [f"C{i:05d}" for i in range(n)]
        regions = {
            "A": {"low": 0, "high": 9, "prefix": C[:3], "tail": 8, "verified": {}},
            "B": {"low": 0, "high": 9, "prefix": C[3:6], "tail": 8, "verified": {}},
        }
        inst = {"candidates": C, "regions": regions, "weights": {"A": 1, "B": 1}}
        t0 = time.time()
        best, worst, _, _, _ = roster.rank_bounds(inst)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 10.0)
        self.assertEqual(best[C[0]], 1)
        self.assertEqual(worst[C[-1]], n)

    def test_large_file_certificate(self):
        path = os.path.join(HERE, "..", "large", "large_instance.json")
        if not os.path.exists(path):
            self.skipTest("large instance not built")
        with open(path) as fh:
            inst = json.load(fh)
        cert = roster.certificate(inst, 25, ["C003"])
        self.assertEqual(len(cert["per_candidate"]), len(inst["candidates"]))
        tk = cert["top_k"]
        self.assertEqual(
            len(tk["must_be_in_top_k"]) + len(tk["may_be_in_top_k"])
            + len(tk["cannot_be_in_top_k"]), len(inst["candidates"]))
        rec = cert["witnesses"]["C003"]
        self.assertEqual(rec["best"]["rank"],
                         cert["per_candidate"]["C003"]["best_rank"])
        self.assertEqual(rec["worst"]["rank"],
                         cert["per_candidate"]["C003"]["worst_rank"])


class TestIdOrdering(unittest.TestCase):
    def test_code_string_order_is_the_tie_break(self):
        # Project codes compare as strings, ascending -- exactly the published
        # board rule.  No numeric interpretation is imposed on ids.
        ids = ["C09", "C10", "C2"]
        self.assertEqual(sorted(ids, key=roster._code), ["C09", "C10", "C2"])
        inst = {
            "candidates": ids,
            "regions": {"R": {"low": 0, "high": 2, "prefix": ["C10"], "tail": 2,
                              "verified": {}}},
            "weights": {"R": 1},
        }
        bounds = roster.validate(inst)
        # C10 holds the tail; "C09" sorts before "C10" so it cannot tie the
        # tail, while "C2" sorts after "C10" and may.
        self.assertEqual(bounds["R"]["C10"], (2, 2))
        self.assertEqual(bounds["R"]["C09"], (0, 1))
        self.assertEqual(bounds["R"]["C2"], (0, 2))

    def test_fixed_width_ids_tie_break(self):
        inst = {
            "candidates": ["C01", "C02"],
            "regions": {"R": {"low": 0, "high": 1, "prefix": ["C02"], "tail": 1,
                              "verified": {}}},
            "weights": {"R": 1},
        }
        bounds = roster.validate(inst)
        self.assertEqual(bounds["R"]["C02"], (1, 1))
        self.assertEqual(bounds["R"]["C01"], (0, 0))


class TestScale(unittest.TestCase):
    def test_short_prefixes_scale_without_pairwise(self):
        import time
        n = 6000
        C = [f"C{i:05d}" for i in range(n)]
        regions = {
            "A": {"low": 0, "high": 9, "prefix": C[:3], "tail": 8, "verified": {}},
            "B": {"low": 0, "high": 9, "prefix": C[3:6], "tail": 8, "verified": {}},
        }
        inst = {"candidates": C, "regions": regions, "weights": {"A": 1, "B": 1}}
        t0 = time.time()
        best, worst, _, _, _ = roster.rank_bounds(inst)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 10.0)
        self.assertEqual(best[C[0]], 1)
        self.assertEqual(worst[C[-1]], n)

    def test_large_file_certificate(self):
        path = os.path.join(HERE, "..", "large", "large_instance.json")
        if not os.path.exists(path):
            self.skipTest("large instance not built")
        with open(path) as fh:
            inst = json.load(fh)
        cert = roster.certificate(inst, 25, ["C003"])
        self.assertEqual(len(cert["per_candidate"]), len(inst["candidates"]))
        tk = cert["top_k"]
        self.assertEqual(
            len(tk["must_be_in_top_k"]) + len(tk["may_be_in_top_k"])
            + len(tk["cannot_be_in_top_k"]), len(inst["candidates"]))
        rec = cert["witnesses"]["C003"]
        self.assertEqual(rec["best"]["rank"],
                         cert["per_candidate"]["C003"]["best_rank"])
        self.assertEqual(rec["worst"]["rank"],
                         cert["per_candidate"]["C003"]["worst_rank"])


if __name__ == "__main__":
    unittest.main()
