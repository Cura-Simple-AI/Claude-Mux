"""Tests for _time_ago — håndterer både float-epoch og ISO-streng."""
import time
import claude_mux as hs


class TestTimeAgo:
    def test_float_epoch(self):
        """test_res['ts'] = time.time() (float) — må ikke crashe."""
        ts = time.time() - 65  # 65 sekunder siden
        result = hs._time_ago(ts)
        assert "ago" in result
        assert result != "-"

    def test_int_epoch(self):
        ts = int(time.time()) - 130
        result = hs._time_ago(ts)
        assert "ago" in result

    def test_iso_string(self):
        from datetime import datetime, timezone
        iso = datetime.now(timezone.utc).isoformat()
        result = hs._time_ago(iso)
        assert "ago" in result or result in ("0s", "-")

    def test_iso_z_suffix(self):
        from datetime import datetime, timezone
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = hs._time_ago(iso)
        assert "ago" in result or result in ("0s", "-")

    def test_none_returns_dash(self):
        assert hs._time_ago(None) == "-"

    def test_zero_returns_dash(self):
        """0 / falsy float → '-'."""
        assert hs._time_ago(0) == "-"

    def test_future_returns_zero(self):
        ts = time.time() + 9999
        assert hs._time_ago(ts) == "0s"
