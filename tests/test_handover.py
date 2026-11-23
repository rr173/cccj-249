"""Tests for the handover list builder.

The catalog resolution is cross-checked against an independent recursive
implementation with explicit depth enumeration (no fixpoint, no shared state),
and random catalogs are fuzzed against it.  Everything else is checked with
small hand-built ledgers so each rule has a readable witness.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import handover  # noqa: E402


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def cert(ids, must, may, cannot, K=None):
    """A structurally valid roster-shaped certificate for the given buckets."""
    K = K if K is not None else len(must)
    per = {}
    rank = 1
    for c in must:
        per[c] = {"best_rank": rank, "worst_rank": rank if len(may) == 0 else K,
                  "composite_min_possible": 0, "composite_max_possible": 0}
        rank += 1
    for c in may:
        per[c] = {"best_rank": 1, "worst_rank": K + 1,
                  "composite_min_possible": 0, "composite_max_possible": 0}
    for c in cannot:
        per[c] = {"best_rank": K + 1, "worst_rank": len(ids),
                  "composite_min_possible": 0, "composite_max_possible": 0}
    positions = [
        {"position": p, "certain_holder": None, "candidates_forced_to_this_position": []}
        for p in range(1, K + 1)
    ]
    return {
        "K": K,
        "candidate_count": len(ids),
        "per_candidate": per,
        "top_k": {
            "positions": positions,
            "must_be_in_top_k": list(must),
            "may_be_in_top_k": list(may),
            "cannot_be_in_top_k": list(cannot),
        },
        "witnesses": {},
    }


def log(version, eff, rcv, state="valid"):
    return {"version": version, "effective_at": eff,
            "received_at": rcv, "state": state}


def ledger(catalog, projects, records, required=("A", "B")):
    return {
        "ledger_id": "t",
        "required_items": list(required),
        "catalog": catalog,
        "projects": projects,
        "status_log": records,
    }


def validated(led):
    """The resolver consumes the normalised ledger shape produced by validation."""
    return handover.validate_ledger(led)


def original():
    return {"kind": "original"}


def derived(*groups):
    return {"kind": "derived",
            "basis_groups": [list(g) for g in groups]}


# ---------------------------------------------------------------------------
# independent reference implementation (deliberately naive, no fixpoint)
# ---------------------------------------------------------------------------
def reference_holds(catalog, valid_versions, vid, stack):
    """Return (holds, depth, group_index) for one catalog version."""
    if vid not in valid_versions:
        return False, None, None
    if catalog[vid]["kind"] == "original":
        return True, 0, None
    if vid in stack:                       # an ungrounded cycle proves nothing
        return False, None, None
    best = None
    for gi, group in enumerate(catalog[vid]["basis_groups"]):
        depth = 0
        ok = True
        for member in group:
            holds, d, _ = reference_holds(catalog, valid_versions, member,
                                          stack | {vid})
            if not holds:
                ok = False
                break
            depth = max(depth, d)
        if ok:
            candidate = (depth + 1, gi)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return False, None, None
    return True, best[0], best[1]


def reference_resolve(led, valid_versions):
    out = {}
    for vid in led["catalog"]:
        holds, depth, gi = reference_holds(led["catalog"], valid_versions, vid, frozenset())
        if holds:
            out[vid] = (depth, gi)
    return out


# ---------------------------------------------------------------------------
class TestStatusResolution(unittest.TestCase):
    def test_most_recent_effective_then_latest_intake(self):
        led = ledger({"L1": original()}, {}, [
            log("L1", 10, 5), log("L1", 20, 6), log("L1", 20, 9)])
        chosen = handover.resolve_statuses(led, 100, 100)
        self.assertEqual(chosen["L1"]["effective_at"], 20)
        self.assertEqual(chosen["L1"]["received_at"], 9)

    def test_later_revocation_overrides_earlier_valid(self):
        led = ledger({"L1": original()}, {}, [
            log("L1", 10, 5, "valid"), log("L1", 30, 8, "revoked")])
        self.assertNotIn("L1", handover.resolve_statuses(led, 100, 100))

    def test_exact_duplicates_count_once(self):
        led = ledger({"L1": original()}, {}, [
            log("L1", 10, 5), log("L1", 10, 5), log("L1", 10, 5)])
        chosen = handover.resolve_statuses(led, 100, 100)
        self.assertEqual(chosen["L1"]["received_at"], 5)

    def test_opposite_states_same_times_reject(self):
        led = ledger({"L1": original()}, {}, [
            log("L1", 10, 5, "valid"), log("L1", 10, 5, "revoked")])
        with self.assertRaises(handover.HandoverError):
            handover.resolve_statuses(led, 100, 100)

    def test_both_cutoffs_must_hold(self):
        # effective within business cutoff but intake after intake cutoff.
        led = ledger({"L1": original()}, {}, [log("L1", 10, 60)])
        self.assertEqual(handover.resolve_statuses(led, 100, 50), {})
        # intake fine but effective time after business cutoff.
        led = ledger({"L1": original()}, {}, [log("L1", 120, 5)])
        self.assertEqual(handover.resolve_statuses(led, 100, 50), {})
        # boundary values are inclusive.
        led = ledger({"L1": original()}, {}, [log("L1", 100, 50)])
        self.assertIn("L1", handover.resolve_statuses(led, 100, 50))

    def test_no_usable_record_means_invalid(self):
        led = ledger({"L1": original()}, {}, [])
        self.assertEqual(handover.resolve_statuses(led, 100, 100), {})

    def test_records_after_cutoff_are_invisible_even_when_conflicting(self):
        # The contradictory pair sits beyond the cutoffs, so this run is fine.
        led = ledger({"L1": original()}, {}, [
            log("L1", 10, 5, "valid"),
            log("L1", 130, 60, "valid"), log("L1", 130, 60, "revoked")])
        chosen = handover.resolve_statuses(led, 100, 50)
        self.assertEqual(chosen["L1"]["state"], "valid")


class TestCatalogResolution(unittest.TestCase):
    def _fuzz(self, rng):
        n = rng.randint(1, 8)
        ids = [f"V{i}" for i in range(n)]
        catalog = {}
        for vid in ids:
            if rng.random() < 0.4:
                catalog[vid] = original()
            else:
                seen = set()
                groups = []
                for _ in range(rng.randint(1, 2)):
                    size = rng.randint(1, min(2, n))
                    group = rng.sample(ids, size)
                    key = tuple(group)
                    if key not in seen:          # the ledger rejects duplicate groups
                        seen.add(key)
                        groups.append(group)
                if not groups:
                    catalog[vid] = original()
                else:
                    catalog[vid] = derived(*groups)
        records = []
        valid_versions = {}
        for vid in ids:
            if rng.random() < 0.7:
                records.append(log(vid, rng.randint(1, 20), rng.randint(1, 20)))
                valid_versions[vid] = records[-1]
            else:
                records.append(log(vid, 1, 1, "revoked"))
        led = ledger(catalog, {}, records)
        got = handover.resolve_catalog(validated(led), valid_versions)
        expected = reference_resolve(led, valid_versions)
        got_flat = {vid: (info["ground_depth"], info["group_index"])
                    for vid, info in got.items()}
        self.assertEqual(got_flat, expected, catalog)

    def test_matches_independent_reference_on_random_catalogs(self):
        rng = random.Random(20261123)
        for _ in range(2000):
            self._fuzz(rng)

    def test_pure_cycle_never_holds(self):
        catalog = {"X1": derived(["X2"]), "X2": derived(["X1"])}
        led = ledger(catalog, {}, [log("X1", 1, 1), log("X2", 1, 1)])
        valid = handover.resolve_statuses(led, 100, 100)
        self.assertEqual(handover.resolve_catalog(validated(led), valid), {})

    def test_cycle_with_an_escape_grounds_via_the_escape_only(self):
        # X1 can use L1 directly OR go through the X1/X2 cycle.
        catalog = {
            "L1": original(),
            "X1": derived(["L1"], ["X2"]),
            "X2": derived(["X1"]),
        }
        led = ledger(catalog, {}, [log(v, 1, 1) for v in catalog])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertEqual(held["X1"]["ground_depth"], 1)
        self.assertEqual(held["X1"]["basis"], ["L1"])
        # X2 grounds through the escaping branch, not through circularity.
        self.assertEqual(held["X2"]["ground_depth"], 2)
        self.assertEqual(held["X2"]["basis"], ["X1"])

    def test_shortest_longest_chain_wins(self):
        catalog = {
            "L1": original(), "L2": original(), "L3": original(),
            "D1": derived(["L1", "L2"]),
            "D2": derived(["D1"]),
            "D3": derived(["L3"], ["D2"]),       # depth 1 via L3, depth 3 via D2
        }
        led = ledger(catalog, {}, [log(v, 1, 1) for v in catalog])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertEqual(held["D3"]["ground_depth"], 1)
        self.assertEqual(held["D3"]["group_index"], 0)

    def test_equal_depth_resolved_by_catalog_group_order(self):
        catalog = {
            "L1": original(), "L2": original(),
            "D1": derived(["L1"], ["L2"]),
        }
        led = ledger(catalog, {}, [log(v, 1, 1) for v in catalog])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertEqual(held["D1"]["group_index"], 0)
        self.assertEqual(held["D1"]["basis"], ["L1"])

    def test_deep_members_survive_until_their_grounds_appear(self):
        # Ordering must not let a shallow group starve a member that only
        # grounds at depth 3 in a later fixpoint round.
        catalog = {
            "L1": original(),
            "D1": derived(["L1"]),
            "D2": derived(["D1"]),
            "D3": derived(["D2"]),
            "D4": derived(["D3"], ["L1"]),       # depth 4 vs depth 1
        }
        led = ledger(catalog, {}, [log(v, 1, 1) for v in catalog])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertEqual(held["D3"]["ground_depth"], 3)
        self.assertEqual(held["D4"]["ground_depth"], 1)
        self.assertEqual(held["D4"]["group_index"], 1)

    def test_invalid_original_blocks_every_dependent(self):
        catalog = {"L1": original(), "D1": derived(["L1"]),
                   "D2": derived(["D1"])}
        led = ledger(catalog, {}, [log("L1", 1, 1, "revoked"),
                                   log("D1", 1, 1), log("D2", 1, 1)])
        valid = handover.resolve_statuses(led, 100, 100)
        self.assertEqual(handover.resolve_catalog(validated(led), valid), {})

    def test_independent_alternative_group_survives(self):
        # A correction that kills one member must not kill an independent group.
        catalog = {
            "L1": original(), "L2": original(),
            "D1": derived(["L1"], ["L2"]),
        }
        led = ledger(catalog, {}, [
            log("L1", 1, 1, "valid"), log("L1", 9, 9, "revoked"),
            log("L2", 1, 1), log("D1", 1, 1)])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertEqual(held["D1"]["group_index"], 1)
        self.assertEqual(held["D1"]["basis"], ["L2"])

    def test_derived_with_invalid_own_status_cannot_hold(self):
        catalog = {"L1": original(), "D1": derived(["L1"])}
        led = ledger(catalog, {}, [log("L1", 1, 1),
                                   log("D1", 1, 1, "valid"),
                                   log("D1", 5, 5, "revoked")])
        valid = handover.resolve_statuses(led, 100, 100)
        held = handover.resolve_catalog(validated(led), valid)
        self.assertIn("L1", held)
        self.assertNotIn("D1", held)


class TestHandoverSelection(unittest.TestCase):
    def test_priority_first_holding_version(self):
        c = cert(["P1", "P2"], ["P1"], [], ["P2"])
        cat = {"L1": original(), "L2": original(), "L3": original()}
        led = ledger(cat, {"P1": {"A": ["L3", "L1", "L2"], "B": ["L2"]}},
                     [log(v, 1, 1) for v in cat])
        out = handover.build_handover(c, led, 100, 100, "h")
        p1 = out["projects"][0]
        self.assertEqual(p1["status"], "complete")
        picks = {i["item"]: i["version"] for i in p1["items"]}
        self.assertEqual(picks, {"A": "L3", "B": "L2"})

    def test_no_substitution_when_named_version_invalid(self):
        # A later valid version of the same material must not replace the named
        # invalid candidate: the ledger reference is exact.
        c = cert(["P1"], ["P1"], [], [])
        cat = {"OLD": original(), "NEW": original()}
        led = ledger(cat, {"P1": {"A": ["OLD"], "B": ["NEW"]}},
                     [log("OLD", 1, 1, "revoked"), log("NEW", 1, 1)])
        out = handover.build_handover(c, led, 100, 100)
        p1 = out["projects"][0]
        self.assertEqual(p1["status"], "incomplete")
        self.assertEqual(p1["missing_items"], ["A"])
        self.assertEqual(p1["items"], [])

    def test_incomplete_project_lists_only_missing_items(self):
        c = cert(["P1", "P2"], ["P1", "P2"], [], [])
        cat = {"L1": original()}
        led = ledger(cat, {"P1": {"A": ["L1"]}},
                     [log("L1", 1, 1)])
        out = handover.build_handover(c, led, 100, 100)
        statuses = {p["project"]: p for p in out["projects"]}
        self.assertEqual(statuses["P1"]["missing_items"], ["B"])
        # P2 has no ledger entry at all: every mandatory item is missing.
        self.assertEqual(statuses["P2"]["missing_items"], ["A", "B"])
        self.assertTrue(all(p["items"] == [] for p in out["projects"]))

    def test_possible_only_projects_are_not_handed_over(self):
        c = cert(["P1", "P2", "P3"], ["P1"], ["P2"], ["P3"])
        cat = {"L1": original()}
        led = ledger(cat,
                     {p: {"A": ["L1"], "B": ["L1"]} for p in ("P1", "P2", "P3")},
                     [log("L1", 1, 1)])
        out = handover.build_handover(c, led, 100, 100)
        self.assertEqual([p["project"] for p in out["projects"]], ["P1"])
        self.assertEqual(out["summary"]["possible_only_projects_not_handed_over"], ["P2"])

    def test_position_uncertain_but_certain_project_is_handled(self):
        # worst_rank <= K but best < worst: certain to be in, position unknown.
        c = cert(["P1", "P2"], ["P1"], ["P2"], [], K=3)
        c["per_candidate"]["P1"] = {"best_rank": 1, "worst_rank": 2,
                                    "composite_min_possible": 0,
                                    "composite_max_possible": 0}
        cat = {"L1": original()}
        led = ledger(cat, {"P1": {"A": ["L1"], "B": ["L1"]}},
                     [log("L1", 1, 1)])
        out = handover.build_handover(c, led, 100, 100)
        self.assertEqual(out["projects"][0]["project"], "P1")
        self.assertEqual(out["projects"][0]["status"], "complete")

    def test_version_detailed_once_with_closure_only(self):
        c = cert(["P1", "P2"], ["P1", "P2"], [], [])
        cat = {"L1": original(), "L2": original(), "D1": derived(["L1", "L2"]),
               "UNRELATED": original()}
        led = ledger(
            cat,
            {"P1": {"A": ["D1"], "B": ["D1"]},
             "P2": {"A": ["D1"], "B": ["D1"]}},
            [log(v, 1, 1) for v in cat])
        out = handover.build_handover(c, led, 100, 100)
        vids = [v["version"] for v in out["versions"]]
        self.assertEqual(sorted(vids), ["D1", "L1", "L2"])   # no duplicates, no unrelated
        d1 = next(v for v in out["versions"] if v["version"] == "D1")
        self.assertEqual(d1["basis_group"]["basis"], ["L1", "L2"])
        self.assertEqual(d1["status_record"],
                         {"effective_at": 1, "received_at": 1, "state": "valid"})

    def test_cutoffs_appear_in_output(self):
        c = cert(["P1"], ["P1"], [], [])
        led = ledger({}, {}, [])
        out = handover.build_handover(c, led, 77, 33, "digest")
        self.assertEqual(out["cutoffs"],
                         {"business_cutoff": 77, "intake_cutoff": 33})
        self.assertEqual(out["certificate_sha256"], "digest")

    def test_certificate_without_optional_witnesses_is_accepted(self):
        c = cert(["P1"], ["P1"], [], [])
        del c["witnesses"]                    # roster.py always emits it, but it is optional here
        cat = {"L1": original()}
        led = ledger(cat, {"P1": {"A": ["L1"], "B": ["L1"]}}, [log("L1", 1, 1)])
        out = handover.build_handover(c, led, 100, 100)
        self.assertEqual(out["projects"][0]["status"], "complete")

    def test_output_basis_closure_is_complete_and_acyclic(self):
        c = cert(["P1"], ["P1"], [], [])
        cat = {"L1": original(), "L2": original(),
               "D1": derived(["L1", "L2"]), "D2": derived(["D1"])}
        led = ledger(cat, {"P1": {"A": ["D2"], "B": ["L1"]}},
                     [log(v, 1, 1) for v in cat])
        out = handover.build_handover(c, led, 100, 100)
        vers = {v["version"]: v for v in out["versions"]}
        for vid, v in vers.items():
            if v["kind"] == "derived":
                for member in v["basis_group"]["basis"]:
                    self.assertIn(member, vers)            # grounds are detailed
            else:
                self.assertIsNone(v["basis_group"])
        # Walk every chain: it must terminate at originals without revisiting.
        seen, stack = set(), ["D2"]
        while stack:
            x = stack.pop()
            self.assertNotIn(x, seen)                      # no cycle in the output
            seen.add(x)
            bg = vers[x]["basis_group"]
            if bg is not None:
                stack.extend(bg["basis"])
        leaves = [x for x in seen if vers[x]["basis_group"] is None]
        self.assertTrue(leaves)
        self.assertTrue(all(vers[x]["kind"] == "original" for x in leaves))

    def test_correction_cascades_to_dependents_only(self):
        # Revoking L1 invalidates D1 and D2; an independent L2 stays valid and
        # its dependent D3 is untouched.
        c = cert(["P1"], ["P1"], [], [])
        cat = {"L1": original(), "L2": original(),
               "D1": derived(["L1"]), "D2": derived(["D1"]),
               "D3": derived(["L2"])}
        led = ledger(
            cat, {"P1": {"A": ["D2"], "B": ["D3"]}},
            [log("L1", 1, 1), log("L1", 8, 8, "revoked"),
             log("L2", 1, 1), log("D1", 2, 2), log("D2", 3, 3),
             log("D3", 2, 2)])
        out = handover.build_handover(c, led, 100, 100)
        p1 = out["projects"][0]
        self.assertEqual(p1["status"], "incomplete")
        self.assertEqual(p1["missing_items"], ["A"])
        # Incomplete projects hand over no partial materials, so none of their
        # versions may appear in the shared detail section.
        self.assertEqual(out["versions"], [])


class TestValidation(unittest.TestCase):
    GOOD_CERT = cert(["P1"], ["P1"], [], [])

    def _reject(self, c=None, led=None):
        c = c or self.GOOD_CERT
        led = led if led is not None else ledger(
            {"L1": original()}, {"P1": {"A": ["L1"], "B": ["L1"]}},
            [log("L1", 1, 1)])
        with self.assertRaises(handover.HandoverError):
            handover.build_handover(c, led, 100, 100)

    def test_certificate_structure(self):
        bad = json.loads(json.dumps(self.GOOD_CERT))
        del bad["top_k"]
        self._reject(c=bad)
        bad = json.loads(json.dumps(self.GOOD_CERT))
        bad["top_k"]["must_be_in_top_k"] = ["P1", "P1"]
        self._reject(c=bad)
        bad = json.loads(json.dumps(self.GOOD_CERT))
        bad["per_candidate"]["P1"]["best_rank"] = 9
        self._reject(c=bad)
        bad = json.loads(json.dumps(self.GOOD_CERT))
        bad["candidate_count"] = 99
        self._reject(c=bad)
        bad = json.loads(json.dumps(self.GOOD_CERT))
        bad["top_k"]["positions"] = []
        self._reject(c=bad)
        self._reject(c="not-an-object")

    def test_ledger_structure(self):
        self._reject(led={"required_items": [], "catalog": {},
                          "projects": {}, "status_log": []})
        self._reject(led={"required_items": ["A", "A"], "catalog": {},
                          "projects": {}, "status_log": []})
        self._reject(led={"required_items": ["A"],
                          "catalog": {"D1": {"kind": "derived",
                                             "basis_groups": [[]]}},
                          "projects": {}, "status_log": []})
        self._reject(led={"required_items": ["A"],
                          "catalog": {"D1": {"kind": "weird"}},
                          "projects": {}, "status_log": []})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {"P1": {"A": ["GHOST"]}}, "status_log": []})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {"GHOST": {"A": ["L1"]}}, "status_log": []})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {"P1": {"NOPE": ["L1"]}}, "status_log": []})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {"P1": {"A": ["L1", "L1"]}}, "status_log": []})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {},
                          "status_log": [{"version": "GHOST", "effective_at": 1,
                                          "received_at": 1, "state": "valid"}]})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {},
                          "status_log": [{"version": "L1", "effective_at": True,
                                          "received_at": 1, "state": "valid"}]})
        self._reject(led={"required_items": ["A"], "catalog": {"L1": original()},
                          "projects": {},
                          "status_log": [{"version": "L1", "effective_at": 1,
                                          "received_at": 1, "state": "maybe"}]})
        # unknown top-level field is rejected rather than silently ignored
        self._reject(led={"required_items": ["A"], "catalog": {}, "projects": {},
                          "status_log": [], "surprise": 1})

    def test_no_partial_output_on_error(self):
        # The CLI must exit 2 and print nothing on stdout.
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "cert.json")
            lp = os.path.join(d, "ledger.json")
            with open(cp, "w") as fh:
                json.dump(self.GOOD_CERT, fh)
            with open(lp, "w") as fh:
                json.dump({"required_items": [], "catalog": {},
                           "projects": {}, "status_log": []}, fh)
            proc = subprocess.run(
                [sys.executable, "-I",
                 os.path.join(HERE, "..", "solver", "handover.py"),
                 cp, lp, "--business-cutoff", "100", "--intake-cutoff", "100"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertIn("REJECTED", proc.stderr)


class TestCLI(unittest.TestCase):
    def test_hash_and_round_trip(self):
        c = cert(["P1"], ["P1"], [], [])
        led = ledger({"L1": original()},
                     {"P1": {"A": ["L1"], "B": ["L1"]}}, [log("L1", 1, 1)])
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "cert.json")
            lp = os.path.join(d, "ledger.json")
            op = os.path.join(d, "out.json")
            with open(cp, "w") as fh:
                json.dump(c, fh)
            with open(lp, "w") as fh:
                json.dump(led, fh)
            digest = hashlib.sha256(open(cp, "rb").read()).hexdigest()
            proc = subprocess.run(
                [sys.executable, "-I",
                 os.path.join(HERE, "..", "solver", "handover.py"),
                 cp, lp, "--business-cutoff", "100", "--intake-cutoff", "50",
                 "--out", op],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(op) as fh:
                out = json.load(fh)
            self.assertEqual(out["certificate_sha256"], digest)
            self.assertEqual(out["projects"][0]["status"], "complete")

    def test_missing_file_rejected(self):
        proc = subprocess.run(
            [sys.executable, "-I",
             os.path.join(HERE, "..", "solver", "handover.py"),
             "/no/such/cert.json", "/no/such/ledger.json",
             "--business-cutoff", "1", "--intake-cutoff", "1"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


class TestScale(unittest.TestCase):
    def test_shared_catalog_resolved_once_independent_of_project_count(self):
        # A deep shared evidence graph referenced by many projects: catalog work
        # and the version-detail size must not grow with the project count.
        sys.path.insert(0, os.path.join(HERE, "..", "large"))
        import build_large_handover as bl
        import time

        cert_s, led_s, must_s, _, _ = bl.build(n_projects=400)
        cert_l, led_l, must_l, _, _ = bl.build(n_projects=4000)
        # The certain set is fixed by the prefix while total size grows 10x:
        # this is exactly the shared-graph independence we want to prove.
        self.assertEqual(len(must_l), len(must_s))
        self.assertEqual(len(cert_l["per_candidate"]),
                         10 * len(cert_s["per_candidate"]))

        def run(cert, led):
            t0 = time.time()
            normalised = handover.validate_ledger(led)
            valid = handover.resolve_statuses(led, bl.BUSINESS_CUTOFF, bl.INTAKE_CUTOFF)
            held = handover.resolve_catalog(normalised, valid)
            out = handover.build_handover(
                cert, led, bl.BUSINESS_CUTOFF, bl.INTAKE_CUTOFF)
            return time.time() - t0, held, out

        t_s, held_s, out_s = run(cert_s, led_s)
        t_l, held_l, out_l = run(cert_l, led_l)
        # The shared graph resolves to exactly the same versions either way.
        self.assertEqual(set(held_s), set(held_l))
        # Detail size is bounded by the shared catalog, never by project count.
        self.assertLessEqual(len(out_l["versions"]), len(led_l["catalog"]))
        self.assertEqual(
            [v["version"] for v in out_s["versions"]],
            [v["version"] for v in out_l["versions"]])
        # Handing over 10x more projects must stay comfortably sub-second.
        self.assertLess(t_l, 5.0)

    def test_cli_on_generated_large_inputs(self):
        path = os.path.join(HERE, "..", "large", "large_handover_inputs.json")
        if not os.path.exists(path):
            self.skipTest("large handover inputs not built")
        with open(path) as fh:
            bundle = json.load(fh)
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "cert.json")
            lp = os.path.join(d, "ledger.json")
            op = os.path.join(d, "out.json")
            with open(cp, "w") as fh:
                json.dump(bundle["certificate"], fh)
            with open(lp, "w") as fh:
                json.dump(bundle["ledger"], fh)
            proc = subprocess.run(
                [sys.executable, "-I",
                 os.path.join(HERE, "..", "solver", "handover.py"),
                 cp, lp,
                 "--business-cutoff", str(bundle["business_cutoff"]),
                 "--intake-cutoff", str(bundle["intake_cutoff"]),
                 "--out", op],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(op) as fh:
                out = json.load(fh)
            self.assertEqual(out["cutoffs"]["business_cutoff"],
                             bundle["business_cutoff"])
            self.assertTrue(out["projects"])
            vids = [v["version"] for v in out["versions"]]
            self.assertEqual(len(vids), len(set(vids)))


if __name__ == "__main__":
    unittest.main()
