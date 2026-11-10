"""Predbat guard — notices when Predbat stops actually controlling the inverter.

Why this exists
---------------
On 2026-09-26 Predbat was given charge control of the Huawei inverter (backlog
H-44e). Before that it had run in read-only mode for months: it drew a plan,
published ~297 entities, and wrote nothing — `predbat.inverter_register_writes`
read **0**, not "few", zero, for its entire life. Everything in its UI was a
simulation and nobody noticed, because a read-only Predbat looks exactly like a
working one.

That is the failure this app exists to catch. The inverter is now driven across
a Modbus link that times out most nights, and a *write* that silently fails
leaves the inverter holding whatever the previous slot set while Predbat's plan
says otherwise. Nothing announces it; the symptom is the electricity bill.

It also carries H-44f: the grid operator can push settings to this inverter
remotely, and again nothing announces it. The only detection is comparison
against a recorded baseline.

What it watches
---------------
* **Write stall** — `inverter_register_writes` static while Predbat's own plan
  has an active charge or export window. The conjunction matters: measured on
  2026-09-28 the counter sat at 34 for three hours and then resumed, which was a
  full battery with nothing to do. See `predbat_rules` for why a bare
  "counter did not move" check would have paged at 03:00.
* **Baseline drift** — watched inverter settings no longer matching
  `expected_settings`. A setting that has gone missing counts as drift.
* **Modbus burst / rate** — degradation of the link itself, against a measured
  baseline of 1-2 timeouts a day.

How Modbus failures are detected, and the honest caveat
-------------------------------------------------------
HA logs `Timeout communicating with TA21X0123456` when the huawei_solar
coordinator fails, but that log is not reachable from inside the AppDaemon
container without a token and a mount. Instead this app listens for the watched
Huawei entity going `unavailable`, which is what a coordinator failure produces.

**That is a proxy, not the log line.** It will also fire if the entity becomes
unavailable for another reason — an integration reload, a HA restart. The counts
are therefore an upper bound on Modbus timeouts. They are used for the burst and
rate rules, which are context; the rule that matters (write stall) does not
depend on them at all.

Policy
------
D1: config is validated in `initialize()` and invalid config raises, so the app
shows failed in the admin console rather than looking healthy and doing nothing.
D2: alerting goes through `notification_policy` — first occurrence always sends,
repeats every 6h, held during quiet hours without consuming the repeat.
"""

import json
import os
import re
import time
from typing import Any

import appdaemon.plugins.hass.hassapi as hass

import irisone_bus
import notification_policy as policy
import predbat_rules as rules

# Owner, IQ-22 2026-10-29: money is being lost now, and it can be fixed remotely
# -> urgent. Burst and rate are early warnings of a degrading link; if it
# degrades far enough, WRITE_STALL fires anyway -> non-urgent.
URGENT_RULES = frozenset({rules.WRITE_STALL, rules.BASELINE_DRIFT})

HA_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", ""})

DEFAULT_INTERVAL = 300
DEFAULT_CHANNEL = "predbat_guard_alerts"
DEFAULT_PRIORITY = "high"
DEFAULT_STATE_FILE = "/conf/predbat_guard_state.json"
DEFAULT_WRITES_ENTITY = "predbat.inverter_register_writes"
DEFAULT_STATUS_ENTITY = "predbat.status"

# Bump when the state-file shape changes and write a migration in load_state.
# A format change that silently loads an old file as empty disarms the guard.
STATE_VERSION = 1

_WINDOW_RE = re.compile(
    r"\{'start': (\d+), 'end': (\d+), 'average': ([\d.]+), 'target': ([\d.]+)\}"
)


def parse_plan_windows(debug: str, day_start_epoch: float) -> list[dict[str, float]]:
    """Turn Predbat's `debug` attribute into epoch-bounded windows.

    Predbat expresses window bounds as minutes from local midnight, and they run
    past 1440 into following days, so they are offsets rather than times of day.
    """
    out: list[dict[str, float]] = []
    for start, end, average, target in _WINDOW_RE.findall(debug or ""):
        out.append({
            "start": day_start_epoch + int(start) * 60,
            "end": day_start_epoch + int(end) * 60,
            "ore": float(average),
            "target_kwh": float(target),
        })
    return out


class PredbatGuard(hass.Hass):
    """Watches that Predbat is still really driving the inverter."""

    def initialize(self) -> None:
        # A-71.5: an older shared module winning AppDaemon's import path must stop
        # the app here, where the watchdog sees it, not at the first alert.
        if not hasattr(policy, "should_notify") or not hasattr(irisone_bus, "presence_for_target"):
            raise RuntimeError(f"stale shared module on the import path: "
                               f"{policy.__file__}, {irisone_bus.__file__}")
        # (rule, target) -> presence `since` of the last logged drop (O3).
        self._drop_logged: dict = {}
        self.notify_targets = self._require_list("notify_targets")
        self.expected_settings = self._require_mapping("expected_settings")

        self.interval = int(self.args.get("interval", DEFAULT_INTERVAL))
        if self.interval < 60:
            raise ValueError(
                f"interval must be at least 60s, got {self.interval}. Predbat "
                "replans every ~5 min; polling faster only adds load."
            )

        self.writes_entity = self.args.get("writes_entity", DEFAULT_WRITES_ENTITY)
        self.status_entity = self.args.get("status_entity", DEFAULT_STATUS_ENTITY)
        self.availability_entity = self.args.get("availability_entity")
        if not self.availability_entity:
            raise ValueError(
                "availability_entity is required: it is the Huawei entity whose "
                "transition to 'unavailable' stands in for a Modbus coordinator "
                "failure. Without it the burst and rate rules cannot run."
            )

        self.channel = self.args.get("notification_channel", DEFAULT_CHANNEL)
        self.priority = self.args.get("notification_priority", DEFAULT_PRIORITY)
        self.quiet_start = int(self.args.get("quiet_start", policy.DEFAULT_QUIET_START))
        self.quiet_end = int(self.args.get("quiet_end", policy.DEFAULT_QUIET_END))
        self.repeat_after = int(self.args.get("repeat_after", policy.DEFAULT_REPEAT_AFTER))
        self.state_file = self.args.get("state_file", DEFAULT_STATE_FILE)

        self.burst_count = int(self.args.get("burst_count", rules.DEFAULT_BURST_COUNT))
        self.burst_window = int(self.args.get("burst_window", rules.DEFAULT_BURST_WINDOW))
        self.rate_max = int(self.args.get("rate_max", rules.DEFAULT_RATE_MAX))
        self.rate_window = int(self.args.get("rate_window", rules.DEFAULT_RATE_WINDOW))

        # Entity existence is a config error here, not a tolerated outage: this app
        # exists to detect silence, so it must not itself fail silently. The T-04
        # dead-hardware carve-out does not apply — none of these are hardware.
        for entity in (self.writes_entity, self.status_entity, self.availability_entity):
            if not self.entity_exists(entity):
                raise ValueError(
                    f"entity {entity!r} does not exist. Refusing to start: a guard "
                    "bound to a missing entity reports 'all clear' forever."
                )

        state = self.load_state()
        self.timeouts: list[float] = state.get("timeouts", [])
        self.last_writes: int | None = state.get("last_writes")
        # When the write counter last moved. Unknown after a restart: start the
        # clock now, so a window already under way is not judged blind.
        self.last_write_change: float = state.get("last_write_change") or time.time()
        self.predbat_controlled = list(self.args.get(
            "predbat_controlled_settings", rules.DEFAULT_PREDBAT_CONTROLLED))
        self.sent: dict[str, float] = state.get("sent", {})

        self.listen_state(
            self.on_availability, self.availability_entity, new="unavailable"
        )
        self.run_every(self.scan, "now+30", self.interval)
        self.log(
            f"predbat guard up: watching {self.writes_entity}, "
            f"{len(self.expected_settings)} baseline setting(s), "
            f"Modbus proxy {self.availability_entity}, every {self.interval}s"
        )

    # ---------------------------------------------------------------- config

    def _require_list(self, key: str) -> list[str]:
        value = self.args.get(key)
        if not value or not isinstance(value, list):
            raise ValueError(
                f"{key} is required and must be a non-empty list. A guard that "
                "cannot tell anyone is worse than no guard: it looks like coverage."
            )
        return [str(v) for v in value]

    def _require_mapping(self, key: str) -> dict[str, str]:
        value = self.args.get(key)
        if not value or not isinstance(value, dict):
            raise ValueError(
                f"{key} is required and must be a non-empty mapping of "
                "entity_id -> expected value."
            )
        return {str(k): str(v) for k, v in value.items()}

    # ----------------------------------------------------------------- state

    def load_state(self) -> dict[str, Any]:
        try:
            with open(self.state_file) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.log(f"state file unreadable ({exc}); starting clean", level="WARNING")
            return {}
        if data.get("version") != STATE_VERSION:
            self.log(
                f"state file version {data.get('version')} != {STATE_VERSION}; "
                "starting clean rather than misreading it",
                level="WARNING",
            )
            return {}
        return data

    def save_state(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "timeouts": self.timeouts[-500:],
            "last_writes": self.last_writes,
            "last_write_change": self.last_write_change,
            "sent": self.sent,
        }
        tmp = f"{self.state_file}.tmp"
        try:
            with open(tmp, "w") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            self.log(f"could not persist state: {exc}", level="ERROR")

    # ---------------------------------------------------------------- inputs

    def on_availability(self, entity, attribute, old, new, kwargs) -> None:
        """A watched Huawei entity went unavailable — treat as a coordinator failure."""
        self.timeouts.append(time.time())
        self.save_state()

    def read_int(self, entity: str) -> int | None:
        raw = self.get_state(entity)
        if raw is None or raw in HA_UNAVAILABLE_STATES:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def current_settings(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for entity in self.expected_settings:
            raw = self.get_state(entity)
            out[entity] = None if raw in HA_UNAVAILABLE_STATES else raw
        return out

    def plan_windows(self) -> list[dict[str, float]]:
        debug = self.get_state(self.status_entity, attribute="debug") or ""
        now = self.get_now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return parse_plan_windows(debug, midnight.timestamp())

    # ------------------------------------------------------------------ scan

    def scan(self, kwargs) -> None:
        now = time.time()
        writes = self.read_int(self.writes_entity)
        if writes is None:
            self.log(
                f"{self.writes_entity} is unavailable — cannot judge write health "
                "this cycle",
                level="WARNING",
            )
            return

        previous = self.last_writes if self.last_writes is not None else writes
        if writes != previous:
            self.last_write_change = now
        findings = rules.evaluate(
            timeouts=self.timeouts,
            writes_now=writes,
            writes_before=previous,
            windows=self.plan_windows(),
            expected=self.expected_settings,
            actual=self.current_settings(),
            now=now,
            burst_count=self.burst_count,
            burst_window=self.burst_window,
            rate_max=self.rate_max,
            rate_window=self.rate_window,
            last_change=self.last_write_change,
            controlled=self.predbat_controlled,
        )
        self.last_writes = writes

        by_key = {f["rule"]: f for f in findings}
        before = dict(self.sent)
        to_send, held, self.sent = policy.apply(
            by_key.keys(),
            now_epoch=now,
            now_hour=self.get_now().hour,
            state=self.sent,
            quiet_start=self.quiet_start,
            quiet_end=self.quiet_end,
            repeat_after=self.repeat_after,
        )
        for key, reason in to_send:
            if self.alert(by_key[key], reason) == 0:
                # apply() marked the rule before anyone was gated; nobody was
                # reached, so the repeat interval must not start (IQ-13).
                self.sent = policy.uncommit(self.sent, before, key)
        for key, reason in held:
            self.log(f"holding {key}: {reason}", level="INFO")
        self.save_state()

    def alert(self, finding: dict[str, Any], reason: str) -> int:
        """Notify every target, minus non-urgent drops for anyone away. Returns reached."""
        message = finding["detail"]
        rule = finding["rule"]
        self.log(f"ALERT {rule} ({reason}): {message}", level="ERROR")
        domain = self.get_state(irisone_bus.DOMAIN)
        now_epoch = time.time()
        reached = 0
        for target in self.notify_targets:
            in_home, since = irisone_bus.presence_for_target(domain, target, now_epoch)
            if rule in URGENT_RULES:
                send, gate_reason = policy.should_notify(policy.URGENT, in_home)
            else:
                send, gate_reason = policy.should_notify(policy.NON_URGENT, in_home)
            self._report_gate(rule, target, send, gate_reason)
            if not send:
                if policy.drop_log_due(rule, target, since, self._drop_logged):
                    self.log(f"[presence] dropped {self.name}/{rule} to {target}: {gate_reason}")
                continue
            try:
                self.call_service(
                    f"notify/{target}",
                    title="Predbat guard",
                    message=message,
                    data={
                        "channel": self.channel,
                        "priority": self.priority,
                        "ttl": 0,
                    },
                )
                reached += 1
            except Exception as exc:  # a failed notify must not kill the scan
                self.log(f"notify {target} failed: {exc}", level="ERROR")
        return reached

    def _report_gate(self, rule: str, target: str, send: bool, reason: str) -> None:
        """Count the decision in irisone.notify_gate_stats (IQ-28). Never blocks a send."""
        try:
            urgency = policy.URGENT if rule in URGENT_RULES else policy.NON_URGENT
            self.fire_event(irisone_bus.GATE_EVENT, **irisone_bus.gate_event(
                self.name, rule, target, urgency, send, reason))
        except Exception as exc:  # the counter is evidence, not a dependency
            self.log(f"[presence] could not report gate decision: {exc}", level="WARNING")
