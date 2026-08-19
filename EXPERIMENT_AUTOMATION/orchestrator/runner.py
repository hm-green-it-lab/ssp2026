# orchestrator/runner.py
"""
Experiment runner.

============  ==========================================================
Controller    runs this code; holds credentials, stages artifacts, and
              collects every result into ``output/``. Also drives the load
              itself when ``jmeter.location: controller`` (the default).
Load driver   an optional dedicated JMeter host, used when
              ``jmeter.location: remote``
SUT           runs the Spring service plus the instrumentation under test
              (OTJAE in-process, or the eBPF jAgent attached from outside)
============  ==========================================================

Either way the load generator is a different machine from the SUT, so it does
not compete with the traced JVM for CPU.

One iteration executes strictly in this order, because each step depends on
the previous one:

1. start the SUT (the agent variant is baked into its JVM arguments),
2. wait until it accepts connections,
3. warm up, so JIT compilation has settled before anything is measured,
4. attach the eBPF jAgent -- only possible now, since it needs the JVM's PID,
5. run JMeter for ``2 * ramp + steady`` seconds,
6. stop the agent with SIGINT so it flushes, then stop the SUT,
7. collect every artifact and write a run manifest. A controller-side JMeter
   writes straight into the output directory, so only the SUT's files (and a
   remote driver's, if used) need downloading.

Step 3 deliberately precedes step 4: warming up with the tracer already
attached would both distort the overhead measurement and fill the trace with
transactions the paper excludes.
"""

from __future__ import annotations

import datetime
import json
import platform
import posixpath
import time
from pathlib import Path

import paramiko

from helper import collector as collector_helper
from helper import http_logger as http_logger_helper
from helper import jagent as jagent_helper
from helper import jmeter as jmeter_helper
from helper import sut as sut_helper
from helper.collector import Collector
from helper.downloads import stage_otjae, stage_test_plan
from helper.local_proc import LocalProcess
from helper.naming import build_filename, ts
from helper.ssh import RemoteProcess, connect, credentials, download, ensure_dir, run

HERE = Path(__file__).resolve().parent.parent


def _output_dir(
    config: dict, experiment_type: str, stamp: str, repetition: int | None = None
) -> Path:
    """Create and return the run's output directory.

    The campaign repetition is part of the name because it is the *blocking
    factor*: pairing a variant against its baseline requires knowing which runs
    belong to the same block, and a bare timestamp does not say.
    """
    configured = config.get("experiment", {}).get("local_output_directory", "./output")
    root = Path(configured)
    if not root.is_absolute():
        root = HERE / root
    tag = f"rep{repetition:02d}_" if repetition is not None else ""
    path = root / f"{stamp}_{tag}{experiment_type}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dry_run(config: dict, experiment_type: str) -> None:
    """Print the resolved topology and commands without touching any host."""
    sut = config.get("sut", {})
    jmeter_config = config.get("jmeter", {})

    jmeter_where = (
        f"{jmeter_config.get('target_host')} (remote)"
        if jmeter_helper.location(jmeter_config) == "remote"
        else "this controller"
    )

    print(f"\n=== dry run: {experiment_type} ===")
    print("\nTopology:")
    print(f"  controller  : this machine ({platform.node()})")
    print(f"  load driver : {jmeter_where}")
    print(f"  SUT         : {sut.get('target_host')}  (agent: {sut.get('agent')})")

    print("\nSUT command (on the SUT host):")
    resolved = json.loads(json.dumps(config))
    resolved.setdefault("otjae", {}).update(
        {
            "remote_otel_agent_jar": "<staged>/opentelemetry-javaagent.jar",
            "remote_extension_jar": "<staged>/io.retit.opentelemetry.javaagent.extension.jar",
        }
    )
    print("  " + sut_helper.build_sut_command(resolved))

    if sut_helper.uses_jagent(sut.get("agent", "none")):
        jagent_config = config.get("jagent", {})
        mode = str(jagent_config.get("provision", "release")).lower()
        install_dir = jagent_config.get("install_dir")
        print(f"\njAgent provisioning (mode: {mode}):")
        if mode == "release":
            print(f"  download : {jagent_config.get('release_url')}")
            print(f"  unpack   -> {install_dir} (SUT); binary located inside")
        elif mode == "noop":
            print(f"  ship     : ./noop_agent (empty control agent, controller)")
            print(f"           -> {install_dir}-noop (SUT)")
            print("  build    : make clean all on the SUT")
        elif mode == "source":
            print(f"  sync     : {jagent_config.get('source_dir')} (controller)")
            print(f"           -> {install_dir} (SUT)")
            print("  build    : make clean all on the SUT")
        else:
            print(f"  assumed present at {jagent_config.get('binary') or install_dir} on the SUT")

        # Mirrors helper/jagent.py::start_agent. Both optional flags change what
        # is measured -- --probes decides which kernel-wide probes get attached,
        # --min-duration-us decides which calls reach the emit path -- so a dry
        # run that omitted them would misrepresent the experiment.
        flags = f"-p <pid> -f {jagent_config.get('filter')}"
        probes = jagent_config.get("probes")
        if probes:
            if isinstance(probes, (list, tuple)):
                probes = ",".join(str(p) for p in probes)
            flags += f" --probes '{probes}'"
        if jagent_config.get("min_duration_us"):
            flags += f" --min-duration-us {int(jagent_config['min_duration_us'])}"
        trace = "<trace file>" if jagent_config.get("write_trace", False) else "/dev/null"

        print("\njAgent command (on the SUT host, binary and PID resolved at runtime):")
        print(
            f"  sudo -n env JVM_LIB_PATH={jagent_config.get('jvm_lib_path')} "
            f"OTLP_ENDPOINT={jagent_config.get('otlp_endpoint')} "
            f"<binary> {flags} {trace}"
        )

    if jmeter_config.get("enabled", True):
        print(f"\nJMeter command (on {jmeter_where}):")
        print("  " + jmeter_helper.describe_command(jmeter_config))
        print(f"\nRun duration: {jmeter_helper.run_duration_secs(jmeter_config)}s")
    print()


def _fetch(client: paramiko.SSHClient, remote_dir: str, filename: str, output_dir: Path) -> None:
    """Download one artifact into the local output directory."""
    download(client, posixpath.join(remote_dir, filename), output_dir / filename)


def run_experiment(
    config: dict,
    experiment_type: str,
    skip_downloads: bool = False,
    dry_run: bool = False,
    repetition: int | None = None,
    position: int | None = None,
) -> None:
    """Run every configured iteration of one experiment."""
    if dry_run:
        _dry_run(config, experiment_type)
        return

    experiment = config.get("experiment", {})
    sut_config = config.get("sut", {})
    jmeter_config = dict(config.get("jmeter", {}) or {})

    iterations = int(experiment.get("iterations", 1))
    wait_between = int(experiment.get("wait_between_runs", 0))
    agent_variant = str(sut_config.get("agent", "none")).lower()
    jmeter_enabled = bool(jmeter_config.get("enabled", True))
    duration = jmeter_helper.run_duration_secs(jmeter_config) if jmeter_enabled else 0

    jmeter_location = jmeter_helper.location(jmeter_config) if jmeter_enabled else "controller"
    jmeter_is_remote = jmeter_enabled and jmeter_location == "remote"

    sut_user, sut_password = credentials("SUT")
    sut_remote_root = str(sut_config["remote_dir"])
    jmeter_remote_root = str(jmeter_config["remote_dir"]) if jmeter_is_remote else None

    stamp = ts()
    output_dir = _output_dir(config, experiment_type, stamp, repetition)

    if not jmeter_enabled:
        driver = "-"
    elif jmeter_is_remote:
        driver = f"{jmeter_config.get('target_host')} (remote)"
    else:
        driver = "this controller"

    print(
        f"\n[*] {experiment_type}: {iterations} iteration(s)\n"
        f"    SUT         : {sut_config['target_host']} (agent: {agent_variant})\n"
        f"    load driver : {driver}\n"
        f"    output      : {output_dir}"
    )

    sut_client = connect(str(sut_config["target_host"]), sut_user, sut_password)
    jmeter_client: paramiko.SSHClient | None = None
    try:
        if jmeter_is_remote:
            jmeter_user, jmeter_password = credentials("JMETER")
            jmeter_client = connect(
                str(jmeter_config["target_host"]), jmeter_user, jmeter_password
            )

        # ── resolve the telemetry endpoints before anything is configured ────
        collector_config = config.setdefault("collector", {})
        # `none` and `usdt` carry no exporter, so there is nothing for a
        # collector to receive; requiring a container runtime for them would be
        # a dependency with no purpose.
        collector_config["__needed__"] = (
            sut_helper.uses_otjae(agent_variant) or sut_helper.uses_jagent(agent_variant)
        )
        if collector_config.get("enabled", True) and collector_config["__needed__"]:
            # Both exporters are pointed at the controller-side collector, so
            # OTJAE and the jAgent travel the same path and stay comparable.
            # The exact address is settled per iteration, once the collector is
            # listening and its reachability from the SUT can be probed.
            otjae_props = config.setdefault("otjae", {}).setdefault("properties", {})
            otjae_props["otel.exporter.otlp.protocol"] = "http/protobuf"
            for signal in ("traces", "metrics", "logs"):
                otjae_props[f"otel.{signal}.exporter"] = "otlp"

        # ── stage artifacts once per experiment ──────────────────────────────
        ensure_dir(sut_client, sut_remote_root)
        if sut_helper.uses_otjae(agent_variant):
            stage_otjae(sut_client, config, sut_remote_root, skip=skip_downloads)
        if jmeter_enabled:
            if jmeter_client and jmeter_remote_root:
                ensure_dir(jmeter_client, jmeter_remote_root)
                stage_test_plan(
                    jmeter_config, jmeter_client, jmeter_remote_root, skip=skip_downloads
                )
            else:
                stage_test_plan(jmeter_config, skip=skip_downloads)

        # ── preflight every host before burning a 4-minute run ───────────────
        print("[~] Preflight checks ...")
        sut_helper.preflight(sut_client, config)
        if sut_helper.uses_jagent(agent_variant):
            jagent_helper.preflight(sut_client, config, skip_downloads=skip_downloads)
        if jmeter_enabled:
            if jmeter_client:
                jmeter_helper.preflight_remote(jmeter_client, jmeter_config)
            else:
                jmeter_helper.preflight_local(jmeter_config)
        print("[v] Preflight OK")

        for iteration in range(1, iterations + 1):
            _run_iteration(
                config=config,
                experiment_type=experiment_type,
                iteration=iteration,
                iterations=iterations,
                duration=duration,
                agent_variant=agent_variant,
                sut_client=sut_client,
                sut_remote_root=sut_remote_root,
                jmeter_client=jmeter_client,
                jmeter_config=jmeter_config,
                jmeter_remote_root=jmeter_remote_root,
                jmeter_enabled=jmeter_enabled,
                output_dir=output_dir,
                skip_downloads=skip_downloads,
                repetition=repetition,
                position=position,
            )

            if iteration < iterations and wait_between > 0:
                print(f"[~] Waiting {wait_between}s before the next iteration ...")
                time.sleep(wait_between)

    finally:
        sut_client.close()
        if jmeter_client:
            jmeter_client.close()

    print(f"\n[*] Done. Artifacts in {output_dir}\n")


def _run_iteration(
    config: dict,
    experiment_type: str,
    iteration: int,
    iterations: int,
    duration: int,
    agent_variant: str,
    sut_client: paramiko.SSHClient,
    sut_remote_root: str,
    jmeter_client: paramiko.SSHClient | None,
    jmeter_config: dict,
    jmeter_remote_root: str | None,
    jmeter_enabled: bool,
    output_dir: Path,
    skip_downloads: bool = False,
    repetition: int | None = None,
    position: int | None = None,
) -> None:
    """Execute a single iteration and collect its artifacts."""
    started = datetime.datetime.now()
    print(
        f"\n{'=' * 70}\n"
        f"[*] Iteration {iteration}/{iterations} of {experiment_type}\n"
        f"    start: {started:%Y-%m-%d %H:%M:%S}"
        + (
            f"  |  expected end: "
            f"{started + datetime.timedelta(seconds=duration):%H:%M:%S} (+{duration}s load)"
            if duration
            else ""
        )
        + f"\n{'=' * 70}"
    )

    run_stamp = ts()
    names = {
        "sut": build_filename("sut", experiment_type, run_stamp, iteration, iterations, ".log"),
        "jagent_log": build_filename(
            "jagent", experiment_type, run_stamp, iteration, iterations, ".log"
        ),
        "jagent_trace": build_filename(
            "jagent-trace", experiment_type, run_stamp, iteration, iterations, ".txt"
        ),
        "collector_telemetry": build_filename(
            "collector-telemetry", experiment_type, run_stamp, iteration, iterations, ".jsonl"
        ),
        "collector_log": build_filename(
            "collector", experiment_type, run_stamp, iteration, iterations, ".log"
        ),
        "collector_config": build_filename(
            "collector-config", experiment_type, run_stamp, iteration, iterations, ".yaml"
        ),
        "http_logger": build_filename(
            "http-logger", experiment_type, run_stamp, iteration, iterations, ".csv"
        ),
        "jtl": build_filename("jmeter", experiment_type, run_stamp, iteration, iterations, ".jtl"),
        "jmeter_log": build_filename(
            "jmeter", experiment_type, run_stamp, iteration, iterations, ".log"
        ),
        "jmeter_stdout": build_filename(
            "jmeter", experiment_type, run_stamp, iteration, iterations, ".stdout.log"
        ),
        "manifest": build_filename(
            "manifest", experiment_type, run_stamp, iteration, iterations, ".json"
        ),
    }

    sut_dir = posixpath.join(sut_remote_root, run_stamp)
    jmeter_dir = posixpath.join(jmeter_remote_root, run_stamp) if jmeter_remote_root else None

    sut_process: RemoteProcess | None = None
    agent_process: RemoteProcess | None = None
    jmeter_process: RemoteProcess | LocalProcess | None = None
    collector: Collector | None = None
    tunnel = None
    http_logger_process: LocalProcess | None = None
    status = "ok"
    error: str | None = None

    collector_config = config.get("collector", {}) or {}
    http_logger_config = config.get("http_logger", {}) or {}

    try:
        ensure_dir(sut_client, sut_dir)

        # 0) the collector must be listening before the SUT starts exporting
        if collector_config.get("enabled", True) and collector_config.get("__needed__", True):
            collector = Collector(
                collector_config,
                config_path=output_dir / names["collector_config"],
                output_dir=output_dir,
                log_path=output_dir / names["collector_log"],
            )
            collector_helper.render_config(
                collector_config,
                collector.config_path,
                output_dir / names["collector_telemetry"],
            )
            collector.start()

            # Settle how the SUT reaches the collector before the JVM starts,
            # since OTJAE takes the endpoint as a JVM argument.
            endpoint, tunnel = collector_helper.resolve_endpoint(
                sut_client, collector_config, str(config["sut"]["target_host"])
            )
            config.setdefault("jagent", {})["otlp_endpoint"] = f"{endpoint}/v1/metrics"
            config.setdefault("otjae", {}).setdefault("properties", {})[
                "otel.exporter.otlp.endpoint"
            ] = endpoint

        # 1) + 2) start the service and wait for it
        sut_process = sut_helper.start_sut(
            sut_client, config, sut_dir, posixpath.join(sut_dir, names["sut"])
        )
        sut_helper.wait_until_ready(sut_client, config, sut_process)

        # 3) warm up before the tracer attaches
        sut_helper.warm_up(sut_client, config)

        # 4) attach the eBPF agent, which needs the JVM PID
        if sut_helper.uses_jagent(agent_variant):
            agent_process = jagent_helper.start_agent(
                sut_client,
                config,
                target_pid=sut_process.pid,
                remote_dir=sut_dir,
                trace_path=posixpath.join(sut_dir, names["jagent_trace"]),
                log_path=posixpath.join(sut_dir, names["jagent_log"]),
            )

        # 5) start the Prometheus scraper, then drive load. The scraper covers
        #    only the load phase, so its series brackets the measured window.
        if http_logger_config.get("enabled", True) and collector:
            http_logger_process = http_logger_helper.start_http_logger(
                http_logger_config,
                urls=list(http_logger_config.get("urls") or [])
                or [collector_helper.prometheus_url(collector_config)],
                output_path=output_dir / names["http_logger"],
                skip_downloads=skip_downloads,
            )

        if jmeter_enabled:
            if jmeter_client and jmeter_dir:
                ensure_dir(jmeter_client, jmeter_dir)
                jmeter_process = jmeter_helper.start_jmeter_remote(
                    jmeter_client,
                    jmeter_config,
                    remote_dir=jmeter_dir,
                    results_path=posixpath.join(jmeter_dir, names["jtl"]),
                    jmeter_log_path=posixpath.join(jmeter_dir, names["jmeter_log"]),
                    stdout_log_path=posixpath.join(jmeter_dir, names["jmeter_stdout"]),
                )
            else:
                # Local run writes straight into the output directory, so there
                # is nothing to fetch afterwards.
                jmeter_process = jmeter_helper.start_jmeter_local(
                    jmeter_config,
                    results_path=output_dir / names["jtl"],
                    jmeter_log_path=output_dir / names["jmeter_log"],
                    stdout_log_path=output_dir / names["jmeter_stdout"],
                )

            # The schedule only begins once JMeter has booted its JVM and
            # parsed the plan, which is not part of `duration` -- on a Windows
            # controller that is easily 30s. Budget for it explicitly, or a
            # perfectly good run gets killed just before it finishes.
            grace = int(jmeter_config.get("summary_wait_secs", 60))
            startup = int(jmeter_config.get("startup_allowance_secs", 90))
            budget = startup + duration + grace
            print(
                f"[JMeter] running; waiting up to {budget}s "
                f"({startup}s start-up + {duration}s schedule + {grace}s flush)"
            )
            deadline = time.time() + budget
            while time.time() < deadline and jmeter_process.is_running():
                if sut_process and not sut_process.is_running():
                    raise RuntimeError(
                        "[SUT] died during the load phase. Last log lines:\n"
                        + sut_process.tail()
                    )
                time.sleep(5)

            if jmeter_process.is_running():
                print("[JMeter] exceeded its expected duration; stopping it")
                jmeter_process.stop(timeout=30)
                status = "jmeter_timeout"
        else:
            print(f"[*] No load configured; idling for {duration}s")
            time.sleep(duration)

    except Exception as exc:  # noqa: BLE001 - recorded, then cleaned up below
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        print(f"\n[!] Iteration {iteration} failed: {error}")

    finally:
        # 6) tear down in reverse order: load first, then the agent so it can
        #    flush while the JVM is still alive, then the service, and the
        #    collector last of all so it receives the final exports and gets a
        #    chance to flush its batches to the file exporter.
        if jmeter_process:
            jmeter_process.stop(timeout=30)
        if http_logger_process:
            http_logger_process.stop(timeout=15)
        if agent_process:
            jagent_helper.stop_agent(
                agent_process, timeout=int(config.get("jagent", {}).get("stop_timeout", 30))
            )
        if sut_process:
            sut_process.stop(timeout=int(config.get("sut", {}).get("stop_timeout", 30)))
        if tunnel:
            tunnel.stop()
        if collector:
            collector.stop()

        # 7) collect everything onto the controller
        print("[~] Fetching artifacts ...")
        write_trace = bool(config.get("jagent", {}).get("write_trace", False))
        for key in ("sut", "jagent_trace", "jagent_log"):
            if key.startswith("jagent") and not sut_helper.uses_jagent(agent_variant):
                continue
            if key == "jagent_trace" and not write_trace:
                continue
            _fetch(sut_client, sut_dir, names[key], output_dir)
        if jmeter_client and jmeter_dir:
            for key in ("jtl", "jmeter_log", "jmeter_stdout"):
                _fetch(jmeter_client, jmeter_dir, names[key], output_dir)

        if config.get("experiment", {}).get("cleanup_remote", True):
            run(sut_client, f"rm -rf {sut_dir}", check=False)
            if jmeter_client and jmeter_dir:
                run(jmeter_client, f"rm -rf {jmeter_dir}", check=False)

        (output_dir / names["manifest"]).write_text(
            json.dumps(
                {
                    "experiment_type": experiment_type,
                    "agent_variant": agent_variant,
                    # Blocking factor: which runs may be paired with each other.
                    "campaign_repetition": repetition,
                    # Order within the block, for checking residual order effects.
                    "campaign_position": position,
                    "iteration": iteration,
                    "total_iterations": iterations,
                    "started": started.isoformat(timespec="seconds"),
                    "finished": datetime.datetime.now().isoformat(timespec="seconds"),
                    "status": status,
                    "error": error,
                    "load_duration_secs": duration,
                    "hosts": {
                        "sut": config.get("sut", {}).get("target_host"),
                        "jmeter": (
                            jmeter_config.get("target_host")
                            if jmeter_client
                            else f"controller ({platform.node()})"
                        ),
                    },
                    "artifacts": names,
                    "config": config,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    print(f"[{'x' if status != 'ok' else 'v'}] Iteration {iteration} finished ({status})")
