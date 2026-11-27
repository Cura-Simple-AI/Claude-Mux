"""A private tmux server for the session tests (socket under TMUX_TMPDIR, see conftest)."""
from __future__ import annotations

import re
import subprocess
import time


class TmuxServer:
    SESSION = "main"

    def __call__(self, *args: str) -> str:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def _has_session(self) -> bool:
        return subprocess.run(["tmux", "has-session", "-t", self.SESSION],
                              capture_output=True).returncode == 0

    def _new(self, command: str, cwd: str | None) -> str:
        where = ["-c", cwd] if cwd else []
        if not self._has_session():
            out = self("new-session", "-d", "-s", self.SESSION, "-x", "160", "-y", "45", *where,
                       "-P", "-F", "#{pane_id}", command)
        else:
            out = self("new-window", "-d", "-t", self.SESSION, *where, "-P", "-F", "#{pane_id}", command)
        return out.strip()

    def shell_pane(self, cwd: str | None = None) -> str:
        """A pane running an interactive bash, like a user's terminal."""
        pane = self._new("exec /bin/bash --noprofile --norc", cwd)
        self.wait(pane, lambda s: s.rstrip().endswith("$"), what="shell prompt")
        return pane

    def direct_pane(self, command: str, cwd: str | None = None) -> str:
        """A pane whose process IS the command (e.g. tmux new-window 'claude ...')."""
        return self._new(f"exec {command}", cwd)

    def target(self, pane: str) -> str:
        return self("display-message", "-p", "-t", pane,
                    "#{session_name}:#{window_index}.#{pane_index}").strip()

    def type(self, pane: str, text: str, enter: bool = True) -> None:
        self("send-keys", "-t", pane, "-l", text)
        if enter:
            time.sleep(0.15)  # let the program render the text, like a person typing
            self("send-keys", "-t", pane, "Enter")

    def keys(self, pane: str, *keys: str) -> None:
        self("send-keys", "-t", pane, *keys)

    def capture(self, pane: str) -> str:
        return self("capture-pane", "-p", "-t", pane)

    def option(self, pane: str, name: str) -> str:
        return self("show-options", "-p", "-v", "-t", pane, name).strip()

    def pane_info(self, pane: str) -> dict:
        out = self("display-message", "-p", "-t", pane, "#{pane_dead}|#{pane_in_mode}|#{pane_pid}")
        dead, in_mode, pid = out.strip().split("|")
        return {"dead": dead == "1", "in_mode": in_mode == "1", "pid": int(pid)}

    def wait(self, pane: str, check, timeout: float = 20, what: str = "screen") -> str:
        """Wait until check(screen) is true; check may also be a substring or regex."""
        if isinstance(check, str):
            needle = check
            check = lambda s: needle in s  # noqa: E731
        elif isinstance(check, re.Pattern):
            pattern = check
            check = lambda s: pattern.search(s)  # noqa: E731
        deadline = time.time() + timeout
        screen = ""
        while time.time() < deadline:
            screen = self.capture(pane)
            if check(screen):
                return screen
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for {what} in {pane}; screen:\n{screen}")

    def kill(self) -> None:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
