"""OAuth quota measurement for `claude-mux probe --quota`.

Makes one 1-token haiku request per OAuth subscription directly against
api.anthropic.com and reads the ``anthropic-ratelimit-unified-*`` response
headers. A 429 response still carries the headers, so it is read the same way.

Security: the subscription token is only ever placed in the Authorization
header of the request. It is never returned, printed, logged or put in an
exception message by this module. Errors are reported by exception type only.
"""
import datetime
import json
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
QUOTA_MODEL = "claude-haiku-4-5-20251001"
_PREFIX = "anthropic-ratelimit-unified-"
NOT_OAUTH = "not an OAuth profile"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_quota_headers(headers) -> dict:
    """Extract the unified rate-limit fields from a response header mapping."""
    lowered = {str(k).lower(): v for k, v in headers.items()}

    def get(key):
        return lowered.get(_PREFIX + key)

    week = _num(get("7d-utilization"))
    five = _num(get("5h-utilization"))
    return {
        "week_pct": None if week is None else round(week * 100, 1),
        "week_reset_epoch": _epoch(get("7d-reset")),
        "five_hour_pct": None if five is None else round(five * 100, 1),
        "five_hour_reset_epoch": _epoch(get("5h-reset")),
        "status": get("status") or None,
    }


def _has_quota(q: dict) -> bool:
    return any(v is not None for v in q.values())


def measure_quota(sub: dict, timeout: int = 30) -> dict:
    """Measure one subscription. The returned dict never contains the token.

    Result keys: name, measured (bool), http_code, week_pct, week_reset_epoch,
    five_hour_pct, five_hour_reset_epoch, status, reason (when not measured).
    """
    name = sub.get("name", "")
    if sub.get("auth_type") != "oauth":
        return {"name": name, "measured": False, "reason": NOT_OAUTH}
    token = sub.get("api_key")
    if not token:
        return {"name": name, "measured": False, "reason": "no token stored"}

    body = json.dumps({
        "model": QUOTA_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "x"}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    })
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code, headers = resp.status, resp.headers
        except urllib.error.HTTPError as e:
            code, headers = e.code, (e.headers or {})
    except Exception as e:  # report the type only: a message could echo the request
        return {"name": name, "measured": False, "reason": type(e).__name__,
                "network_error": True}

    q = parse_quota_headers(headers)
    result = {"name": name, "measured": _has_quota(q), "http_code": code, **q}
    if not result["measured"]:
        reason = f"HTTP {code} without quota headers"
        if code == 401:
            reason = "HTTP 401 (token rejected or expired)"
        result["reason"] = reason
    return result


def _pct(v):
    return "-" if v is None else f"{v:.0f} %"


def _local(epoch):
    """Render an epoch in the host's local zone (honours TZ), naming the zone."""
    if epoch is None:
        return "-"
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .astimezone().strftime("%a %d/%m %H:%M %Z"))


def format_row(r: dict) -> str:
    """One human-readable line per result."""
    if r.get("http_code") is None:
        return f"{r['name']} | cannot be measured: {r['reason']}"
    if not r["measured"]:
        return f"{r['name']} | HTTP {r['http_code']} | cannot be measured: {r['reason']}"
    return (f"{r['name']} | HTTP {r['http_code']} | week {_pct(r['week_pct'])}"
            f" | week resets {_local(r['week_reset_epoch'])}"
            f" | 5h {_pct(r['five_hour_pct'])}"
            f" | 5h resets {_local(r['five_hour_reset_epoch'])}"
            f" | status {r['status'] or '-'}")
