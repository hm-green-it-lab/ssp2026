# helper/local_proc.py
"""
Local process management on the controller.

Used for JMeter when ``jmeter.location: controller``. The interface mirrors
:class:`helper.ssh.RemoteProcess` so the runner can treat both the same way.

Killing a process *tree* is the awkward part and differs per platform: JMeter's
launcher spawns a separate ``java`` child, so signalling only the launcher would
leave the load generator running and hold the next iteration's results hostage.
On POSIX we start a new session and signal the whole process group; on Windows
we hand the tree to ``taskkill /T``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

IS_WINDOWS = sys.platform == "win32"


class LocalProcess:
    """A locally started process whose output is captured to a log file."""

    def __init__(
        self,
        name: str,
        command: list[str],
        log_path: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.command = [str(c) for c in command]
        self.log_path = Path(log_path)
        self.cwd = Path(cwd) if cwd else None
        self.env = env or {}
        self.process: subprocess.Popen | None = None
        self._log: IO | None = None
        self._finished = False

    def start(self) -> int:
        """Launch the process and return its PID."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("w", encoding="utf-8", errors="replace")

        merged = dict(os.environ)
        merged.update({k: str(v) for k, v in self.env.items()})

        kwargs: dict = {}
        if IS_WINDOWS:
            # Needed for the CTRL_BREAK path and to keep Ctrl-C in this console
            # from reaching the child.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        print(f"[{self.name}] $ {' '.join(self.command)}")
        print(f"[{self.name}] log -> {self.log_path}")
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            env=merged,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        print(f"[{self.name}] pid={self.process.pid}")
        return self.process.pid

    @property
    def pid(self) -> int:
        """PID of the running process."""
        if not self.process:
            raise RuntimeError(f"[{self.name}] not started")
        return self.process.pid

    def is_running(self) -> bool:
        """True while the process has not exited."""
        return self.process is not None and self.process.poll() is None

    def _kill_tree(self, force: bool) -> None:
        """Terminate the process and its children, platform-appropriately."""
        if not self.process:
            return
        if IS_WINDOWS:
            cmd = ["taskkill", "/PID", str(self.process.pid), "/T"]
            if force:
                cmd.append("/F")
            # Captured as bytes on purpose: taskkill writes localised text in
            # the OEM codepage, which the default cp1252 decoder chokes on
            # ("charmap codec can't decode byte 0x81"). The output is unused,
            # so decoding it would only ever be a source of crashes.
            subprocess.run(cmd, capture_output=True)
            return
        try:
            os.killpg(
                os.getpgid(self.process.pid),
                signal.SIGKILL if force else signal.SIGTERM,
            )
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def stop(self, timeout: int = 30) -> int | None:
        """Stop the process tree, escalating to a forced kill if needed.

        Idempotent: the runner may call this both on the timeout path and again
        during teardown, and reporting the exit twice is just noise.
        """
        if not self.process:
            return None
        if self._finished:
            return self.process.poll()
        if self.process.poll() is not None:
            return self._finish()

        print(f"[{self.name}] stopping ...")
        self._kill_tree(force=False)
        deadline = time.time() + timeout
        while time.time() < deadline and self.process.poll() is None:
            time.sleep(0.2)

        if self.process.poll() is None:
            print(f"[{self.name}] still alive; forcing ...")
            self._kill_tree(force=True)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"[{self.name}] WARNING: process did not exit")

        return self._finish()

    def wait(self, timeout: int) -> int | None:
        """Wait up to *timeout* seconds; return the exit code or None."""
        if not self.process:
            return None
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def _finish(self) -> int | None:
        """Close the log handle and report the exit code."""
        code = self.process.poll() if self.process else None
        self._finished = True
        if self._log:
            try:
                self._log.flush()
                self._log.close()
            except OSError:
                pass
            self._log = None
        print(f"[{self.name}] exited with code {code}")
        return code

    def tail(self, lines: int = 20) -> str:
        """Return the last lines of the log, for error reporting."""
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(log unavailable)"
        return "\n".join(content.splitlines()[-lines:]) or "(log empty)"
