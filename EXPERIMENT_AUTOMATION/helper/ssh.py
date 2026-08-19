# helper/ssh.py
"""
SSH/SFTP plumbing shared by the SUT and JMeter hosts.

Three machines are involved: this controller runs the automation, the load
driver runs JMeter, and the SUT runs the application together with whichever
instrumentation is under test. Neither of the remote hosts runs any agent of
ours, so everything is driven over plain SSH with password auth, matching the
existing automation in the ASE project.

Long-running remote commands are started through :class:`RemoteProcess`, which
detaches them from the SSH channel so they survive it, and records a PID the
controller can poll and signal later.
"""

from __future__ import annotations

import os
import posixpath
import shlex
import socket
import stat as pystat
import time
from pathlib import Path

import paramiko


def connect(host: str, user: str, password: str, port: int = 22) -> paramiko.SSHClient:
    """Open an SSH connection with password auth."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=30)
    return client


def credentials(role: str) -> tuple[str, str]:
    """Read ``{ROLE}_SSH_USER`` / ``{ROLE}_SSH_PASSWORD`` from the environment."""
    user = os.environ.get(f"{role}_SSH_USER")
    password = os.environ.get(f"{role}_SSH_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            f"Missing SSH credentials for the {role} host. "
            f"Set {role}_SSH_USER and {role}_SSH_PASSWORD (see .env.template)."
        )
    return user, password


def run(
    client: paramiko.SSHClient,
    command: str,
    check: bool = True,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a command to completion and return ``(exit_code, stdout, stderr)``."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    try:
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
    except socket.timeout as exc:
        # paramiko raises a bare socket.timeout here, which *is* TimeoutError on
        # Python 3.10+ and carries no message -- unhelpful when it surfaces
        # several layers up. Name the command and the limit instead.
        raise TimeoutError(
            f"Remote command produced no output for {timeout}s and was abandoned: "
            f"{command}\n"
            "If the command starts a background process, it must be launched with "
            "run_detached() so the channel is not held open by the child."
        ) from exc
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        raise RuntimeError(
            f"Remote command failed ({code}): {command}\n"
            f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
        )
    return code, out, err


def run_detached(client: paramiko.SSHClient, command: str) -> None:
    """Fire a command without waiting for its channel to reach EOF.

    ``run()`` blocks on ``stdout.read()`` until the remote side closes the
    channel. A command that leaves a long-running background process behind may
    never close it -- the child keeps the session's descriptors alive even when
    its own streams are redirected -- so the read blocks until the channel
    timeout and the caller sees a bare ``TimeoutError`` despite the process
    having started perfectly well.

    Nothing is read here and the channel is closed immediately. The caller
    confirms the launch out of band, by polling for the PID file.
    """
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not connected")
    channel = transport.open_session()
    try:
        channel.exec_command(command)
    finally:
        channel.close()


def is_root(client: paramiko.SSHClient) -> bool:
    """True when the SSH user is already root."""
    code, out, _ = run(client, "id -u", check=False)
    return code == 0 and out.strip() == "0"


def has_passwordless_sudo(client: paramiko.SSHClient) -> bool:
    """True when ``sudo`` runs without prompting for a password."""
    code, _, _ = run(client, "sudo -n true", check=False)
    return code == 0


def privilege_prefix(client: paramiko.SSHClient) -> str:
    """Return the prefix needed to run a command as root.

    Empty when the session is already root -- which is the common case on a
    minimal Debian or Proxmox install, where ``sudo`` is often not installed at
    all, so prefixing it unconditionally would break every privileged command.
    Otherwise ``sudo -n ``, and a hard failure if neither route works.

    Cached on the client: this runs on every privileged command and the answer
    cannot change within a session.
    """
    cached = getattr(client, "_privilege_prefix", None)
    if cached is not None:
        return cached

    if is_root(client):
        prefix = ""
        print("[ssh] session is root; running privileged commands directly")
    elif has_passwordless_sudo(client):
        prefix = "sudo -n "
    else:
        raise RuntimeError(
            "Root privileges are required to load the eBPF programs, but the SSH "
            "user is not root and passwordless sudo is unavailable.\n"
            "Either connect as root, or grant sudo:\n"
            "  echo \"$USER ALL=(ALL) NOPASSWD: ALL\" | sudo tee /etc/sudoers.d/ebpf-experiments"
        )

    client._privilege_prefix = prefix  # type: ignore[attr-defined]
    return prefix


def ensure_dir(client: paramiko.SSHClient, path: str) -> None:
    """Create a remote directory, including parents."""
    run(client, f"mkdir -p {shlex.quote(path)}")


def exists(client: paramiko.SSHClient, path: str) -> bool:
    """True when a remote path exists."""
    code, _, _ = run(client, f"test -e {shlex.quote(path)}", check=False)
    return code == 0


def upload(client: paramiko.SSHClient, local: Path, remote: str) -> None:
    """Copy one local file to the remote host."""
    sftp = client.open_sftp()
    try:
        ensure_dir(client, posixpath.dirname(remote))
        print(f"[ssh] upload {local.name} -> {remote}")
        sftp.put(str(local), remote)
    finally:
        sftp.close()


def download(client: paramiko.SSHClient, remote: str, local: Path) -> bool:
    """Copy one remote file to the controller; False when it is absent."""
    sftp = client.open_sftp()
    try:
        try:
            remote_size = sftp.stat(remote).st_size
        except FileNotFoundError:
            print(f"[ssh] missing on remote, skipped: {remote}")
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(remote, str(local))
        if local.stat().st_size != remote_size:
            print(f"[ssh] WARNING size mismatch for {remote}")
            return False
        print(f"[ssh] fetched {posixpath.basename(remote)} -> {local}")
        return True
    finally:
        sftp.close()


def download_dir(client: paramiko.SSHClient, remote_dir: str, local_dir: Path) -> int:
    """Copy every regular file of a remote directory (non-recursive)."""
    sftp = client.open_sftp()
    count = 0
    try:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except FileNotFoundError:
            print(f"[ssh] remote directory absent: {remote_dir}")
            return 0
        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            if pystat.S_ISDIR(entry.st_mode):
                continue
            sftp.get(posixpath.join(remote_dir, entry.filename), str(local_dir / entry.filename))
            count += 1
        print(f"[ssh] fetched {count} file(s) from {remote_dir}")
        return count
    finally:
        sftp.close()


class RemoteProcess:
    """A detached remote process the controller can poll, signal and inspect.

    The command is wrapped as::

        setsid bash -c 'echo $$ > pidfile; exec <command>' > log 2>&1 &

    ``bash`` writes its own PID and then *execs* the target, so the pidfile holds
    the real process. ``setsid`` makes that PID a process-group leader, which
    lets us signal the whole group later -- necessary because ``sudo`` forks
    before running the eBPF agent, so signalling the recorded PID alone would
    leave the agent behind.
    """

    def __init__(
        self,
        client: paramiko.SSHClient,
        name: str,
        command: str,
        remote_dir: str,
        log_path: str,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> None:
        self.client = client
        self.name = name
        self.command = command
        self.remote_dir = remote_dir
        self.log_path = log_path
        self.env = env or {}
        self.sudo = sudo
        self.pid: int | None = None
        self.pid_path = posixpath.join(remote_dir, f".{name.lower()}.pid")

    def start(self) -> int:
        """Launch the process and return its PID."""
        ensure_dir(self.client, self.remote_dir)
        ensure_dir(self.client, posixpath.dirname(self.log_path))

        env_prefix = "".join(f"{k}={shlex.quote(str(v))} " for k, v in self.env.items())
        payload = f"echo $$ > {shlex.quote(self.pid_path)}; exec {env_prefix}{self.command}"
        launcher = (
            f"cd {shlex.quote(self.remote_dir)} && "
            f"setsid bash -c {shlex.quote(payload)} "
            f"> {shlex.quote(self.log_path)} 2>&1 < /dev/null & "
            f"disown; sleep 0.5"
        )

        print(f"[{self.name}] $ {self.command}")
        print(f"[{self.name}] log -> {self.log_path}")

        # Fire and forget: the started process keeps the channel open, so
        # waiting for EOF here would block until the channel timeout.
        run(self.client, f"rm -f {shlex.quote(self.pid_path)}", check=False)
        run_detached(self.client, launcher)

        # The pidfile is written by the wrapper, so it may lag the launcher.
        for _ in range(20):
            code, out, _ = run(self.client, f"cat {shlex.quote(self.pid_path)}", check=False)
            if code == 0 and out.strip().isdigit():
                self.pid = int(out.strip())
                print(f"[{self.name}] pid={self.pid}")
                return self.pid
            time.sleep(0.25)

        raise RuntimeError(f"[{self.name}] did not report a PID; check {self.log_path}")

    def is_running(self) -> bool:
        """True while the process exists.

        Uses ``/proc`` rather than ``kill -0``: the eBPF agent runs as root, and
        ``kill -0`` against another user's process fails with EPERM, which would
        be indistinguishable from "gone".
        """
        if self.pid is None:
            return False
        code, _, _ = run(self.client, f"test -d /proc/{self.pid}", check=False)
        return code == 0

    def signal(self, signal_name: str) -> None:
        """Send a signal to the whole process group."""
        if self.pid is None:
            return
        prefix = privilege_prefix(self.client) if self.sudo else ""
        run(self.client, f"{prefix}kill -{signal_name} -- -{self.pid} 2>/dev/null || true", check=False)

    def stop(self, timeout: int = 30, first_signal: str = "TERM") -> None:
        """Stop the process group, escalating until it is gone.

        ``first_signal`` is INT for tools that flush on interrupt -- the eBPF
        jAgent installs a SIGINT handler for exactly that purpose.
        """
        if self.pid is None or not self.is_running():
            return

        print(f"[{self.name}] stopping (SIG{first_signal}) ...")
        self.signal(first_signal)
        deadline = time.time() + timeout
        while time.time() < deadline and self.is_running():
            time.sleep(0.5)

        for escalation in ("TERM", "KILL"):
            if not self.is_running():
                break
            print(f"[{self.name}] still alive; sending SIG{escalation} ...")
            self.signal(escalation)
            deadline = time.time() + 10
            while time.time() < deadline and self.is_running():
                time.sleep(0.5)

        if self.is_running():
            print(f"[{self.name}] WARNING: still running after SIGKILL")
        else:
            print(f"[{self.name}] stopped")

    def tail(self, lines: int = 20) -> str:
        """Return the last lines of the remote log, for error reporting."""
        code, out, _ = run(
            self.client, f"tail -n {lines} {shlex.quote(self.log_path)}", check=False
        )
        return out if code == 0 and out.strip() else "(log unavailable)"
