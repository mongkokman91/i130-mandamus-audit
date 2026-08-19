#!/usr/bin/env python3
"""Run pacer_audit.py while respecting CourtListener's API throttle.

A persistent 429 must return control to the audit quickly so its normal
per-query error handling can preserve partial output instead of allowing the
GitHub job to time out.
"""
from __future__ import annotations

import re
import runpy
import threading
import time

import requests

# CourtListener token limit observed for this project is 20 requests/minute.
# Stay below the ceiling and retry a throttled request only once.
_MIN_INTERVAL = 3.5
_MAX_429_RETRIES = 1
_MAX_RETRY_SLEEP = 65.0
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
            return min(max(float(header), 1.0), _MAX_RETRY_SLEEP)
        except ValueError:
            pass
    match = re.search(r"available in\s+(\d+)\s+seconds", response.text or "", re.I)
    if match:
        return min(max(float(match.group(1)) + 1.0, 1.0), _MAX_RETRY_SLEEP)
    return min(_MAX_RETRY_SLEEP, 5.0 * (attempt + 1))


def throttled_request(self, method, url, *args, **kwargs):
    response = None
    for attempt in range(_MAX_429_RETRIES + 1):
        _wait_for_slot()
        response = _original_request(self, method, url, *args, **kwargs)
        if response.status_code != 429:
            return response
        if attempt >= _MAX_429_RETRIES:
            print("CourtListener rate limit persists; recording this request as an error and continuing.", flush=True)
            return response
        delay = _retry_seconds(response, attempt)
        print(f"CourtListener rate limit reached; one retry in {delay:.0f}s...", flush=True)
        time.sleep(delay)
    return response


requests.Session.request = throttled_request
runpy.run_path("pacer_audit.py", run_name="__main__")
