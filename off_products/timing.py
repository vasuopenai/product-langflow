"""Per-request step timings.

Code marks its steps with ``with step("search"):``; whoever started a timer (the mobile gateway,
once per request) reads the milliseconds per step afterwards. Without a running timer, step()
does nothing, so the CLI and tests are unaffected. The timer travels in a context variable, which
FastAPI copies into the worker thread that runs a request, so nothing is passed around.
"""

import contextlib
import contextvars
import time

_current = contextvars.ContextVar("request_timings", default=None)


def start():
    """Start timing the current request. Returns (timings, token); pass the token to stop()."""
    timings = {"steps": {}, "meta": {}}
    return timings, _current.set(timings)


def stop(token):
    _current.reset(token)


@contextlib.contextmanager
def step(name):
    """Time a block. Repeated steps add up (e.g. a search that runs again with a filter relaxed)."""
    timings = _current.get()
    if timings is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        steps = timings["steps"]
        steps[name] = round(steps.get(name, 0.0) + (time.perf_counter() - t0) * 1000, 1)


def note(key, value):
    """Something about the request worth knowing next to its timings (e.g. cache_hit)."""
    timings = _current.get()
    if timings is not None:
        timings["meta"][key] = value
