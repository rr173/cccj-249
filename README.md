# Rank certificates over truncated regional leaderboards

`roster.py` turns the material regions are allowed to publish — a score band, a
truncated ordered prefix, a tail score, and sparse manually audited cells — into

* a **best / worst possible rank for every candidate**,
* for each of the first `K` positions, the **unique candidate forced there** (or
  `null` when the position is not determined),
* the three top-K buckets **must be in / may be in / must not be in**,
* on request, a **complete legal score assignment** per named candidate that
  independently reproduces both ends of its rank interval.

It never guesses an unobserved score and never asks a region for more data. If
the supplied material is contradictory the request is rejected instead of being
patched.

## Model

Candidates `C` are fully known. For every region `r` and candidate `c` there is
one integer score `s[r][c] ∈ [low_r, high_r]`. A region may publish:

| field      | meaning |
|------------|---------|
| `prefix`   | the top `m` candidates in published order (descending score, ties by **ascending project code**) |
| `tail`     | the score of the *last* published candidate; it must equal that candidate's score |
| `verified` | sparse manually audited cells, which must agree with the board |

Rules enforced:

* no prefix ⇒ no tail constraint at all (`tail` must be `null`);
* an unlisted candidate has `s ≤ tail`, and `s = tail` is legal **only if its
  id sorts after the last published id** — this is exactly why tail ties depend
  on candidate ids;
* equal scores in the prefix are only legal when ids ascend, so a published
  order with descending ids forces a *strict* step (`s_i ≥ s_{i+1} + 1`);
* `S_c = Σ_r w_r · s[r][c]`, weights non-negative integers, composite ties by
  ascending project code.

Everything else about unobserved cells is unconstrained, so the certificate is
a statement about *all* material the regions could ever have, not about a guess.

## Why the bounds are exact

Region constraints are independent (no cross-region budgets or quotas), so:

1. Each cell has an exact feasible interval (`cell_bounds`), tightened by the
   prefix, the tail, verified cells and the mandatory strict steps.
2. Two candidates sharing a published prefix have their **difference** coupled
   by those strict steps. Every other pair couples only through the two
   marginal intervals.
3. A candidate can be pushed to its regional maximum while every other
   candidate is pushed to its conditional minimum — and mirrored for the
   minimum. So the per-pair difference extremes are attained together and each
   rank extreme is realised by one concrete assignment.

That last point is what makes a *witness* cheap: the assignment is constructed,
not searched.

## Complexity

```
bounds           O(Σ_r (|C| + m_r + |verified_r|))
rank extremes    O(|C| log |C| + Σ_r m_r²)
witness          O(|C| + Σ_r m_r)   per requested candidate
```

The marginal best/worst counts use sorted arrays and binary search; only pairs
that **share a prefix** get an individual correction. With 6 000 candidates and
3-entry prefixes the fast path evaluates 4 pair corrections in ~20 ms. There is
no `|C|²` comparison unless a published prefix itself is that long, and no
missing-score matrix is ever materialised. `rank_bounds_reference` keeps the
naive `O(|C|² · |R|)` version for cross-checking.

## Usage

```bash
python3 -I solver/roster.py instance.json --k 25 --witness C003,C035 --out cert.json
```

Exit status `2` means the request was rejected as contradictory.

```python
import roster
cert = roster.certificate(instance, K=25, witness_for=["C003"])
best, worst, bounds, lo, hi = roster.rank_bounds(instance)
```

Certificate shape:

```json
{
  "K": 25,
  "candidate_count": 400,
  "per_candidate": {"C003": {"best_rank": 3, "worst_rank": 17,
                             "composite_min_possible": 221,
                             "composite_max_possible": 268}},
  "top_k": {
    "positions": [{"position": 1, "certain_holder": null,
                   "candidates_forced_to_this_position": []}],
    "must_be_in_top_k": [], "may_be_in_top_k": [], "cannot_be_in_top_k": []
  },
  "witnesses": {"C003": {"best": {...}, "worst": {...}}}
}
```

Each witness carries the full per-region score table, the composite scores, the
resulting order and the recomputed rank, so a caller can validate it without
this library. `best` and `worst` may use different global assignments.

Note that a candidate can be **certainly in the top K while its position is not
certain** (`best ≤ K < worst`): it is listed under `must_be_in_top_k` and no
position claims it.

## Examples (small, fully enumerable)

| script | shows |
|---|---|
| `examples/tail_tie.py` | a tail tie is legal for `C07` but not for `C02`, because ids decide |
| `examples/verified_evidence.py` | one manual cell outside the prefix tightens ranks; a contradicting cell is rejected |
| `examples/interval_overlap.py` | overlapping bands with no unique holder, `must/may/cannot`, two witnesses for one candidate |
| `examples/illegal_lower_bound.py` | a lower bound already sitting on the tail is infeasible and rejected; one point of slack is accepted |

Each example enumerates its own tiny table from first principles and asserts
that the solver's answers match.

## Large example

`large/build_large.py` builds a 400-candidate, 5-region roster with prefixes of
80 and a few manual cells, then checks only the hand-derivable endpoints (tail
cells pinned, forced strict steps, unlisted caps) and reports the top-K counts.
It writes `large/large_instance.json`, which the test suite also consumes. No
global enumeration is performed anywhere.

## Tests

```bash
python3 -I -m unittest discover -s tests -v
```

The suite cross-checks cell bounds, pairwise difference extremes, rank bounds
(fast vs. reference vs. exhaustive enumeration), witness legality and
recomputability, the certificate's bucket/position invariants, rejection of
contradictory material, and the scale properties above.

# Handover lists for the next review round

`solver/handover.py` consumes the **existing** rank certificate (it never
recomputes a rank and never changes the certificate interface) together with a
material ledger, and emits the JSON handover list for the projects that are
**certain** to be shortlisted. A project whose exact position is uncertain but
whose inclusion is certain is handed over (it sits in
`top_k.must_be_in_top_k`); a project that can only possibly make the cut
(`may_be_in_top_k`) is never handed over.

## Running it

```bash
python3 -I solver/handover.py CERT.json LEDGER.json \
  --business-cutoff 100 --intake-cutoff 50 --out handover.json
```

Both cutoffs are inclusive integers. Exit status `2` means the inputs were
rejected (bad structure, dangling version reference, contradictory status
records, …) and **no** partial list is written.

## Ledger format

```json
{
  "ledger_id": "demo-ledger-001",
  "required_items": ["business_license", "permit", "financial_report"],
  "catalog": {
    "L1": {"kind": "original"},
    "D1": {"kind": "derived", "basis_groups": [["L1", "L2"], ["L3", "D2"]]}
  },
  "projects": {
    "P1": {"business_license": ["D3", "D1"], "permit": ["Q1"]}
  },
  "status_log": [
    {"version": "L1", "effective_at": 10, "received_at": 5, "state": "valid"},
    {"version": "L1", "effective_at": 40, "received_at": 20, "state": "revoked"}
  ]
}
```

| section | meaning |
|---|---|
| `required_items` | the mandatory material templates; order is the reporting order |
| `catalog` | every version defined exactly once. `original` versions are issued directly; `derived` versions list one or more non-empty `basis_groups` |
| `projects` | per project and per mandatory item, a **priority-ordered** candidate version list (may be omitted). References name an exact version — nothing is ever swapped for a later submission of the same material |
| `status_log` | `effective_at` (business time) and `received_at` (intake time) per version, each `valid` or `revoked`; the log may be shuffled and repeated |

A basis group is a conjunction (every member must hold) and the list of groups
is a disjunction (any one group suffices); group order breaks ties between
equally deep grounds.

### Which status record governs

A record is usable only when `effective_at <= business_cutoff` **and**
`received_at <= intake_cutoff`. Among usable records of one version the most
recent `effective_at` wins, then the latest `received_at`; exact duplicate
records count once. If visible records with the same two times carry opposite
states the whole run is rejected. A version with no usable record, or whose
governing record is `revoked`, is invalid and cannot ground anything.

### What "holds" means

A derived version holds only when one of its groups is fully grounded and the
chain reaches **valid originals** in finitely many steps. Mutual recognition
may form cycles, but a cycle whose members never reach a valid original proves
nothing and never holds. When several groups hold, the chosen one has the
shortest longest trace chain (`ground_depth`); equal depth resolves by catalog
group order. A status correction invalidates the version and the materials that
actually depend on it; an independently grounded alternative group survives.

### Per-project decision and output

For each certain project and each mandatory item the first **holding** version
in candidate priority order is selected. When every item is present the project
is `complete` and its selected versions are listed; otherwise it is
`incomplete`, reports only `missing_items`, and hands over no partial
materials. The output carries the SHA-256 of the exact certificate bytes, both
cutoffs, and a shared `versions` section: each referenced version appears once
with the governing status record and the selected direct basis ids, so the
grounds can be checked level by level with no cycles and no unrelated versions.

## Why it scales

The catalog is resolved **once, globally**, before any project is inspected, as
a least fixpoint that only re-examines groups whose members changed in the
previous round (bounded by the longest real evidence chain). Per project the
work is then just a walk over its short candidate lists; the shared graph is
never traversed per project and never expanded into per-project paths, and the
`versions` section stays bounded by the catalog size.

## Handover examples and tests

| artifact | shows |
|---|---|
| `examples/handover_demo.py` | builds a real certificate + ledger covering revoked records, cutoff invisibility, multi-level chains, a multi-group shortest-chain choice, an empty endorsement cycle, complete vs. incomplete projects, and possible-only exclusion; re-runnable with assertions |
| `large/build_large_handover.py` | generator for an 8 000-project batch over one shared 12-level catalog; asserts the cycle never holds, corrections route around independent groups, and the detail size is independent of project count |
| `tests/test_handover.py` | 2000-case fuzz of catalog resolution against an independent recursive reference, plus status, selection, validation, CLI and scale tests |
