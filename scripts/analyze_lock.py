"""Score the point locks in a dashboard log export (Protocol tab -> Export).

    python scripts/analyze_lock.py siyi-frames-1791339867563.json [more exports...]

For each app lock (rate or angle steering) it prints the siyi_sdk.tracking.metrics numbers,
recomputed from the per-frame ``app_lock`` records, next to the server's own
``lock_summary`` when the export has one. It then summarizes what can make a lock
twitch: event-loop stalls (``loop_lag``), the callbacks that caused them
(``slow_callback``), and late video frames.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from siyi_sdk.tracking.metrics import LockMetrics  # noqa: E402

COLUMNS = ("duration_s", "rms_deg", "p95_deg", "rms_all_deg", "reversals_per_s", "max_gap_ms", "events",
           "half_s", "settle_s", "unsettled_events", "overshoot_deg")


def percentile(values: list[float], fraction: float) -> float | None:
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))] if values else None


def split_locks(records: list[dict]) -> list[list[dict]]:
    """Group app_lock records by lock number, or by 2 s gaps in exports that predate it."""
    if all("lock" in r for r in records):
        groups: dict[int, list[dict]] = defaultdict(list)
        for r in records:
            groups[r["lock"]].append(r)
        return [groups[k] for k in sorted(groups)]
    locks: list[list[dict]] = []
    for r in records:
        if locks and r["t"] - locks[-1][-1]["t"] < 2.0:
            locks[-1].append(r)
        else:
            locks.append([r])
    return locks


def analyze(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tracking = data.get("tracking") or []
    print(f"== {path}")
    settings = data.get("settings") or {}
    print("settings:", {k: settings.get(k) for k in
                        ("lock_control", "lock_response", "command_delay_ms", "frame_delay_ms")})

    summaries = {r.get("lock"): r for r in tracking if r.get("event") == "lock_summary"}
    app = [r for r in tracking if r.get("event") == "app_lock"]
    for number, records in enumerate(split_locks(app), 1):
        metrics = LockMetrics()
        for r in records:
            metrics.add(r["t"], tuple(r["error_deg"]), tuple(r["command"]))
        score = metrics.summary()
        lock_id = records[0].get("lock", number)
        ages = [r["frame_age_ms"] for r in records]
        print(f"\nlock {lock_id} ({records[0]['control']}, {len(records)} frames, "
              f"source {Counter(r.get('source') for r in records).most_common(1)[0][0]})")
        for key in COLUMNS:
            line = f"  {key:18} {score[key]}"
            server = summaries.get(lock_id)
            if server is not None and key in server:
                line += f"   (server: {server[key]})"
            print(line)
        print(f"  frame_age_ms       p50 {percentile(ages, 0.5)}  p90 {percentile(ages, 0.9)}  "
              f"max {max(ages)}")
    if not app:
        print("\nno app_lock records (firmware locks, or an export from before they existed)")

    lag = [r for r in tracking if r.get("event") == "loop_lag"]
    if lag:
        print(f"\nloop lag: max {max(r['max_ms'] for r in lag)} ms, "
              f"windows with a stall over 50 ms: {sum(1 for r in lag if r['over_50ms'])}/{len(lag)}")
    slow = [r for r in tracking if r.get("event") == "slow_callback"]
    if slow:
        by_name: dict[str, list[float]] = defaultdict(list)
        for r in slow:
            by_name[r["what"]].append(r["ms"])
        print("slow callbacks (count, worst ms):")
        for name, values in sorted(by_name.items(), key=lambda kv: -len(kv[1]))[:10]:
            print(f"  {len(values):4}  {max(values):7.1f}  {name}")
    elif lag:
        print("slow callbacks: none recorded (older exports have none; otherwise stalls without one "
              "mean the whole process was starved)")
    stats = [r for r in tracking if r.get("event") == "video_stats"]
    if stats:
        print(f"video: worst decode {max(r['max_decode_ms'] for r in stats)} ms, "
              f"gaps over 200 ms {sum(r['gaps_over_200ms'] for r in stats)}, "
              f"bursts {sum(r['bursts'] for r in stats)}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for argument in sys.argv[1:]:
        analyze(argument)
