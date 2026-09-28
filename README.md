# appdaemon_predbat_guard

Notices when Predbat stops actually controlling the inverter.

Implements backlog **H-44d** (Modbus failure alerting) and **H-44f** (remote
override detection). Replaces a `cron`-driven watch that only ran while a Claude
session happened to be idle — on the night of 2026-09-27 it fired thirteen queued
prompts at once instead of hourly, which is not monitoring.

## Why

On 2026-09-26 Predbat was given charge control of the Huawei inverter (H-44e).
Before that it had run read-only for months: it drew a plan, published ~297
entities, and wrote nothing. `predbat.inverter_register_writes` read **0** — not
"few", zero, for its entire life. Everything in its UI was a simulation, and
nobody noticed, because a read-only Predbat looks exactly like a working one.

That is the failure this app exists to catch. The inverter is now driven across
a Modbus link that times out most nights, and a *write* that silently fails
leaves it holding whatever the previous slot set while Predbat's plan says
otherwise. Nothing announces it. The symptom is the electricity bill.

## What it watches

| rule | fires when |
|---|---|
| `write_stall` | `inverter_register_writes` static **while the plan has an active charge/export window** |
| `baseline_drift` | a watched inverter setting no longer matches `expected_settings`, or has gone missing |
| `modbus_burst` | ≥3 Modbus failures within 15 minutes |
| `modbus_rate` | >3 in a rolling 24h, against a measured baseline of 1–2/day |

### Why there is no time-of-day window

The first version of H-44d asserted the timeouts were the inverter's 00:00–01:00
nightly housekeeping and proposed alerting on failures *outside* that window.
That was fitted to three days of data. Nine days showed the first timeout of each
day drifting roughly **+23 minutes per day**:

```
09-22 20:19 | 09-23 20:39 | 09-24 21:13 | 09-25 21:29 | 09-26 21:53 | 09-27 22:16
```

Not anchored to wall-clock time, so any fixed window is wrong within a week.
Every rule here is time-independent.

### Why `write_stall` requires an active window

Measured on the night of 2026-09-28, `inverter_register_writes` sat at **34 for
three consecutive hours** (02:29–04:29) and then resumed 37 → 43 → 45. The stall
was benign: a full battery with nothing to do. A bare "the counter did not move"
alarm would have paged at 03:00. The rule therefore requires *both* a static
counter and a plan that wants action. `test_stall_outside_window_is_silent` is
that night, pinned.

## The Modbus detection caveat

HA logs `Timeout communicating with TA21X0123456` when the huawei_solar
coordinator fails, but that log is **not reachable from inside the AppDaemon
container** without a token and a mount. So this app listens for
`availability_entity` going `unavailable`, which is what a coordinator failure
produces.

**That is a proxy, not the log line.** It will also fire on an integration reload
or an HA restart, so the counts are an upper bound. They feed the burst and rate
rules, which are context. The rule that matters — `write_stall` — does not depend
on them at all.

## Files

| file | role |
|---|---|
| `i1_predbat_guard.py` | the AppDaemon app: I/O, config validation, alerting |
| `predbat_rules.py` | the detection rules, free of AppDaemon and of any clock — every input including `now` is passed in, so each rule is directly testable |
| `notification_policy.py` | policy D2, **copied byte-identically** from `appdaemon_watchdog`. Do not edit this copy; re-copy from the canonical one. `tests/test_policy_copies_match.py` enforces it |
| `config.yaml.example` | committed configuration reference; the live `config.yaml` is gitignored |

Tests live in `/code/hass/tests/test_predbat_guard_rules.py`, per PROJECT_RULES §1.

## Policy compliance

- **D1** — config is validated in `initialize()` and invalid config `raise`s, so
  the app shows failed in the admin console rather than looking healthy and doing
  nothing. Missing `notify_targets`, missing `expected_settings`, a missing
  `availability_entity`, an interval under 60s, or a watched entity that does not
  exist all refuse to start. The T-04 dead-hardware carve-out deliberately does
  **not** apply: none of these entities are hardware, and a guard bound to a
  missing entity reports "all clear" forever.
- **D2** — first occurrence always sends whatever the hour; repeats every 6h;
  held during 22:00–07:00 without consuming the repeat.
- **§5 clocks** — `self.get_now()` for schedule-relative work; no naive
  `datetime.now()`.

## Deploying

`/conf` is not mounted in this working directory, so nothing deploys from here.
Copy the repo to `/conf/apps/appdaemon_predbat_guard/`, write a live
`config.yaml` from the example, and confirm with `tools/appdaemon-drift.sh`.

**Note the drift tool compares checkouts and cannot see module shadowing**, which
is a property of AppDaemon's import path and has bitten this project twice. This
repo ships `notification_policy.py`, a name six other repos also ship — that is
why the byte-identical rule exists.
