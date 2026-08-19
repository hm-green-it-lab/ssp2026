# helper/downloads.py
"""
Artifact staging.

The controller downloads the artifacts it can fetch from the internet into
``artifacts/`` and uploads them to the host that needs them:

- the OpenTelemetry Java agent and the RETIT OTJAE extension -> SUT host,
- the JMeter test plan -> load driver.

Staging from one place keeps all three machines on the same versions, and makes
a rerun offline once the first run has populated the cache.

Two things are deliberately *not* staged, because they must be built on the SUT
and are large or kernel-specific: the ``spring-rest-service.jar`` and the eBPF
jAgent itself. Their absence is reported by the preflight checks.
"""

from __future__ import annotations

import posixpath
import urllib.error
import urllib.request
from pathlib import Path

import paramiko

from helper.ssh import upload

HERE = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = HERE / "artifacts"


def _download(url: str, destination: Path) -> Path:
    """Download *url* to *destination* unless it is already cached."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        print(f"[stage] cached: {destination.name}")
        return destination

    print(f"[stage] downloading {url}")
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            tmp.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download {url}: {exc}") from exc

    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded an empty file from {url}")

    tmp.replace(destination)
    print(f"[stage] -> {destination}")
    return destination


def _local_copy(configured: str | None, url: str | None, default_name: str, skip: bool) -> Path:
    """Return a local path for one artifact, downloading it when needed."""
    path = Path(configured).expanduser() if configured else ARTIFACT_DIR / default_name
    if path.exists():
        return path
    if skip:
        raise FileNotFoundError(
            f"{path} is missing and downloads are disabled (--skip-downloads)"
        )
    if not url:
        raise FileNotFoundError(f"{path} is missing and no download URL is configured")
    return _download(url, path)


def stage_otjae(
    client: paramiko.SSHClient,
    config: dict,
    remote_dir: str,
    skip: bool = False,
) -> None:
    """Upload the OTel agent and RETIT extension to the SUT.

    Writes the resulting remote paths back into the config so
    :func:`helper.sut.build_sut_command` can reference them.
    """
    otjae = config.setdefault("otjae", {})

    agent = _local_copy(
        otjae.get("local_otel_agent_jar"),
        otjae.get("otel_agent_url"),
        "opentelemetry-javaagent.jar",
        skip,
    )
    extension = _local_copy(
        otjae.get("local_extension_jar"),
        otjae.get("extension_url"),
        "io.retit.opentelemetry.javaagent.extension.jar",
        skip,
    )

    remote_agent = posixpath.join(remote_dir, agent.name)
    remote_extension = posixpath.join(remote_dir, extension.name)

    upload(client, agent, remote_agent)
    upload(client, extension, remote_extension)

    otjae["remote_otel_agent_jar"] = remote_agent
    otjae["remote_extension_jar"] = remote_extension


def fetch_http_logger_jar(http_logger_config: dict, skip: bool = False) -> Path:
    """Download the http-logger runner JAR onto the controller."""
    return _local_copy(
        http_logger_config.get("local_jar"),
        http_logger_config.get("jar_url"),
        "http-logger-1.0-runner.jar",
        skip,
    )


def fetch_release_archive(jagent_config: dict, skip: bool = False) -> Path:
    """Download the published eBPF jAgent release tarball onto the controller.

    Cached in ``artifacts/`` and named after the URL, so pinning a different tag
    fetches a separate file rather than silently reusing the old one.
    """
    url = jagent_config.get("release_url")
    configured = jagent_config.get("local_release_archive")

    if configured:
        path = Path(configured).expanduser()
    elif url:
        path = ARTIFACT_DIR / url.rstrip("/").rsplit("/", 1)[-1]
    else:
        raise ValueError(
            "provision: release requires jagent.release_url "
            "(or a local jagent.local_release_archive)"
        )

    if path.exists():
        print(f"[stage] cached: {path.name}")
        return path
    if skip:
        raise FileNotFoundError(
            f"{path} is missing and downloads are disabled (--skip-downloads)"
        )
    if not url:
        raise FileNotFoundError(f"{path} is missing and no jagent.release_url is configured")
    return _download(url, path)


def stage_test_plan(
    jmeter_config: dict,
    client: paramiko.SSHClient | None = None,
    remote_dir: str | None = None,
    skip: bool = False,
) -> str:
    """Resolve the JMeter test plan for whichever host will run it.

    Sets ``jmeter.resolved_test_plan`` to a controller path when JMeter runs
    locally, or to the uploaded path on the load driver when it runs remotely.
    """
    plan = _local_copy(
        jmeter_config.get("local_test_plan"),
        jmeter_config.get("test_plan_url"),
        "jmeter_testplan.jmx",
        skip,
    )

    if client is None or remote_dir is None:
        jmeter_config["resolved_test_plan"] = str(plan)
        return str(plan)

    remote_plan = posixpath.join(remote_dir, plan.name)
    upload(client, plan, remote_plan)
    jmeter_config["resolved_test_plan"] = remote_plan
    return remote_plan
