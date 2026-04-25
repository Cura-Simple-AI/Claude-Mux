"""claude-mux — use any LLM inside Claude Code. Automatically."""

from claude_mux.tui import (
    __version__,
    ConfigManager,
    InstanceManager,
    SyncManager,
    FailoverManager,
    HeimsenseApp,
    _format_duration,
    _time_ago,
    _status_char,
    _status_color,
    CLAUDE_MUX_DIR,
    SUBSCRIPTIONS_FILE,
)

__all__ = [
    "__version__",
    "ConfigManager",
    "InstanceManager",
    "SyncManager",
    "FailoverManager",
    "HeimsenseApp",
    "_format_duration",
    "_time_ago",
    "_status_char",
    "_status_color",
    "CLAUDE_MUX_DIR",
    "SUBSCRIPTIONS_FILE",
]
