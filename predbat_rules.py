"""The detection rules for the Predbat/inverter guard, free of AppDaemon.

Every function here takes its inputs explicitly — including `now` — so each rule
is directly testable without a clock, a network, or a running AppDaemon. The app
module does the I/O; this module decides.

Why these rules and not a time window
-------------------------------------
The first version of backlog H-44d asserted that the Huawei Modbus timeouts were
the inverter's 00:00-01:00 nightly housekeeping, and proposed alerting on
failures *outside* that window. That was fitted to three days of data. Nine days
showed the first timeout of each day drifting roughly +23 minutes per day:

    09-22 20:19 | 09-23 20:39 | 09-24 21:13 | 09-25 21:29 | 09-26 21:53 | 09-27 22:16

Not anchored to wall-clock time at all, so any fixed window is wrong within a
week. These rules are therefore time-independent.

Measured baseline (2026-09-20 .. 2026-09-28): 19 timeouts over 9 days, so 1-2 per
day arriving in ones and twos, plus one burst of 6 inside 10 minutes on 09-20.

The rule that actually matters
------------------------------
A *read* timeout is cosmetic — the next poll fixes it. A *write* that silently
fails while Predbat believes it acted is the real risk, because the inverter is
then left holding whatever the previous slot set. `write_stall` is that check.

It is deliberately conditional on the plan wanting action. Measured on the night
of 2026-09-28, `predbat.inverter_register_writes` sat at 34 for three consecutive
hours (02:29-04:29) and then resumed 37 -> 43 -> 45. That stall was benign: a full
battery with nothing to do. A naive "the counter did not move" alarm would have
paged for it at 03:00.
"""

from typing import Any, Iterable, Mapping, Sequence

# Measured 2026-09-20..09-28: 1-2 timeouts/day normally, one burst of 6 in 10 min.
DEFAULT_BURST_COUNT = 3
DEFAULT_BURST_WINDOW = 900  # 15 minutes, seconds
DEFAULT_RATE_MAX = 3  # per rolling 24h; baseline is 1-2
DEFAULT_RATE_WINDOW = 86400

BURST = "modbus_burst"
RATE = "modbus_rate"
WRITE_STALL = "write_stall"
BASELINE_DRIFT = "baseline_drift"


def find_burst(
    timestamps: Sequence[float],
    now: float,
    count: int = DEFAULT_BURST_COUNT,
    window: float = DEFAULT_BURST_WINDOW,
) -> dict[str, Any] | None:
    """Return a finding if `count` timeouts fall inside any `window`-second span.

    Looks only at the most recent `window` seconds: an old burst is history, not
    news, and re-reporting it on every scan would be noise.
    """
    recent = sorted(t for t in timestamps if now - t <= window)
    if len(recent) < count:
        return None
    return {
        "rule": BURST,
        "count": len(recent),
        "window_s": window,
        "first": recent[0],
        "last": recent[-1],
        "detail": (
            f"{len(recent)} Modbus timeouts within {int(window / 60)} min "
            f"(threshold {count}) — the link is degrading, not the daily blip"
        ),
    }


def find_rate(
    timestamps: Sequence[float],
    now: float,
    maximum: int = DEFAULT_RATE_MAX,
    window: float = DEFAULT_RATE_WINDOW,
) -> dict[str, Any] | None:
    """Return a finding if the rolling-24h timeout count exceeds the baseline."""
    recent = [t for t in timestamps if now - t <= window]
    if len(recent) <= maximum:
        return None
    return {
        "rule": RATE,
        "count": len(recent),
        "window_s": window,
        "detail": (
            f"{len(recent)} Modbus timeouts in {int(window / 3600)}h "
            f"(baseline 1-2/day, threshold {maximum})"
        ),
    }


def plan_wants_action(windows: Iterable[Mapping[str, float]], now: float) -> bool:
    """True if `now` falls inside any planned charge or export window.

    `windows` are dicts carrying epoch `start` and `end`, as the app builds them
    from Predbat's `best_charge_window` / `best_export_window`.
    """
    return any(w["start"] <= now < w["end"] for w in windows)


def write_stall(
    writes_now: int,
    writes_before: int,
    windows: Iterable[Mapping[str, float]],
    now: float,
) -> dict[str, Any] | None:
    """Return a finding only if the write counter is static while the plan is acting.

    Both halves are required. A static counter with no active window is the
    normal idle case and must never alert — see the module docstring for the
    three-hour benign stall this guards against.
    """
    if writes_now != writes_before:
        return None
    if not plan_wants_action(windows, now):
        return None
    return {
        "rule": WRITE_STALL,
        "writes": writes_now,
        "detail": (
            f"inverter_register_writes stuck at {writes_now} while a charge/export "
            f"window is active — Predbat believes it acted; the inverter may not have"
        ),
    }


def baseline_drift(
    expected: Mapping[str, str],
    actual: Mapping[str, str | None],
) -> dict[str, Any] | None:
    """Return a finding if any watched setting no longer matches the baseline.

    This is H-44f: the grid operator can push settings to the inverter and
    nothing announces it, so the only detection is comparison. A setting that has
    gone *missing* counts as drift — an entity that stopped existing is not
    reassurance.
    """
    changed = []
    for key in sorted(expected):
        want = str(expected[key])
        got = actual.get(key)
        if got is None:
            changed.append((key, want, "MISSING"))
        elif str(got) != want:
            changed.append((key, want, str(got)))
    if not changed:
        return None
    lines = ", ".join(f"{k}: {w} -> {g}" for k, w, g in changed)
    return {
        "rule": BASELINE_DRIFT,
        "changed": changed,
        "detail": f"{len(changed)} inverter setting(s) changed since baseline — {lines}",
    }


def evaluate(
    timeouts: Sequence[float],
    writes_now: int,
    writes_before: int,
    windows: Iterable[Mapping[str, float]],
    expected: Mapping[str, str],
    actual: Mapping[str, str | None],
    now: float,
    burst_count: int = DEFAULT_BURST_COUNT,
    burst_window: float = DEFAULT_BURST_WINDOW,
    rate_max: int = DEFAULT_RATE_MAX,
    rate_window: float = DEFAULT_RATE_WINDOW,
) -> list[dict[str, Any]]:
    """Run every rule and return the findings, most serious first.

    Order is deliberate: a write stall means the inverter may be mis-set right
    now, drift means somebody else changed it, and the Modbus counts are context.
    """
    windows = list(windows)
    findings = [
        write_stall(writes_now, writes_before, windows, now),
        baseline_drift(expected, actual),
        find_burst(timeouts, now, count=burst_count, window=burst_window),
        find_rate(timeouts, now, maximum=rate_max, window=rate_window),
    ]
    return [f for f in findings if f is not None]
