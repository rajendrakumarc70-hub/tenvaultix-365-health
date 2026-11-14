"""Emergency stop: one switch that stops the app using anyone's Microsoft access while you deal with an incident.

While it's on, no scans run (scheduled or manual), no company can connect, no one-click fix or undo can run and
Azure costs aren't read. People can still sign in and read the reports already stored. It can be turned on from
the console (Security page) or, if the console itself is in doubt, on the server with TVX_EMERGENCY_STOP=on.
"""
from __future__ import annotations

from .db import Store, utcnow

STOP_KEY = "emergency_stop"


def stop_state(settings, store: Store) -> dict | None:
    """Who turned the emergency stop on and when, or None when everything is running normally."""
    if getattr(settings, "emergency_stop", False):
        return {"by": "the server setting TVX_EMERGENCY_STOP", "at": None, "source": "server"}
    value = store.get_option(STOP_KEY)
    if not value:
        return None
    by, _, at = value.partition("|")
    return {"by": by, "at": at or None, "source": "console"}


def turn_on(store: Store, by: str) -> None:
    store.set_option(STOP_KEY, f"{by}|{utcnow().isoformat(timespec='seconds')}")


def turn_off(store: Store) -> None:
    store.delete_option(STOP_KEY)
