"""Shared helpers for E2E and full-stack tests."""

import requests

from compute_space.tests.utils import poll


def poll_endpoint(session, url, timeout=30, interval=1, fail_msg="Endpoint not responding"):
    """Poll *url* until it returns HTTP 200, then return the response."""

    def _check():
        try:
            r = session.get(url, timeout=5)
            return r if r.status_code == 200 else None
        except (requests.ConnectionError, requests.exceptions.SSLError):
            return None

    return poll(_check, timeout=timeout, interval=interval, fail_msg=fail_msg)
