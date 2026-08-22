#!/usr/bin/env python3
"""Run the audit with conservative, bounded CourtListener retry behavior."""
from __future__ import annotations

import random
import re
import runpy
import threading
import time

import requests

_MIN_INTERVAL = 5.0
_MAX_429_RETRIES = 8
_MAX_RETRY_SLEEP = 180.0
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
            base = float(header)
        except ValueError:
            base = 0.0
    else:
        match = re.search(r"available in\s+(\d+)\s+seconds", response.text or "", re.I)
        base = float(match.group(1)) + 1.0 if match else 5.0 * (2 ** attempt)
    return min(max(base + random.uniform(0.5, 2.5), 1.0), _MAX_RETRY_SLEEP)


def throttled_request(self, method, url, *args, **kwargs):
    response = None
    for attempt in range(_MAX_429_RETRIES + 1):
        _wait_for_slot()
        response = _original_request(self, method, url, *args, **kwargs)
        if response.status_code != 429:
            return response
        if attempt >= _MAX_429_RETRIES:
            print("CourtListener rate limit persisted after all retries.", flush=True)
            return response
        delay = _retry_seconds(response, attempt)
        print(f"CourtListener 429; retry {attempt + 1}/{_MAX_429_RETRIES} in {delay:.0f}s", flush=True)
        time.sleep(delay)
    return response


requests.Session.request = throttled_request
runpy.run_path("pacer_audit.py", run_name="__main__")
