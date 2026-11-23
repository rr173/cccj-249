"""Small, re-runnable end-to-end demo of the review-group sync tool.

Scenarios exercised against one tiny working directory (two groups, two
projects), all with in-process assertions so the script itself is a check:

1. initialize from an existing handover list -- only the ``complete``
   projects enter the sync scope, every group starts at position 0 with
   empty conclusions;
2. stream files arrive batched, out of order, re-sent and re-packaged;
3. a close against a gappy stream answers "awaiting" and advances nothing;
4. a one-sided transfer (only the outgoing leg in the window) is rejected,
   not partially applied;
5. the headline rule: after G1 transfers P1 out, a conclusion G1 still
   writes on its own stream can never take effect even though G2 confirmed
   the receipt -- the whole close is rejected;
6. a clean window publishes a change package; retrying the same release and
   positions returns the identical package while newer streams already sit
   in the inbox;
7. the next incremental release only processes the new positions.

Run it with:  python3 -I examples/proc_sync_demo.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "solver"))
import proc_sync  # noqa: E402


def conclusion(group, position, project, action):
    return {"group": group, "position": position,
            "project": project, "action": action}


def transfer(group, position, project, tid, frm, to, action):
    return {"group": group, "position": position, "project": project,
            "action": action, "transfer_id": tid,
            "from_group": frm, "to_group": to}


def write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def main():
    tmp = tempfile.mkdtemp(prefix="proc-sync-demo-")
    work = os.path.join(tmp, "work")
    print("working directory:", work)

    # -- 1. initialization from existing handover material -----------------
    handover = os.path.join(tmp, "handover.json")
    groups_file = os.path.join(tmp, "groups.json")
    ownership_file = os.path.join(tmp, "ownership.json")
    write(handover, {"projects": [
        {"project": "P1", "status": "complete", "items": []},
        {"project": "P2", "status": "complete", "items": []},
        {"project": "P9", "status": "incomplete",
         "missing_items": ["permit"]},       # not part of the sync scope
    ]})
    write(groups_file, {"groups": ["G1", "G2"]})
    write(ownership_file, {"ownership": {"P1": "G1", "P2": "G1"}})
    init = proc_sync.init_workdir(work, handover, groups_file, ownership_file)
    assert init["projects"] == ["P1", "P2"]                     # P9 excluded
    print("initialized scope:", init["projects"],
          "groups:", init["groups"])

    def ingest(name, records):
        path = os.path.join(tmp, name)
        write(path, {"records": records})
        return proc_sync.ingest_file(work, path)

    def close(release, positions):
        path = os.path.join(tmp, f"close-{release}.json")
        write(path, {"release": release, "positions": positions})
        return proc_sync.request_close(work, path)

    # -- 2. batched, out-of-order delivery plus an exact re-send -----------
    r = ingest("d1.json", [
        conclusion("G1", 3, "P1", "fail"),                      # gap at 2
        conclusion("G1", 1, "P2", "pass"),
    ])
    assert r["new_records"] == 2
    r = ingest("d1-resend.json", [                             # same records,
        conclusion("G1", 1, "P2", "pass"),                     # re-packaged
        conclusion("G1", 3, "P1", "fail"),
    ])
    assert r["new_records"] == 0 and r["duplicate_records"] == 2
    print("ingest: out-of-order accepted, re-send is a no-op")

    # -- 3. gaps -> awaiting, nothing advances, records stay ---------------
    outcome, payload = close(1, {"G1": 3, "G2": 0})
    assert outcome == "awaiting"
    assert payload["missing_positions"] == {"G1": [2]}
    assert proc_sync.load_state(work)["published"]["last_release"] == 0
    print("close with a gap -> awaiting", payload["missing_positions"])

    # -- 4. only the outgoing leg of a transfer is in the window -----------
    # Filling position 2 with the transfer-out; G2's receipt has not arrived.
    ingest("d2.json", [
        transfer("G1", 2, "P1", "T1", "G1", "G2", "transfer_out"),
    ])
    try:
        close(1, {"G1": 3, "G2": 0})
    except proc_sync.SyncError as exc:
        print("one-sided transfer rejected:", exc)
    else:  # pragma: no cover
        raise AssertionError("one-sided transfer must reject the close")
    assert proc_sync.load_state(work)["published"]["last_release"] == 0

    # -- 5. old-group conclusion written after transfer-out ----------------
    # G1's stream already carries a stale conclusion on P1 at position 3,
    # i.e. after its own outgoing leg at position 2.  G2 now confirms the
    # receipt at its position 1: the legs pair, but the old group's
    # post-transfer conclusion can never become effective.
    ingest("d3.json", [
        transfer("G2", 1, "P1", "T1", "G1", "G2", "transfer_in"),
    ])
    try:
        close(1, {"G1": 3, "G2": 1})
    except proc_sync.SyncError as exc:
        print("stale old-group conclusion rejected:", exc)
    else:  # pragma: no cover
        raise AssertionError("stale conclusion must reject the close")
    assert proc_sync.load_state(work)["published"]["last_release"] == 0

    # The valid window excluding the stale G1 position 3 can still publish;
    # G1's position 3 stays stored for later and never gets applied.
    outcome, pkg1 = close(1, {"G1": 2, "G2": 1})
    assert outcome == "published"
    assert pkg1["changes"] == [
        {"project": "P1", "owner": "G2", "conclusion": None},
        {"project": "P2", "owner": "G1", "conclusion": "pass"},
    ]
    print("release 1 package:")
    print(json.dumps(pkg1, indent=2, ensure_ascii=False))

    # -- 6. retry is idempotent even with newer streams already in ---------
    ingest("d4.json", [                                   # newer, not yet closed
        conclusion("G2", 2, "P1", "fail"),
    ])
    outcome, retried = close(1, {"G1": 2, "G2": 1})
    assert outcome == "published" and retried == pkg1
    print("retry of release 1 returns the identical package")

    # rebinding release 1 to other positions is refused
    try:
        close(1, {"G1": 2, "G2": 2})
    except proc_sync.SyncError as exc:
        print("rebinding release 1 rejected:", exc)
    else:  # pragma: no cover
        raise AssertionError("release numbers cannot be rebound")

    # -- 7. incremental release only scans the new positions ---------------
    outcome, pkg2 = close(2, {"G1": 2, "G2": 2})
    assert outcome == "published"
    assert pkg2["previous_positions"] == {"G1": 2, "G2": 1}
    assert pkg2["changes"] == [
        {"project": "P1", "owner": "G2", "conclusion": "fail"}]
    print("release 2 package:")
    print(json.dumps(pkg2, indent=2, ensure_ascii=False))

    # fetch regenerates the on-disk export even if it was lost.
    os.remove(proc_sync.package_path(work, 1))
    fetched = proc_sync.fetch_package(work, 1)
    assert fetched == pkg1
    status = proc_sync.workdir_status(work)
    assert status["last_release"] == 2
    assert status["ownership"] == {"P1": "G2", "P2": "G1"}
    print("final status:", json.dumps(status, indent=2, ensure_ascii=False))
    print("\nall demo assertions passed")


if __name__ == "__main__":
    main()
