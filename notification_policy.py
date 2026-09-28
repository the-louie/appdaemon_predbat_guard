"""When to send a repeating alert, and when to stay quiet — policy D2.

Deliberately free of AppDaemon imports and of any clock of its own: `now` is
always passed in. That makes every rule directly testable, including the
midnight-wrapping quiet window, which is the part most likely to be wrong.

The rules, in the order they are applied to one condition:

1. **A condition not seen before is always sent, whatever the hour.** A dead app
   at 23:00 still matters. Quiet hours suppress *nagging*, not *news*.
2. Within the repeat window, stay silent.
3. Past the repeat window but inside quiet hours, stay silent and do not
   consume the repeat — so the alert lands promptly once the window opens
   rather than waiting another full interval.
4. Otherwise, send.

A condition that clears is forgotten, so if it returns it is news again.

State is a plain dict of `{key: last_sent_epoch}`, JSON-serialisable so the
caller can persist it across restarts. That matters: without it a reload resets
every cooldown and re-notifies everything, which is the "redundant restart-time
re-alerts" problem D2 names explicitly.
"""

DEFAULT_QUIET_START = 22  # inclusive, local hour
DEFAULT_QUIET_END = 7  # exclusive, local hour
DEFAULT_REPEAT_AFTER = 6 * 3600  # seconds

SEND_FIRST = "first occurrence"
SEND_REPEAT = "repeat interval elapsed"
HOLD_RECENT = "within repeat interval"
HOLD_QUIET = "quiet hours"


def in_quiet_hours(hour, start=DEFAULT_QUIET_START, end=DEFAULT_QUIET_END):
    """True if `hour` falls in the quiet window, which may wrap past midnight.

    A window like 22->07 wraps; one like 01->06 does not. Getting this wrong in
    the wrapping case is the classic bug, so both forms are handled explicitly.
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def decide(
    key,
    now_epoch,
    now_hour,
    state,
    quiet_start=DEFAULT_QUIET_START,
    quiet_end=DEFAULT_QUIET_END,
    repeat_after=DEFAULT_REPEAT_AFTER,
):
    """Return (send: bool, reason: str) for one condition. Does not mutate state."""
    last_sent = state.get(key)

    if last_sent is None:
        return True, SEND_FIRST

    if now_epoch - last_sent < repeat_after:
        return False, HOLD_RECENT

    if in_quiet_hours(now_hour, quiet_start, quiet_end):
        return False, HOLD_QUIET

    return True, SEND_REPEAT


def apply(
    keys,
    now_epoch,
    now_hour,
    state,
    quiet_start=DEFAULT_QUIET_START,
    quiet_end=DEFAULT_QUIET_END,
    repeat_after=DEFAULT_REPEAT_AFTER,
):
    """Decide for a whole set of currently-active conditions.

    Returns (to_send, held, new_state). Keys absent from `keys` are dropped from
    the state, so a condition that clears and returns is treated as news.
    """
    to_send, held, new_state = [], [], {}

    for key in sorted(keys):
        send, reason = decide(
            key, now_epoch, now_hour, state,
            quiet_start=quiet_start, quiet_end=quiet_end, repeat_after=repeat_after,
        )
        if send:
            to_send.append((key, reason))
            new_state[key] = now_epoch
        else:
            held.append((key, reason))
            # Preserve the original timestamp: holding must not extend the window.
            new_state[key] = state[key]

    return to_send, held, new_state
