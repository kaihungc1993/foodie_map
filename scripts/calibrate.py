"""Report the evidence needed to set per-account rating tiers.

Two reviewers do not share a scale. On the venues both have reviewed,
jc_foodidi scored lower on every single one — so applying born2eat's thresholds
to his ratings would push almost all his restaurants into the bottom tiers and
make the top tier unreachable for him.

This script only *reports*. A human reads data/calibration_report.json and edits
config/rating_calibration.json, the same way merge and rename suggestions work.
Cut points are never derived at build time, because recomputing them from a live
distribution would repaint the map every month.
"""
import argparse
import collections
import statistics
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

# born2eat's hand-set cuts sit at these percentiles of his own ratings, so the
# same shape is the natural starting proposal for any other reviewer.
TARGET_PERCENTILES = [77.7, 58.3, 38.0, 22.3]
MIN_TIER_SHARE = 0.03     # a tier holding under 3% is not worth a colour
MAX_TIER_SHARE = 0.45     # one holding over 45% means the lattice cannot split


def ratings_by_account(restaurants):
    out = {}
    for r in restaurants:
        for acc, rev in (r.get("reviews") or {}).items():
            if rev.get("rating") is not None:
                out.setdefault(acc, []).append(rev["rating"])
    return {a: sorted(v) for a, v in out.items()}


def propose_cuts(values):
    """Cut points that would put ~the same share of this reviewer's restaurants
    in each tier as born2eat has in his."""
    if not values:
        return []
    cuts = []
    for pct in sorted(TARGET_PERCENTILES, reverse=True):
        idx = min(len(values) - 1, int(round(len(values) * (1 - pct / 100.0))))
        cuts.append(values[idx])
    # A coarse rating lattice can land two cuts on the same value; collapsing
    # them means that reviewer simply gets fewer tiers.
    return sorted(set(cuts))


def tier_counts(values, cuts):
    counts = [0] * (len(cuts) + 1)
    for v in values:
        tier = 0
        for c in cuts:
            if v >= c:
                tier += 1
        counts[tier] += 1
    return counts


def overlap_table(restaurants, a, b):
    rows = []
    for r in restaurants:
        rv = r.get("reviews") or {}
        ra, rb = (rv.get(a) or {}).get("rating"), (rv.get(b) or {}).get("rating")
        if ra is not None and rb is not None:
            rows.append({"name": r["name"], "id": r["id"],
                         a: ra, b: rb, "delta": round(rb - ra, 2)})
    return sorted(rows, key=lambda x: x["delta"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="write the report file")
    args = ap.parse_args()

    data = common.read_json(common.RESTAURANTS, {})
    restaurants = data.get("restaurants", [])
    by_acc = ratings_by_account(restaurants)
    frozen = common.read_json(common.CONFIG / "rating_calibration.json", {})
    frozen_accounts = frozen.get("accounts", {})

    report = {"accounts": {}, "overlaps": {}}
    for acc, values in sorted(by_acc.items()):
        proposed = propose_cuts(values)
        current = (frozen_accounts.get(acc) or {}).get("cuts")
        counts = tier_counts(values, current or proposed)
        share = [round(c / len(values), 3) for c in counts]
        warnings = []
        if any(s < MIN_TIER_SHARE for s in share):
            warnings.append("a tier holds under 3% — consider fewer tiers")
        if any(s > MAX_TIER_SHARE for s in share):
            # Not necessarily fixable: if a single rating value is held by more
            # than 45% of a reviewer's restaurants, no cut point can split it.
            top_value = collections.Counter(values).most_common(1)[0]
            warnings.append(
                f"a tier holds over 45% — the single value {top_value[0]} covers "
                f"{100 * top_value[1] / len(values):.1f}% of his ratings"
                + (", so this is his rating habit, not a bad cut"
                   if top_value[1] / len(values) > MAX_TIER_SHARE else ""))
        report["accounts"][acc] = {
            "rated": len(values),
            "distinct_values": len(set(values)),
            "min": values[0], "max": values[-1],
            "median": statistics.median(values),
            "proposed_cuts": proposed,
            "frozen_cuts": current,
            "tier_counts": counts,
            "tier_share": share,
            "warnings": warnings,
        }
        print(f"@{acc}: {len(values)} rated, {len(set(values))} distinct, "
              f"{values[0]}–{values[-1]}, median {statistics.median(values)}")
        print(f"   proposed cuts {proposed}   frozen {current}")
        print(f"   tier counts {counts}  share {share}")
        for w in warnings:
            print(f"   WARNING: {w}")

    accs = sorted(by_acc)
    for i in range(len(accs)):
        for j in range(i + 1, len(accs)):
            a, b = accs[i], accs[j]
            rows = overlap_table(restaurants, a, b)
            report["overlaps"][f"{a} vs {b}"] = rows
            if not rows:
                continue
            deltas = [r["delta"] for r in rows]
            lower = sum(1 for d in deltas if d < 0)
            print(f"\noverlap {a} vs {b}: {len(rows)} venues both rated")
            print(f"   mean delta {statistics.mean(deltas):+.2f}   "
                  f"median {statistics.median(deltas):+.2f}")
            print(f"   {b} lower on {lower}/{len(rows)}")

    if args.report:
        common.write_json(common.DATA / "calibration_report.json", report)
        print("\nwrote data/calibration_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
