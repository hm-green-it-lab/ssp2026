# helper/sut.py
"""
The system under test: the RETIT ``spring-rest-service``, started as a plain
``java -jar`` process on the SUT host over SSH.

Four JVM variants matter for the experiments:

``none``
    Uninstrumented baseline. With ``usdt_probes: true`` this becomes the
    "USDT only" configuration -- the JVM emits its DTrace method probes but no
    eBPF program is attached, which is the cheap nop-patched state.

``otjae``
    The OpenTelemetry Java agent plus the RETIT extension, configured to write
    telemetry through the ``logging`` exporters so the resource demands land in
    the service log and no collector is required.

``jagent``
    The plain JVM with the DTrace probes enabled; the eBPF jAgent attaches
    afterwards (see helper/jagent.py), because it needs the JVM's PID.

``both``
    Both agents at once, on one JVM, measuring the same transactions.

    This is a control, not a measurement point -- the two agents perturb each
    other, so neither its response times nor its demands belong in the overhead
    ladder. It exists to test one specific claim.

    Both tools ultimately read the same kernel field for CPU: the jAgent
    computes ``se.sum_exec_runtime`` plus the current slice in BPF, and OTJAE's
    ``io.retit.*cputime`` comes from ``ThreadMXBean.getCurrentThreadCpuTime()``,
    which on Linux is ``CLOCK_THREAD_CPUTIME_ID`` -> ``task_sched_runtime()`` ->
    the same field. Probe overhead lands on the measured thread and is therefore
    inside *both* readings, so a comparison taken within one run cannot see it
    and the two agents agree however large it is.

    Separate runs are what make it visible: measuring OTJAE on an unperturbed
    system and the jAgent on a perturbed one is the only way the difference
    shows up. This variant reproduces the collapsed comparison deliberately, so
    the artefact can be demonstrated rather than assumed.
"""

from __future__ import annotations

import posixpath
import shlex
import time

import paramiko

from helper.ssh import RemoteProcess, exists, privilege_prefix, run

# Enabling the HotSpot method probes is what makes transaction detection
# possible at all; the allocation probes feed the memory dimension.
USDT_FLAGS = ["-XX:+DTraceAllocProbes", "-XX:+DTraceMethodProbes"]

AGENT_VARIANTS = frozenset({"none", "otjae", "jagent", "both"})


def uses_jagent(agent: str) -> bool:
    """True when the eBPF jAgent has to be attached for this variant."""
    return str(agent).lower() in {"jagent", "both"}


def uses_otjae(agent: str) -> bool:
    """True when the OpenTelemetry Java agent has to be on the JVM command line."""
    return str(agent).lower() in {"otjae", "both"}


def _split_args(value) -> list[str]:
    """Accept JVM args as either a list or a whitespace-separated string."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value).split()


def build_sut_command(config: dict) -> str:
    """Assemble the ``java`` command line for the configured agent variant.

    All paths are interpreted on the SUT host.
    """
    sut = config.get("sut", {})
    agent = str(sut.get("agent", "none")).lower()
    if agent not in AGENT_VARIANTS:
        raise ValueError(
            f"Unknown sut.agent: {agent!r} (expected one of {', '.join(sorted(AGENT_VARIANTS))})"
        )

    parts: list[str] = [str(sut.get("java_bin") or "java")]
    parts += _split_args(sut.get("jvm_args"))

    # Any variant that attaches the jAgent needs the probes; the baseline only
    # when the config asks for the "USDT only" measurement point.
    if uses_jagent(agent) or sut.get("usdt_probes"):
        parts += USDT_FLAGS

    if uses_otjae(agent):
        otjae = config.get("otjae", {})
        parts.append(f"-javaagent:{otjae['remote_otel_agent_jar']}")
        parts.append(f"-Dotel.javaagent.extensions={otjae['remote_extension_jar']}")
        parts.append(f"-Dotel.service.name={otjae.get('service_name', 'spring-app')}")
        for key, value in (otjae.get("properties") or {}).items():
            parts.append(f"-D{key}={value}")

    parts += ["-jar", str(sut["jar_path"])]
    parts += _split_args(sut.get("app_args"))
    return " ".join(shlex.quote(p) if " " in p else p for p in parts)


def preflight(client: paramiko.SSHClient, config: dict) -> None:
    """Fail early if the SUT is missing something the run needs."""
    sut = config.get("sut", {})
    agent = str(sut.get("agent", "none")).lower()

    jar = str(sut["jar_path"])
    if not exists(client, jar):
        raise FileNotFoundError(
            f"[SUT] spring-rest-service.jar not found at {jar}.\n"
            "It has no published release; build it once on the SUT:\n"
            "  git clone https://github.com/RETIT/opentelemetry-javaagent-extension.git\n"
            "  cd opentelemetry-javaagent-extension && mvn -DskipTests package\n"
            "  # -> examples/spring-rest-service/target/spring-rest-service.jar"
        )

    java_bin = str(sut.get("java_bin") or "java")
    code, _, _ = run(client, f"command -v {shlex.quote(java_bin)}", check=False)
    if code != 0 and not exists(client, java_bin):
        raise FileNotFoundError(f"[SUT] java not found at {java_bin}")

    # Nothing must already own the port, or the new JVM dies on bind.
    port = int(sut.get("port", 8081))
    code, out, _ = run(
        client,
        f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E '[:.]{port}\\b' || true",
        check=False,
    )
    if out.strip():
        raise RuntimeError(
            f"[SUT] port {port} is already in use:\n{out.strip()}\n"
            "Stop the stale process before starting a run."
        )

    if uses_jagent(agent):
        # Raises with guidance unless the session is root or sudo works.
        privilege_prefix(client)


def start_sut(
    client: paramiko.SSHClient,
    config: dict,
    remote_dir: str,
    log_path: str,
) -> RemoteProcess:
    """Start the Spring service on the SUT and return the process handle."""
    process = RemoteProcess(
        client=client,
        name="SUT",
        command=build_sut_command(config),
        remote_dir=remote_dir,
        log_path=log_path,
        env=(config.get("sut", {}) or {}).get("env") or {},
    )
    process.start()
    return process


def wait_until_ready(client: paramiko.SSHClient, config: dict, process: RemoteProcess) -> None:
    """Block until the service accepts connections on the SUT itself.

    Probing from the SUT rather than from the controller keeps the check
    independent of any firewall between the machines.
    """
    sut = config.get("sut", {})
    port = int(sut.get("port", 8081))
    timeout = int(config.get("experiment", {}).get("sut_ready_timeout", 180))

    print(f"[SUT] waiting up to {timeout}s for port {port} ...")
    started = time.time()
    deadline = started + timeout
    probe = f"timeout 2 bash -c '</dev/tcp/127.0.0.1/{port}'"

    while time.time() < deadline:
        if not process.is_running():
            raise RuntimeError(
                f"[SUT] exited before becoming ready. Last log lines:\n{process.tail()}"
            )
        code, _, _ = run(client, probe, check=False)
        if code == 0:
            print(f"[SUT] ready after {int(time.time() - started)}s")
            return
        time.sleep(2)

    raise TimeoutError(
        f"[SUT] not ready within {timeout}s. Last log lines:\n{process.tail()}"
    )


def warm_up(client: paramiko.SSHClient, config: dict) -> None:
    """Fire N requests so the JVM reaches a steady state before measuring.

    Issued on the SUT against loopback: the purpose is to settle JIT
    compilation, which does not depend on where the request originates. The
    paper reports only post-warm-up transactions.
    """
    experiment = config.get("experiment", {})
    count = int(experiment.get("warmup_requests", 0))
    if count <= 0:
        return

    sut = config.get("sut", {})
    port = int(sut.get("port", 8081))
    path = experiment.get("warmup_path", "/test-rest-endpoint/getData")
    url = f"http://127.0.0.1:{port}{path}"

    print(f"[SUT] warm-up: {count} requests to {url}")
    code, out, err = run(
        client,
        f"for i in $(seq 1 {count}); do "
        f"curl -s -o /dev/null -w '%{{http_code}}\\n' {shlex.quote(url)} || echo ERR; "
        f"done | sort | uniq -c",
        check=False,
        timeout=max(120, count * 2),
    )
    if code != 0:
        print(f"[SUT] warm-up reported an error (continuing): {err.strip()}")
    else:
        print(f"[SUT] warm-up response codes:\n{out.strip()}")


def remote_artifact(remote_dir: str, filename: str) -> str:
    """Absolute path of one artifact inside the run's remote directory."""
    return posixpath.join(remote_dir, filename)
