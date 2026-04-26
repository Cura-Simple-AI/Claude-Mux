"""Tests for pure utility functions: _format_duration, _status_char, _status_color."""
import claude_mux as hs


class TestFormatDuration:
    def test_seconds_only(self):
        assert hs._format_duration(45) == "45s"

    def test_zero(self):
        assert hs._format_duration(0) == "0s"

    def test_negative(self):
        assert hs._format_duration(-5) == "0s"

    def test_one_minute(self):
        assert hs._format_duration(60) == "1m"

    def test_minutes_and_seconds(self):
        assert hs._format_duration(90) == "1m 30s"

    def test_one_hour(self):
        assert hs._format_duration(3600) == "1h"

    def test_hours_and_minutes(self):
        assert hs._format_duration(3660) == "1h 1m"

    def test_one_day(self):
        assert hs._format_duration(86400) == "1d"

    def test_days_and_hours(self):
        assert hs._format_duration(90000) == "1d 1h"

    def test_max_two_parts(self):
        """Aldrig mere end 2 dele — sekunder droppes når dage/timer vises."""
        result = hs._format_duration(86461)  # 1d 1m 1s
        parts = result.split()
        assert len(parts) <= 2


class TestStatusIcon:
    def test_online(self):
        assert hs._status_char("online") == "*"

    def test_stopped(self):
        assert hs._status_char("stopped") == "o"

    def test_error(self):
        assert hs._status_char("error") == "x"

    def test_unknown(self):
        assert hs._status_char("unknown") == "?"

    def test_unrecognized(self):
        assert hs._status_char("launching") == "?"


class TestStatusColor:
    def test_online_green(self):
        assert hs._status_color("online") == "green"

    def test_stopped_gray(self):
        assert hs._status_color("stopped") == "gray"

    def test_error_red(self):
        assert hs._status_color("error") == "red"

    def test_unknown_yellow(self):
        assert hs._status_color("unknown") == "yellow"

    def test_unrecognized_gray(self):
        assert hs._status_color("starting") == "gray"
