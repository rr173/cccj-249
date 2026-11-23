"""Counter-example: a legal-looking region whose lower bound cannot reach the
tail it published.

R0 publishes C02 > C01 with tail 4.  The two published ids descend
(2 -> 1), so the board forces at least one strict step: C02 >= C01 + 1 = 5.
But the region claims its scores live in [4, 4].  No row can satisfy both, so
the request must be rejected rather than patched by "lowering" a score.

A second variation keeps a feasible lower bound but raises the tail above the
region ceiling -- also rejected, for a different reason.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "solver"))
import roster  # noqa: E402

# Region lower bound already sits on the tail, yet the prefix needs a step up.
LOWER_BOUND_ON_TAIL = {
    "candidates": ["C01", "C02", "C03"],
    "regions": {
        "R0": {"low": 4, "high": 4, "prefix": ["C02", "C01"], "tail": 4, "verified": {}}
    },
    "weights": {"R0": 1},
}

# One point of slack is enough: [4, 5] admits C01 = 4 and C02 = 5.
ONE_POINT_OF_SLACK = copy.deepcopy(LOWER_BOUND_ON_TAIL)
ONE_POINT_OF_SLACK["regions"]["R0"]["high"] = 5

# A tail above the region's own ceiling is independently illegal.
TAIL_ABOVE_CEILING = {
    "candidates": ["C01", "C02"],
    "regions": {
        "R0": {"low": 0, "high": 3, "prefix": ["C01"], "tail": 5, "verified": {}}
    },
    "weights": {"R0": 1},
}

# A verified cell that contradicts the published tail on the last entry.
VERIFIED_VS_TAIL = {
    "candidates": ["C01", "C02"],
    "regions": {
        "R0": {"low": 0, "high": 5, "prefix": ["C02", "C01"], "tail": 2,
               "verified": {"C01": 3}}
    },
    "weights": {"R0": 1},
}


def reject(inst, label):
    try:
        roster.certificate(inst, K=1)
    except roster.Contradiction as exc:
        print(f"{label}: REJECTED -- {exc}")
        return
    raise AssertionError(f"{label} was accepted but is infeasible")


def main():
    reject(LOWER_BOUND_ON_TAIL, "lower bound on tail")
    reject(TAIL_ABOVE_CEILING, "tail above ceiling")
    reject(VERIFIED_VS_TAIL, "verified vs tail")

    # The feasible variant is accepted, and its one legal row is found.
    bounds = roster.validate(ONE_POINT_OF_SLACK)
    # C03 is unlisted with an id above C01's, so it may tie the tail at 4; the
    # ceiling of 5 is still unreachable for it because the top slot is taken.
    assert bounds["R0"] == {"C01": (4, 4), "C02": (5, 5), "C03": (4, 4)}
    cert = roster.certificate(ONE_POINT_OF_SLACK, K=2, witness_for=["C02"])
    print("slack variant accepted; bounds:", bounds["R0"])
    print("rank extremes:", {
        c: (v["best_rank"], v["worst_rank"])
        for c, v in cert["per_candidate"].items()
    })

    # Sanity: the prefix really is forced strictly increasing here.
    assert bounds["R0"]["C02"][0] > bounds["R0"]["C01"][1]


if __name__ == "__main__":
    main()
