# helper/jmeter.py
"""
JMeter driver.

``jmeter.location`` decides where the load generator runs:

``controller`` (default)
    JMeter runs on this machine. Results are written straight into the run's
    output directory, so nothing has to be staged or fetched. The controller is
    still a different machine from the SUT, so load generation does not compete
    with the traced JVM for CPU.

``remote``
    JMeter runs on a dedicated load driver over SSH, and its artifacts are
    downloaded afterwards.

The RETIT test plan uses JMeter's built-in ``OpenModelThreadGroup`` with the
schedule::

    rate(0/sec) random_arrivals(RAMP sec)
    rate(TOTAL_RATE/sec) random_arrivals(STEADY sec)
    rate(TOTAL_RATE/sec) random_arrivals(RAMP sec) rate(0/sec)

so one run lasts ``2 * ramp_up_and_down + steady_state`` seconds and needs no
external plugin (JMeter 5.5+).
"""

from __future__ import annotations

import shlex
from pathlib import Path

import paramiko

from helper.local_proc import LocalProcess
from helper.ssh import RemoteProcess, exists, run


def location(jmeter_config: dict) -> str:
    """Return the configured location, defaulting to the controller."""
    value = str(jmeter_config.get("location", "controller")).lower()
    if value not in {"controller", "remote"}:
        raise ValueError(
            f"Unknown jmeter.location: {value!r} (expected controller or remote)"
        )
    return value


def run_duration_secs(jmeter_config: dict) -> int:
    """Total wall-clock seconds of one test-plan execution."""
    props = jmeter_config.get("props", {})
    ramp = int(props.get("ramp_up_and_down", 60))
    steady = int(props.get("steady_state", 120))
    return 2 * ramp + steady


def build_jmeter_command(
    jmeter_config: dict,
    test_plan: str,
    results_path: str,
    jmeter_log_path: str,
) -> list[str]:
    """Build the non-GUI JMeter command line, including all ``-J`` properties."""
    bin_path = jmeter_config.get("bin_path")
    if not bin_path:
        raise RuntimeError("[JMeter] jmeter.bin_path is not configured")

    command = [
        str(bin_path),
        "-n",
        "-t", str(test_plan),
        "-l", str(results_path),
        "-j", str(jmeter_log_path),
    ]
    for key, value in (jmeter_config.get("props") or {}).items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        command.append(f"-J{key}={value}")
    return command


def jmeter_env(jmeter_config: dict) -> dict[str, str]:
    """Heap settings for the JMeter JVM, the way its launcher expects them."""
    parts: list[str] = []
    if jmeter_config.get("heap_xms"):
        parts.append(f"-Xms{jmeter_config['heap_xms']}")
    if jmeter_config.get("heap_xmx"):
        parts.append(f"-Xmx{jmeter_config['heap_xmx']}")
    extra = (jmeter_config.get("extra_jvm_args") or "").strip()
    if extra:
        parts.append(extra)

    env: dict[str, str] = {}
    if parts:
        joined = " ".join(parts)
        env["JVM_ARGS"] = joined
        env["HEAP"] = joined
    if jmeter_config.get("java_home"):
        env["JAVA_HOME"] = str(jmeter_config["java_home"])
    return env


def _describe(jmeter_config: dict) -> None:
    """Print the schedule so the log records what was actually driven."""
    props = jmeter_config.get("props", {})
    print(
        f"[JMeter] running on the {location(jmeter_config)}\n"
        f"[JMeter] target: {props.get('hostname')}:{props.get('port')}\n"
        f"[JMeter] schedule: {props.get('ramp_up_and_down', 60)}s ramp-up + "
        f"{props.get('steady_state', 120)}s steady + "
        f"{props.get('ramp_up_and_down', 60)}s ramp-down = "
        f"{run_duration_secs(jmeter_config)}s "
        f"at {props.get('total_rate', '?')} req/s"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Controller-local execution
# ──────────────────────────────────────────────────────────────────────────────

def preflight_local(jmeter_config: dict) -> None:
    """Fail early if JMeter or the test plan is missing on the controller."""
    bin_path = Path(str(jmeter_config.get("bin_path", ""))).expanduser()
    if not bin_path.exists():
        raise FileNotFoundError(
            f"[JMeter] binary not found on the controller: {bin_path}\n"
            "On Windows this is usually .../bin/jmeter.bat"
        )

    plan = Path(str(jmeter_config.get("resolved_test_plan", ""))).expanduser()
    if not plan.exists():
        raise FileNotFoundError(f"[JMeter] test plan not found: {plan}")


def start_jmeter_local(
    jmeter_config: dict,
    results_path: Path,
    jmeter_log_path: Path,
    stdout_log_path: Path,
) -> LocalProcess:
    """Start JMeter on the controller, writing results straight to *results_path*."""
    command = build_jmeter_command(
        jmeter_config,
        str(jmeter_config["resolved_test_plan"]),
        str(results_path),
        str(jmeter_log_path),
    )
    _describe(jmeter_config)

    process = LocalProcess(
        name="JMeter",
        command=command,
        log_path=stdout_log_path,
        env=jmeter_env(jmeter_config),
    )
    process.start()
    return process


# ──────────────────────────────────────────────────────────────────────────────
# Remote execution on a dedicated load driver
# ──────────────────────────────────────────────────────────────────────────────

def preflight_remote(client: paramiko.SSHClient, jmeter_config: dict) -> None:
    """Fail early if the load driver is missing JMeter or the test plan."""
    bin_path = str(jmeter_config.get("bin_path", ""))
    if not exists(client, bin_path):
        raise FileNotFoundError(f"[JMeter] binary not found on the load driver: {bin_path}")
    run(client, f"chmod +x {shlex.quote(bin_path)} 2>/dev/null || true", check=False)

    plan = str(jmeter_config.get("resolved_test_plan", ""))
    if not exists(client, plan):
        raise FileNotFoundError(f"[JMeter] test plan not found on the load driver: {plan}")


def start_jmeter_remote(
    client: paramiko.SSHClient,
    jmeter_config: dict,
    remote_dir: str,
    results_path: str,
    jmeter_log_path: str,
    stdout_log_path: str,
) -> RemoteProcess:
    """Start JMeter on the load driver and return the process handle."""
    command = build_jmeter_command(
        jmeter_config,
        str(jmeter_config["resolved_test_plan"]),
        results_path,
        jmeter_log_path,
    )
    _describe(jmeter_config)

    process = RemoteProcess(
        client=client,
        name="JMeter",
        command=" ".join(shlex.quote(part) for part in command),
        remote_dir=remote_dir,
        log_path=stdout_log_path,
        env=jmeter_env(jmeter_config),
    )
    process.start()
    return process


def describe_command(jmeter_config: dict) -> str:
    """Human-readable command line, used by ``--dry-run``."""
    command = build_jmeter_command(
        jmeter_config,
        str(jmeter_config.get("resolved_test_plan") or "<test plan>"),
        "<results>.jtl",
        "<jmeter>.log",
    )
    return " ".join(shlex.quote(part) for part in command)
