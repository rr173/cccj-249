"""Tests for the review-group batch synchronization tool (solver/proc_sync.py).

Every rule in the brief gets a small, hand-built witness:

* initialization scopes on handover ``complete`` projects and refuses bad
  ownership/roster material;
* delivery files are validated all-or-nothing (bad format, unknown
  group/project, within-file and stored position conflicts);
* identical re-delivery is a no-op while different content at an occupied
  position conflicts;
* closes detect gaps, never roll groups back, pair both transfer legs and
  reject one-sided / mismatched / id-reused transfers;
* the merge respects per-group order and business ownership, including the
  old-group conclusion written after the transfer-out;
* packages carry only real before/after differences, one row per project,
  with explicit empty conclusions;
* closes and their packages are atomic and idempotent under retries; failed
  or awaiting requests consume no release number and advance nothing;
* incremental closes only touch the new window; records survive restarts;
* the CLI behaves the same as the library API.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import proc_sync  # noqa: E402

SOLVER = os.path.join(HERE, "..", "solver", "proc_sync.py")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def conclusion(group, position, project, action):
    return {"group": group, "position": position,
            "project": project, "action": action}


def transfer(group, position, project, tid, from_group, to_group, action):
    return {"group": group, "position": position, "project": project,
            "action": action, "transfer_id": tid,
            "from_group": from_group, "to_group": to_group}


def out(position, project, tid, frm, to):
    return transfer(frm, position, project, tid, frm, to, "transfer_out")


def inn(position, project, tid, frm, to):
    return transfer(to, position, project, tid, frm, to, "transfer_in")


class SyncFixture:
    """A fresh temp working directory plus the three initialization inputs."""

    def __init__(self, projects=("P1", "P2", "P3"),
                 groups=("G1", "G2"),
                 ownership=None):
        self.tmp = tempfile.mkdtemp()
        self.workdir = os.path.join(self.tmp, "work")
        if ownership is None:
            ownership = {p: groups[0] for p in projects}
        handover = {"projects": [
            {"project": p, "status": "complete", "items": [],
             "missing_items": []} for p in projects
        ] + [{"project": "PX", "status": "incomplete", "items": [],
              "missing_items": ["x"]}]}
        self.handover = self._write("handover.json", handover)
        self.groups_file = self._write("groups.json", {"groups": list(groups)})
        self.ownership_file = self._write(
            "ownership.json", {"ownership": ownership})
        result = proc_sync.init_workdir(
            self.workdir, self.handover,
            self.groups_file, self.ownership_file)
        self.projects, self.groups = result["projects"], result["groups"]
        self.ownership = ownership

    def _write(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def ingest(self, records):
        path = self._write(f"delivery-{len(os.listdir(self.tmp))}.json",
                           {"records": records})
        return proc_sync.ingest_file(self.workdir, path)

    def close(self, release, positions):
        path = self._write(f"close-{release}.json",
                           {"release": release, "positions": positions})
        return proc_sync.request_close(self.workdir, path)

    def state(self):
        return proc_sync.load_state(self.workdir)


# ---------------------------------------------------------------------------
# initialization
# ---------------------------------------------------------------------------
class TestInit(unittest.TestCase):
    def test_scopes_on_complete_projects_and_zeros(self):
        fx = SyncFixture()
        state = fx.state()
        self.assertEqual(state["projects"], ["P1", "P2", "P3"])
        self.assertEqual(state["groups"], ["G1", "G2"])
        self.assertEqual(state["published"]["positions"],
                         {"G1": 0, "G2": 0})
        self.assertEqual(state["published"]["last_release"], 0)
        self.assertEqual(state["conclusions"],
                         {"P1": None, "P2": None, "P3": None})
        self.assertEqual(state["ownership"],
                         {"P1": "G1", "P2": "G1", "P3": "G1"})
        self.assertEqual(state["records"], {"G1": {}, "G2": {}})

    def test_reject_unknown_owner_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "h.json"), "w") as fh:
                json.dump({"projects": [{"project": "P1", "status": "complete"}]}, fh)
            with open(os.path.join(tmp, "g.json"), "w") as fh:
                json.dump({"groups": ["G1"]}, fh)
            with open(os.path.join(tmp, "o.json"), "w") as fh:
                json.dump({"ownership": {"P1": "G9"}}, fh)
            with self.assertRaises(proc_sync.SyncError):
                proc_sync.init_workdir(os.path.join(tmp, "w"),
                                       os.path.join(tmp, "h.json"),
                                       os.path.join(tmp, "g.json"),
                                       os.path.join(tmp, "o.json"))

    def test_ownership_must_cover_exactly_the_complete_projects(self):
        fx = SyncFixture.__new__(SyncFixture)
        fx.tmp = tempfile.mkdtemp()
        with open(os.path.join(fx.tmp, "h.json"), "w") as fh:
            json.dump({"projects": [{"project": "P1", "status": "complete"},
                                    {"project": "P2", "status": "complete"}]}, fh)
        with open(os.path.join(fx.tmp, "g.json"), "w") as fh:
            json.dump({"groups": ["G1"]}, fh)
        with open(os.path.join(fx.tmp, "o.json"), "w") as fh:
            json.dump({"ownership": {"P1": "G1"}}, fh)  # P2 missing
        with self.assertRaises(proc_sync.SyncError):
            proc_sync.init_workdir(os.path.join(fx.tmp, "w"),
                                   os.path.join(fx.tmp, "h.json"),
                                   os.path.join(fx.tmp, "g.json"),
                                   os.path.join(fx.tmp, "o.json"))

    def test_double_init_refused(self):
        fx = SyncFixture()
        with self.assertRaises(proc_sync.SyncError):
            proc_sync.init_workdir(fx.workdir, fx.handover,
                                   fx.groups_file, fx.ownership_file)


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
class TestIngest(unittest.TestCase):
    def test_out_of_order_batched_and_duplicate(self):
        fx = SyncFixture()
        # positions 3 then 1, position 2 missing.
        r = fx.ingest([conclusion("G1", 3, "P1", "pass"),
                       conclusion("G1", 1, "P2", "fail")])
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["new_records"], 2)
        # exact same records re-sent (reordered): no new actions.
        r = fx.ingest([conclusion("G1", 1, "P2", "fail"),
                       conclusion("G1", 3, "P1", "pass")])
        self.assertEqual(r["new_records"], 0)
        self.assertEqual(r["duplicate_records"], 2)
        # records persist with their positions.
        stored = fx.state()["records"]["G1"]
        self.assertEqual(set(stored), {"1", "3"})

    def test_conflict_at_stored_position_rejects_whole_file(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        before = fx.state()
        with self.assertRaises(proc_sync.SyncError):
            fx.ingest([conclusion("G1", 2, "P2", "fail"),      # would be new
                       conclusion("G1", 1, "P1", "fail")])     # conflicts
        after = fx.state()
        self.assertEqual(before, after)                        # nothing survived

    def test_conflict_inside_one_file_rejects_whole_file(self):
        fx = SyncFixture()
        with self.assertRaises(proc_sync.SyncError):
            fx.ingest([conclusion("G1", 1, "P1", "pass"),
                       conclusion("G1", 1, "P2", "pass")])
        self.assertEqual(fx.state()["records"], {"G1": {}, "G2": {}})

    def test_malformed_unknown_group_project_action(self):
        fx = SyncFixture()
        bad_cases = [
            [{"group": "G9", "position": 1, "project": "P1", "action": "pass"}],
            [{"group": "G1", "position": 1, "project": "P9", "action": "pass"}],
            [{"group": "G1", "position": 1, "project": "P1", "action": "approve"}],
            [{"group": "G1", "position": 0, "project": "P1", "action": "pass"}],
            [{"group": "G1", "position": "1", "project": "P1", "action": "pass"}],
            [{"group": "G1", "position": 1, "project": "P1",
              "action": "transfer_out"}],
            [{"group": "G1", "position": 1, "project": "P1",
              "action": "transfer_out", "transfer_id": "T1",
              "from_group": "G1", "to_group": "G1"}],
            [{"group": "G2", "position": 1, "project": "P1",
              "action": "transfer_in", "transfer_id": "T1",
              "from_group": "G1", "to_group": "G1"}],
        ]
        for bad in bad_cases:
            with self.assertRaises(proc_sync.SyncError):
                fx.ingest(bad)
        # malformed JSON file itself.
        path = os.path.join(fx.tmp, "broken.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(proc_sync.SyncError):
            proc_sync.ingest_file(fx.workdir, path)
        self.assertEqual(fx.state()["records"], {"G1": {}, "G2": {}})

    def test_records_survive_reload(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        again = proc_sync.load_state(fx.workdir)
        self.assertEqual(again["records"]["G1"]["1"]["action"], "pass")


# ---------------------------------------------------------------------------
# close: gaps, rollback, release numbering
# ---------------------------------------------------------------------------
class TestCloseLifecycle(unittest.TestCase):
    def test_gap_means_awaiting_and_consumes_nothing(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 2, "P1", "pass")])  # position 1 missing
        outcome, payload = fx.close(1, {"G1": 2, "G2": 0})
        self.assertEqual(outcome, "awaiting")
        self.assertEqual(payload["missing_positions"], {"G1": [1]})
        state = fx.state()
        self.assertEqual(state["published"]["last_release"], 0)
        self.assertEqual(state["published"]["positions"], {"G1": 0, "G2": 0})
        # the already received stream is retained.
        self.assertIn("2", state["records"]["G1"])

    def test_fill_gap_then_publish_and_advance(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 2, "P1", "pass")])
        outcome, _ = fx.close(1, {"G1": 2, "G2": 0})
        self.assertEqual(outcome, "awaiting")
        fx.ingest([conclusion("G1", 1, "P2", "fail")])
        outcome, package = fx.close(1, {"G1": 2, "G2": 0})
        self.assertEqual(outcome, "published")
        self.assertEqual(package["release"], 1)
        self.assertEqual(package["previous_positions"], {"G1": 0, "G2": 0})
        self.assertEqual(package["positions"], {"G1": 2, "G2": 0})
        status = proc_sync.workdir_status(fx.workdir)
        self.assertEqual(status["published_positions"], {"G1": 2, "G2": 0})
        self.assertTrue(os.path.exists(
            proc_sync.package_path(fx.workdir, 1)))

    def test_rollback_behind_published_position_rejected(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        fx.close(1, {"G1": 1, "G2": 0})
        with self.assertRaises(proc_sync.SyncError):
            fx.close(2, {"G1": 0, "G2": 0})

    def test_release_numbers_are_sequential_and_failed_ones_free(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 2, "P1", "pass")])  # gap
        outcome, _ = fx.close(1, {"G1": 2, "G2": 0})
        self.assertEqual(outcome, "awaiting")
        # 1 was not consumed: reusing it after fixing the gap works.
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        outcome, _ = fx.close(1, {"G1": 2, "G2": 0})
        self.assertEqual(outcome, "published")
        with self.assertRaises(proc_sync.SyncError):
            fx.close(3, {"G1": 2, "G2": 0})   # skips 2

    def test_empty_window_close_is_a_valid_no_op_package(self):
        fx = SyncFixture()
        outcome, package = fx.close(1, {"G1": 0, "G2": 0})
        self.assertEqual(outcome, "published")
        self.assertEqual(package["changes"], [])
        self.assertEqual(package["positions"], {"G1": 0, "G2": 0})


# ---------------------------------------------------------------------------
# transfer semantics and the ownership-respecting merge
# ---------------------------------------------------------------------------
class TestTransfer(unittest.TestCase):
    def _two_group_fixture(self):
        return SyncFixture(
            projects=("P1",), groups=("G1", "G2"),
            ownership={"P1": "G1"})

    def test_one_sided_transfer_awaiting_then_rejected_on_close(self):
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        # G2's leg has simply not arrived -> gap-style wait if it is requested.
        outcome, payload = fx.close(1, {"G1": 1, "G2": 1})
        self.assertEqual(outcome, "awaiting")
        self.assertEqual(payload["missing_positions"], {"G2": [1]})
        # Request only the side that exists: the lone leg rejects the close.
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 1, "G2": 0})
        state = fx.state()
        self.assertEqual(state["published"]["last_release"], 0)

    def test_successful_transfer_clears_conclusion_and_moves_owner(self):
        fx = self._two_group_fixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        fx.ingest([out(2, "P1", "T1", "G1", "G2"),
                   inn(1, "P1", "T1", "G1", "G2")])
        outcome, package = fx.close(1, {"G1": 2, "G2": 1})
        self.assertEqual(outcome, "published")
        self.assertEqual(package["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": None}])
        state = fx.state()
        self.assertEqual(state["ownership"]["P1"], "G2")
        self.assertIsNone(state["conclusions"]["P1"])

    def test_receiver_conclusion_inside_same_window(self):
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2"),
                   conclusion("G2", 2, "P1", "fail")])
        outcome, package = fx.close(1, {"G1": 1, "G2": 2})
        self.assertEqual(package["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": "fail"}])

    def test_old_group_conclusion_after_transfer_out_is_rejected(self):
        # The brief's headline case: G1 transfers P1 out, then writes a
        # conclusion anyway; G2 has confirmed receipt. The legs pair, but the
        # stale old-group conclusion can never become effective.
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   conclusion("G1", 2, "P1", "pass")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 1})
        self.assertEqual(fx.state()["published"]["last_release"], 0)

    def test_mismatched_legs_rejected(self):
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        # Each leg sits on its own group's stream, but the in-leg names a
        # different project than the out-leg: close-time pair check rejects.
        fx2_records = [
            {"group": "G2", "position": 1, "project": "P2",
             "action": "transfer_in", "transfer_id": "T1",
             "from_group": "G1", "to_group": "G2"}]
        fx = SyncFixture(projects=("P1", "P2"), groups=("G1", "G2"),
                         ownership={"P1": "G1", "P2": "G1"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        fx.ingest(fx2_records)
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 1, "G2": 1})

    def test_transfer_id_reused_for_another_handover_rejected(self):
        fx = SyncFixture(projects=("P1", "P2"), groups=("G1", "G2"),
                         ownership={"P1": "G1", "P2": "G1"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   out(2, "P2", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2"),
                   inn(2, "P2", "T1", "G1", "G2")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 2})

    def test_transfer_id_reuse_across_releases_rejected(self):
        # T1 legitimately closes in release 1; reusing the same id for a
        # different handover in release 2 must be caught even though the
        # release-1 streams are already published and never re-scanned.
        fx = SyncFixture(projects=("P1", "P2"), groups=("G1", "G2"),
                         ownership={"P1": "G1", "P2": "G2"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        outcome, _ = fx.close(1, {"G1": 1, "G2": 1})
        self.assertEqual(outcome, "published")
        # Same id, different handover (P2 the other direction).
        fx.ingest([out(2, "P2", "T1", "G2", "G1")])
        fx.ingest([inn(2, "P2", "T1", "G2", "G1")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(2, {"G1": 2, "G2": 2})
        # Nothing advanced; the bad records remain stored.
        self.assertEqual(fx.state()["published"]["last_release"], 1)
        self.assertEqual(fx.state()["transfers"]["T1"]["consistent"], False)

    def test_third_leg_on_same_transfer_id_rejected(self):
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   out(2, "P1", "T1", "G1", "G2")])  # second out, same id
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 1})

    def test_chain_transfer_three_groups(self):
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2", "G3"),
                         ownership={"P1": "G1"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   inn(1, "P1", "T1", "G1", "G2"),
                   out(2, "P1", "T2", "G2", "G3")])
        fx.ingest([inn(1, "P1", "T2", "G2", "G3"),
                   conclusion("G3", 2, "P1", "pass")])
        outcome, package = fx.close(1, {"G1": 1, "G2": 2, "G3": 2})
        self.assertEqual(outcome, "published")
        self.assertEqual(package["changes"], [
            {"project": "P1", "owner": "G3", "conclusion": "pass"}])

    def test_transfer_out_without_ownership_is_impossible(self):
        # G1 transfers P1 to G2; later in the same window G1 tries to transfer
        # it again (it no longer owns it) -- the second out can never apply.
        fx = self._two_group_fixture()
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   out(2, "P1", "T2", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2"),
                   inn(2, "P1", "T2", "G1", "G2")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 2})

    def test_conclusion_on_non_owner_rejected_even_without_transfer(self):
        fx = self._two_group_fixture()
        fx.ingest([conclusion("G2", 1, "P1", "pass")])  # G2 never owns P1
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 0, "G2": 1})

    def test_circular_wait_rejected(self):
        # P1 starts at G1, P2 at G2. Each group's stream puts the *receiving*
        # leg before its own *outgoing* leg, so every head waits on the other
        # group: the legs all pair correctly, but no interleaving respects
        # both per-group order and ownership at once.
        fx = SyncFixture(projects=("P1", "P2"), groups=("G1", "G2"),
                         ownership={"P1": "G1", "P2": "G2"})
        fx.ingest([inn(1, "P2", "TA", "G2", "G1"),
                   out(2, "P1", "TB", "G1", "G2")])
        fx.ingest([inn(1, "P1", "TB", "G1", "G2"),
                   out(2, "P2", "TA", "G2", "G1")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 2})
        self.assertEqual(fx.state()["published"]["last_release"], 0)

    def test_transfer_in_on_project_already_owned_is_impossible(self):
        # G2 already owns P2; an in-leg naming G2 as receiver while G2 is the
        # current owner can never apply.
        fx = SyncFixture(projects=("P2",), groups=("G1", "G2"),
                         ownership={"P2": "G2"})
        fx.ingest([{"group": "G1", "position": 1, "project": "P2",
                    "action": "transfer_out", "transfer_id": "T9",
                    "from_group": "G1", "to_group": "G2"}])
        fx.ingest([{"group": "G2", "position": 1, "project": "P2",
                    "action": "transfer_in", "transfer_id": "T9",
                    "from_group": "G1", "to_group": "G2"}])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 1, "G2": 1})


# ---------------------------------------------------------------------------
# package contents and incremental releases
# ---------------------------------------------------------------------------
class TestPackages(unittest.TestCase):
    def test_only_real_differences_one_row_per_project_sorted(self):
        fx = SyncFixture(projects=("P3", "P1", "P2"),
                         ownership={"P3": "G1", "P1": "G1", "P2": "G1"})
        # P1 flips pass->fail within the window: one final row only.
        fx.ingest([conclusion("G1", 1, "P1", "pass"),
                   conclusion("G1", 2, "P1", "fail"),
                   conclusion("G1", 3, "P3", "pass")])
        _, package = fx.close(1, {"G1": 3, "G2": 0})
        self.assertEqual(package["changes"], [
            {"project": "P1", "owner": "G1", "conclusion": "fail"},
            {"project": "P3", "owner": "G1", "conclusion": "pass"}])
        self.assertNotIn("P2", [c["project"] for c in package["changes"]])

    def test_incremental_window_uses_previous_state(self):
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2"),
                         ownership={"P1": "G1"})
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        _, pkg1 = fx.close(1, {"G1": 1, "G2": 0})
        self.assertEqual(pkg1["previous_positions"], {"G1": 0, "G2": 0})
        # Release 2: the transfer.
        fx.ingest([out(2, "P1", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        _, pkg2 = fx.close(2, {"G1": 2, "G2": 1})
        self.assertEqual(pkg2["previous_positions"], {"G1": 1, "G2": 0})
        self.assertEqual(pkg2["positions"], {"G1": 2, "G2": 1})
        self.assertEqual(pkg2["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": None}])
        # Release 3: nothing new on G1; G2 writes the new conclusion.
        fx.ingest([conclusion("G2", 2, "P1", "fail")])
        _, pkg3 = fx.close(3, {"G1": 2, "G2": 2})
        self.assertEqual(pkg3["previous_positions"], {"G1": 2, "G2": 1})
        self.assertEqual(pkg3["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": "fail"}])

    def test_unchanged_final_state_emits_no_row(self):
        # Release 1 establishes the baseline (P1 = pass).  In release 2 the
        # project flips pass -> fail -> pass inside the window: the final
        # state equals the previous published state, so no row is emitted
        # even though intermediate positions changed.
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2"),
                         ownership={"P1": "G1"})
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        _, pkg1 = fx.close(1, {"G1": 1, "G2": 0})
        self.assertEqual(pkg1["changes"], [
            {"project": "P1", "owner": "G1", "conclusion": "pass"}])
        fx.ingest([conclusion("G1", 2, "P1", "fail"),
                   conclusion("G1", 3, "P1", "pass")])
        _, pkg2 = fx.close(2, {"G1": 3, "G2": 0})
        self.assertEqual(pkg2["changes"], [])

    def test_round_trip_across_two_windows(self):
        # G1 -> G2 -> G1 across two releases: each window has a legal merge,
        # and the packages report each new owner with an explicit null
        # conclusion until an owner re-asserts one.
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2"),
                         ownership={"P1": "G1"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        _, pkg1 = fx.close(1, {"G1": 1, "G2": 1})
        self.assertEqual(pkg1["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": None}])
        fx.ingest([out(2, "P1", "T2", "G2", "G1")])
        fx.ingest([inn(2, "P1", "T2", "G2", "G1")])
        _, pkg2 = fx.close(2, {"G1": 2, "G2": 2})
        self.assertEqual(pkg2["previous_positions"], {"G1": 1, "G2": 1})
        self.assertEqual(pkg2["changes"], [
            {"project": "P1", "owner": "G1", "conclusion": None}])

    def test_empty_conclusion_is_explicit(self):
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2"),
                         ownership={"P1": "G1"})
        fx.ingest([conclusion("G1", 1, "P1", "fail")])
        fx.close(1, {"G1": 1, "G2": 0})
        fx.ingest([out(2, "P1", "T1", "G1", "G2")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        _, pkg2 = fx.close(2, {"G1": 2, "G2": 1})
        row = pkg2["changes"][0]
        self.assertIn("conclusion", row)
        self.assertIsNone(row["conclusion"])


# ---------------------------------------------------------------------------
# idempotency, retries and atomicity
# ---------------------------------------------------------------------------
class TestIdempotency(unittest.TestCase):
    def test_retry_returns_identical_package_despite_newer_streams(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass")])
        _, pkg1 = fx.close(1, {"G1": 1, "G2": 0})
        # Newer streams arrive before the warehouse retries.
        fx.ingest([conclusion("G1", 2, "P2", "fail")])
        outcome, retried = fx.close(1, {"G1": 1, "G2": 0})
        self.assertEqual(outcome, "published")
        self.assertEqual(retried, pkg1)
        # Fetch gives the same bytes-shaped package, even if the export was lost.
        os.remove(proc_sync.package_path(fx.workdir, 1))
        fetched = proc_sync.fetch_package(fx.workdir, 1)
        self.assertEqual(fetched, pkg1)
        self.assertTrue(os.path.exists(proc_sync.package_path(fx.workdir, 1)))

    def test_same_release_rebound_to_other_positions_rejected(self):
        fx = SyncFixture()
        fx.ingest([conclusion("G1", 1, "P1", "pass"),
                   conclusion("G1", 2, "P2", "pass")])
        fx.close(1, {"G1": 1, "G2": 0})
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 0})
        # And the bound positions were not changed.
        self.assertEqual(fx.state()["published"]["positions"],
                         {"G1": 1, "G2": 0})

    def test_fetch_unknown_release(self):
        fx = SyncFixture()
        with self.assertRaises(proc_sync.SyncError):
            proc_sync.fetch_package(fx.workdir, 1)

    def test_failed_close_persists_no_progress_but_keeps_records(self):
        fx = SyncFixture(projects=("P1",), groups=("G1", "G2"),
                         ownership={"P1": "G1"})
        fx.ingest([out(1, "P1", "T1", "G1", "G2"),
                   conclusion("G1", 2, "P1", "pass")])
        fx.ingest([inn(1, "P1", "T1", "G1", "G2")])
        with self.assertRaises(proc_sync.SyncError):
            fx.close(1, {"G1": 2, "G2": 1})
        state = fx.state()
        self.assertEqual(state["published"]["last_release"], 0)
        self.assertEqual(state["published"]["positions"], {"G1": 0, "G2": 0})
        self.assertEqual(state["ownership"], {"P1": "G1"})
        self.assertEqual(len(state["records"]["G1"]), 2)
        self.assertEqual(len(state["records"]["G2"]), 1)
        # Fix the data (remove the stale conclusion is impossible: positions
        # are immutable once stored). Instead the correct window excluding
        # position 2 can still publish the valid transfer.
        outcome, package = fx.close(1, {"G1": 1, "G2": 1})
        self.assertEqual(outcome, "published")
        self.assertEqual(package["changes"], [
            {"project": "P1", "owner": "G2", "conclusion": None}])


# ---------------------------------------------------------------------------
# incrementality: no re-scan of already published positions
# ---------------------------------------------------------------------------
class TestIncremental(unittest.TestCase):
    def test_close_touches_only_new_positions(self):
        fx = SyncFixture()
        for p in range(1, 101):
            fx.ingest([conclusion("G1", p, "P1",
                                  "pass" if p % 2 else "fail")])
        fx.close(1, {"G1": 100, "G2": 0})
        fx.ingest([conclusion("G1", 101, "P2", "pass")])

        calls = []
        real_merge = proc_sync._merge_window

        def spy(state, cutoffs):
            calls.append({
                g: len(_) for g, _ in
                proc_sync._window_records(state, cutoffs).items()})
            return real_merge(state, cutoffs)

        proc_sync._merge_window = spy
        try:
            _, package = fx.close(2, {"G1": 101, "G2": 0})
        finally:
            proc_sync._merge_window = real_merge
        self.assertEqual(calls, [{"G1": 1, "G2": 0}])
        self.assertEqual(package["previous_positions"], {"G1": 100, "G2": 0})
        self.assertEqual([c["project"] for c in package["changes"]], ["P2"])

    def test_small_close_does_not_walk_all_projects_for_owners(self):
        # Merge output diff iterates projects, but stored state lookup for the
        # window stays bounded by the stream positions, not the batch size.
        fx = SyncFixture(
            projects=tuple(f"P{i}" for i in range(200)),
            ownership={f"P{i}": "G1" for i in range(200)})
        fx.ingest([conclusion("G1", 1, "P0", "pass")])
        _, package = fx.close(1, {"G1": 1, "G2": 0})
        self.assertEqual(package["changes"], [
            {"project": "P0", "owner": "G1", "conclusion": "pass"}])


# ---------------------------------------------------------------------------
# CLI end to end
# ---------------------------------------------------------------------------
class TestCli(unittest.TestCase):
    def _run(self, *cli_args):
        proc = subprocess.run(
            [sys.executable, "-I", SOLVER, *cli_args],
            capture_output=True, text=True)
        return proc

    def test_full_cli_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "work")
            h = os.path.join(tmp, "handover.json")
            g = os.path.join(tmp, "groups.json")
            o = os.path.join(tmp, "ownership.json")
            d1 = os.path.join(tmp, "d1.json")
            d2 = os.path.join(tmp, "d2.json")
            req = os.path.join(tmp, "close.json")
            out_file = os.path.join(tmp, "pkg.json")
            with open(h, "w") as fh:
                json.dump({"projects": [
                    {"project": "P1", "status": "complete"}]}, fh)
            with open(g, "w") as fh:
                json.dump({"groups": ["G1", "G2"]}, fh)
            with open(o, "w") as fh:
                json.dump({"ownership": {"P1": "G1"}}, fh)
            with open(d1, "w") as fh:
                json.dump({"records": [
                    {"group": "G1", "position": 1, "project": "P1",
                     "action": "transfer_out", "transfer_id": "T1",
                     "from_group": "G1", "to_group": "G2"}]}, fh)
            with open(d2, "w") as fh:
                json.dump({"records": [
                    {"group": "G2", "position": 1, "project": "P1",
                     "action": "transfer_in", "transfer_id": "T1",
                     "from_group": "G1", "to_group": "G2"},
                    {"group": "G2", "position": 2, "project": "P1",
                     "action": "fail"}]}, fh)

            p = self._run("init", work, "--handover", h,
                          "--groups", g, "--ownership", o)
            self.assertEqual(p.returncode, 0, p.stderr)

            p = self._run("ingest", work, d1)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["new_records"], 1)
            p = self._run("ingest", work, d2)
            self.assertEqual(p.returncode, 0, p.stderr)

            # Asking beyond the delivered G2 stream -> awaiting, exit 0.
            with open(req, "w") as fh:
                json.dump({"release": 1,
                           "positions": {"G1": 1, "G2": 3}}, fh)
            p = self._run("close", work, req)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)["status"], "awaiting")

            with open(req, "w") as fh:
                json.dump({"release": 1,
                           "positions": {"G1": 1, "G2": 2}}, fh)
            p = self._run("close", work, req, "--out", out_file)
            self.assertEqual(p.returncode, 0, p.stderr)
            envelope = json.loads(p.stdout)
            self.assertEqual(envelope["status"], "published")
            with open(out_file) as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk, envelope["package"])
            self.assertEqual(on_disk["changes"], [
                {"project": "P1", "owner": "G2", "conclusion": "fail"}])

            # Retry with the same release and positions -> identical package.
            p2 = self._run("close", work, req)
            self.assertEqual(json.loads(p2.stdout)["package"],
                             envelope["package"])

            # Rebinding release 1 rejects with exit 2.
            with open(req, "w") as fh:
                json.dump({"release": 1,
                           "positions": {"G1": 1, "G2": 0}}, fh)
            p3 = self._run("close", work, req)
            self.assertEqual(p3.returncode, 2)
            self.assertIn("REJECTED", p3.stderr)

            p4 = self._run("fetch", work, "1")
            self.assertEqual(p4.returncode, 0)
            self.assertEqual(json.loads(p4.stdout), envelope["package"])

            p5 = self._run("status", work)
            status = json.loads(p5.stdout)
            self.assertEqual(status["last_release"], 1)
            self.assertEqual(status["ownership"], {"P1": "G2"})

    def test_cli_rejects_bad_delivery_file(self):
        fx = SyncFixture()
        bad = os.path.join(fx.tmp, "bad.json")
        with open(bad, "w") as fh:
            json.dump({"records": [
                {"group": "G9", "position": 1,
                 "project": "P1", "action": "pass"}]}, fh)
        p = self._run("ingest", fx.workdir, bad)
        self.assertEqual(p.returncode, 2)
        self.assertIn("REJECTED", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
