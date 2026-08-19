# helper/http_logger.py
"""
The http-logger, scraping the collector's Prometheus endpoint.

Runs on the controller for the duration of the load phase, exactly as in the ASE
automation, so the resulting CSV is comparable across both experiments.

Why sample at all, when the ``file`` exporter already records every point: both
tools emit *cumulative monotonic* sums, so the totals at the end of a run also
contain ramp-up and ramp-down traffic. A sampled series lets the steady-state
window be isolated by subtracting the counter value at its start from the value
at its end -- which a single final reading cannot do.

Note the Prometheus exposition munges metric names, so
``ebpf.jagent.resource.demand.cpu.ms`` appears as
``ebpf_jagent_resource_demand_cpu_ms``.
"""

from __future__ import annotations

from pathlib import Path

from helper.downloads import fetch_http_logger_jar
from helper.local_proc import LocalProcess


def resolve_jar(http_logger_config: dict, skip_downloads: bool = False) -> Path:
    """Locate the http-logger JAR, downloading the published release if needed."""
    return fetch_http_logger_jar(http_logger_config, skip=skip_downloads)


def start_http_logger(
    http_logger_config: dict,
    urls: list[str],
    output_path: Path,
    skip_downloads: bool = False,
) -> LocalProcess:
    """Start the http-logger against *urls*, writing its CSV to *output_path*.

    The tool prints ``DATA:<endpoint> at <timestamp>`` followed by the response
    body, so its stdout *is* the result file.
    """
    jar = resolve_jar(http_logger_config, skip_downloads=skip_downloads)
    cron = str(http_logger_config.get("cron", "*/5 * * * * ?"))

    command = [
        str(http_logger_config.get("java_bin") or "java"),
        # The scraper must not be attachable; it is measurement infrastructure.
        "-XX:+DisableAttachMechanism",
        f"-Dhttplogger.cron={cron}",
        "-jar",
        str(jar),
        *urls,
    ]

    print(f"[http-logger] scraping every '{cron}': {', '.join(urls)}")
    process = LocalProcess(name="http-logger", command=command, log_path=output_path)
    process.start()
    return process
