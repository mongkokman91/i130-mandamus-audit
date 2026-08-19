#!/usr/bin/env python3
"""Run pacer_audit.py while respecting CourtListener's API throttle."""
from __future__ import annotations

import re
import runpy
import threading
import time

import requests

# CourtListener currently enforces 20 requests/minute for this API token.
# 3.25s/request leaves a small safety margin below that ceiling.
_MIN_INTERVAL = 3.25
_MAX_429_RETRIES = 5
_lock = threading.Lock()
_last_request_at = 0.0
_original_request = requests.Session.request


def _wait_for_slot() -> None:
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_seconds(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(float(header), 1.0)
        except ValueError:
            pass
    match = re.search(r"available in\s+(\d+)\s+seconds", response.text or "", re.I)
    if match:
        return max(float(match.group(1)) + 1.0, 1.0)
    return min(60.0, 5.0 * (attempt + 1))


def throttled_request(self, method, url, *args, **kwargs):
    for attempt in range(_MAX_429_RETRIES + 1):
        _wait_for_slot()
        response = _original_request(self, method, url, *args, **kwargs)
        if response.status_code != 429 or attempt >= _MAX_429_RETRIES:
            return response
        delay = _retry_seconds(response, attempt)
        print(f"CourtListener rate limit reached; retrying in {delay:.0f}s...", flush=True)
        time.sleep(delay)
    return response


requests.Session.request = throttled_request
runpy.run_path("pacer_audit.py", run_name="__main__")
