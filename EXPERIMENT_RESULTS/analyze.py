#!/usr/bin/env python3
"""
analyze.py -- steady-state analysis of the eBPF jAgent / OTJAE experiments.

Reads the run directories written by EXPERIMENT_AUTOMATION and reports, per
variant:

* response time from the JMeter JTL,
* per-transaction CPU and memory from the collector's OTLP JSON,
* the paired comparison against the uninstrumented baseline.

Everything is restricted to the **steady-state window** by default. The test
plan ramps the arrival rate 0 -> total_rate over ``ramp_up_and_down`` seconds,
holds it for ``steady_state``, then ramps back down, so samples from the ramps
were taken at a different offered load than the ones in between. Including them
inflates the spread with variation that is a property of the schedule rather
than of the instrumentation.

The window is anchored on the exact schedule start that JMeter logs
("Starting standalone test @ ... (epoch_ms)") rather than on the first sample,
because at the start of the ramp the rate is still near zero and the first
arrival can be seconds late.

Per-transaction demand comes from different places for the two tools:

jAgent
    Cumulative OTLP counters with one datapoint per transaction; consecutive
    datapoints are differenced.
OTJAE
    Span attributes (``io.retit.startcputime`` / ``endcputime`` and the
    heap-allocation pair) on the ``@WithSpan`` method span.

Usage::

    python analyze.py                        # summary of every run
    python analyze.py --baseline none        # paired stats vs the baseline
    python analyze.py --full                 # do not restrict to steady state
    python analyze.py --output-dir <path>
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import statistics as st
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE.parent / "EXPERIMENT_AUTOMATION" / "output"

# "Starting standalone test @ 2026 Aug 18 00:14:09 GMT+02:00 (1787004849904)"
SCHEDULE_START = re.compile(r"Starting standalone test @ .*?\((\d+)\)")

METHOD_SPAN_HINT = "veryComplexBusinessFunction"

# Keys into Run.demands. Named rather than repeated as literals: the paired
# analysis has to look up exactly what _load_demand stored, and a typo in one of
# the two places would silently produce "no pairs" rather than an error.
SOURCE_OTJAE = "OTJAE spans"
SOURCE_JAGENT = "jAgent counters"


# ──────────────────────────────────────────────────────────────────────────────
# Loading
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Run:
    """One experiment run and the measurements extracted from it."""

    path: Path
    variant: str
    status: str
    repetition: int | None
    position: int | None
    probes: str | None
    rate: int
    ramp: int
    steady: int
    window: tuple[int, int] | None          # epoch ms, inclusive
    elapsed: list[int] = field(default_factory=list)
    errors: int = 0
    driven: int = 0                          # samples before windowing
    # source name -> {"cpu": [...], "memory": [...]}. A run normally carries one
    # source, but the `both` control has two: the whole point of that variant is
    # comparing them inside a single run, so keeping only one would discard the
    # measurement it exists to make.
    demands: dict[str, dict[str, list[float]]] = field(default_factory=dict)

    @property
    def cpu_ms(self) -> list[float]:
        """CPU from the primary source, for runs carrying only one."""
        return next(iter(self.demands.values()), {}).get("cpu", [])

    @property
    def label(self) -> str:
        rep = f"rep{self.repetition:02d}" if self.repetition is not None else "rep--"
        probes = f" [{self.probes}]" if self.probes else ""
        return f"{self.variant}{probes} {rep}"


def _first(pattern: str, directory: Path) -> Path | None:
    """Return the first file in *directory* matching *pattern*."""
    hits = sorted(glob.glob(str(directory / pattern)))
    return Path(hits[0]) if hits else None


def _schedule_start_ms(run_dir: Path) -> int | None:
    """Exact epoch-ms at which JMeter began executing the schedule."""
    log = _first("jmeter_*.stdout.log", run_dir)
    if not log:
        return None
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = SCHEDULE_START.search(text)
    return int(match.group(1)) if match else None


def _variant_of(run_dir: Path, manifest: dict) -> str:
    """Short variant name, e.g. ``jagent``."""
    experiment = manifest.get("experiment_type") or run_dir.name
    return experiment.replace("spring_remote_", "")


def load_run(run_dir: Path, steady_only: bool = True) -> Run | None:
    """Load one run directory; returns None when there is nothing usable."""
    manifest_path = _first("manifest_*.json", run_dir)
    if not manifest_path:
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = manifest.get("config", {})
    props = config.get("jmeter", {}).get("props", {})

    ramp = int(props.get("ramp_up_and_down", 60))
    steady = int(props.get("steady_state", 120))

    window = None
    if steady_only:
        start = _schedule_start_ms(run_dir)
        if start is None:
            # Fall back to the first sample. Less exact -- the first arrival can
            # lag the schedule start because the rate begins at zero -- so the
            # window is shifted slightly late rather than being wrong in kind.
            jtl = _first("jmeter_*.jtl", run_dir)
            if jtl:
                stamps = [s for s, _, _ in _read_jtl(jtl)]
                start = min(stamps) if stamps else None
        if start is not None:
            window = (start + ramp * 1000, start + (ramp + steady) * 1000)

    run = Run(
        path=run_dir,
        variant=_variant_of(run_dir, manifest),
        status=manifest.get("status", "?"),
        repetition=manifest.get("campaign_repetition"),
        position=manifest.get("campaign_position"),
        probes=(config.get("jagent", {}) or {}).get("probes"),
        rate=int(props.get("total_rate", 0)),
        ramp=ramp,
        steady=steady,
        window=window,
    )

    jtl = _first("jmeter_*.jtl", run_dir)
    if jtl:
        samples = _read_jtl(jtl)
        run.driven = len(samples)
        for stamp, elapsed, ok in samples:
            if window and not (window[0] <= stamp <= window[1]):
                continue
            run.elapsed.append(elapsed)
            if not ok:
                run.errors += 1

    telemetry = _first("collector_telemetry_*.jsonl", run_dir)
    if telemetry and telemetry.stat().st_size:
        _load_demand(run, telemetry)

    return run


def _read_jtl(path: Path) -> list[tuple[int, int, bool]]:
    """Read (timeStamp, elapsed, success) triples, tolerating a truncated tail."""
    rows: list[tuple[int, int, bool]] = []
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for record in csv.DictReader(handle):
                try:
                    rows.append((int(record["timeStamp"]), int(record["elapsed"]),
                                 record["success"].lower() == "true"))
                except (KeyError, ValueError, AttributeError):
                    continue  # partially written final line of a live run
    except OSError:
        pass
    return rows


def _load_demand(run: Run, telemetry: Path) -> None:
    """Extract per-transaction CPU and memory from the collector's OTLP JSON."""
    jagent_series: dict[tuple[str, str], list[tuple[int, float]]] = {}
    otjae_cpu: list[tuple[int, float]] = []
    otjae_mem: list[tuple[int, float]] = []

    with telemetry.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            for resource in payload.get("resourceMetrics", []):
                for scope in resource.get("scopeMetrics", []):
                    for metric in scope.get("metrics", []):
                        name = metric.get("name", "")
                        if not name.startswith("ebpf.jagent.resource.demand"):
                            continue
                        if "process" in name:
                            continue
                        for point in metric.get("sum", {}).get("dataPoints", []):
                            attrs = {a["key"]: list(a["value"].values())[0]
                                     for a in point.get("attributes", [])}
                            key = (name, attrs.get("method.name", ""))
                            value = point.get("asDouble", point.get("asInt"))
                            if value is None:
                                continue
                            jagent_series.setdefault(key, []).append(
                                (int(point.get("timeUnixNano", 0)), float(value)))

            for resource in payload.get("resourceSpans", []):
                for scope in resource.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        if METHOD_SPAN_HINT not in span.get("name", ""):
                            continue
                        attrs = {a["key"]: list(a["value"].values())[0]
                                 for a in span.get("attributes", [])}
                        try:
                            cpu = (int(attrs["io.retit.endcputime"])
                                   - int(attrs["io.retit.startcputime"])) / 1e6
                            mem = (int(attrs["io.retit.endheapbyteallocation"])
                                   - int(attrs["io.retit.startheapbyteallocation"]))
                            when = int(span.get("startTimeUnixNano", 0))
                        except (KeyError, ValueError):
                            continue
                        otjae_cpu.append((when, cpu))
                        otjae_mem.append((when, mem))

    def in_window(nanos: int) -> bool:
        if not run.window:
            return True
        return run.window[0] <= nanos / 1e6 <= run.window[1]

    # Both blocks run, and neither returns early. In the `both` control the two
    # agents are attached to one JVM and the telemetry file carries OTJAE spans
    # *and* jAgent counters for the same transactions; returning on the first
    # source found would drop exactly the comparison that run is for.
    if otjae_cpu:
        run.demands[SOURCE_OTJAE] = {
            "cpu": [v for t, v in otjae_cpu if in_window(t)],
            "memory": [v for t, v in otjae_mem if in_window(t)],
        }

    if jagent_series:
        collected: dict[str, list[float]] = {}
        for (name, method), points in jagent_series.items():
            if METHOD_SPAN_HINT not in method:
                continue
            points.sort()
            # Difference the full series, then keep the differences whose end
            # falls inside the window: differencing first avoids attributing a
            # window-spanning step to a single transaction.
            deltas = [(b[0], b[1] - a[1]) for a, b in zip(points, points[1:])
                      if b[1] >= a[1]]
            values = [v for t, v in deltas if in_window(t)]
            if name.endswith("cpu.ms"):
                collected["cpu"] = values
            elif name.endswith("memory.bytes"):
                collected["memory"] = values
        if collected:
            run.demands[SOURCE_JAGENT] = {
                "cpu": collected.get("cpu", []),
                "memory": collected.get("memory", []),
            }


# ──────────────────────────────────────────────────────────────────────────────
# Statistics
# ──────────────────────────────────────────────────────────────────────────────

def summarize(values: list[float]) -> dict:
    """Descriptive statistics for one sample."""
    if not values:
        return {}
    ordered = sorted(values)
    mean = st.mean(ordered)
    stdev = st.pstdev(ordered)

    def quantile(fraction: float) -> float:
        return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]

    return {
        "n": len(ordered),
        "mean": mean,
        "median": st.median(ordered),
        "p95": quantile(0.95),
        "p99": quantile(0.99),
        "cv": stdev / mean if mean else 0.0,
    }


def wilcoxon_signed_rank(differences: list[float]) -> tuple[float, float] | None:
    """Exact two-sided Wilcoxon signed-rank test on paired differences.

    Exact rather than normal-approximated because these designs have few
    repetitions, where the approximation is poor. Enumerating every sign
    assignment is 2**n, so it is capped; zero differences are dropped, as the
    test requires.
    """
    nonzero = [d for d in differences if d != 0]
    n = len(nonzero)
    if n == 0 or n > 20:
        return None

    magnitudes = sorted((abs(d), i) for i, d in enumerate(nonzero))
    ranks = [0.0] * n
    index = 0
    while index < len(magnitudes):
        stop = index
        while stop + 1 < len(magnitudes) and magnitudes[stop + 1][0] == magnitudes[index][0]:
            stop += 1
        average = (index + stop) / 2 + 1  # average rank for ties
        for k in range(index, stop + 1):
            ranks[magnitudes[k][1]] = average
        index = stop + 1

    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    total = sum(ranks)
    observed = min(w_plus, total - w_plus)

    # Exact null: every sign assignment is equally likely.
    at_least_as_extreme = 0
    for signs in product((0, 1), repeat=n):
        positive = sum(r for r, s in zip(ranks, signs) if s)
        if min(positive, total - positive) <= observed + 1e-9:
            at_least_as_extreme += 1
    p_value = at_least_as_extreme / (2 ** n)
    return w_plus, min(p_value, 1.0)


def cohens_dz(differences: list[float]) -> float | None:
    """Paired effect size: mean difference over its own standard deviation."""
    if len(differences) < 2:
        return None
    spread = st.stdev(differences)
    return st.mean(differences) / spread if spread else math.inf


def rank_biserial(differences: list[float]) -> float | None:
    """Matched-pairs rank-biserial correlation, the effect size native to Wilcoxon."""
    nonzero = [d for d in differences if d != 0]
    if not nonzero:
        return None
    result = wilcoxon_signed_rank(nonzero)
    if not result:
        return None
    w_plus, _ = result
    n = len(nonzero)
    total = n * (n + 1) / 2
    return 2 * w_plus / total - 1


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────

def print_runs(runs: list[Run], steady_only: bool) -> None:
    """One row per run: response time plus per-transaction demand."""
    scope = "steady state only" if steady_only else "whole run (ramps included)"
    print(f"\n=== response time, {scope} ===")
    print(f"{'run':<30}{'n':>7}{'kept':>7}{'err':>5}{'mean':>9}{'median':>9}"
          f"{'p95':>8}{'p99':>8}{'CV':>7}")
    for run in runs:
        stats = summarize(run.elapsed)
        if not stats:
            print(f"{run.label:<30}  (no samples; status={run.status})")
            continue
        kept = f"{100 * stats['n'] / run.driven:.0f}%" if run.driven else "-"
        print(f"{run.label:<30}{run.driven:>7}{kept:>7}{run.errors:>5}"
              f"{stats['mean']:>9.2f}{stats['median']:>9.1f}"
              f"{stats['p95']:>8.0f}{stats['p99']:>8.0f}{stats['cv']:>7.3f}")

    if not any(run.demands for run in runs):
        return

    print(f"\n=== per-transaction demand, matched on {METHOD_SPAN_HINT} ===")
    print(f"{'run':<30}{'source':<17}{'n':>7}{'cpu mean':>10}{'cpu med':>9}"
          f"{'cv':>7}{'mem mean':>11}{'mem med':>10}")
    for run in runs:
        # One row per source. The `both` control emits two, which is the
        # comparison it exists to make; every other variant emits one.
        for source, values in run.demands.items():
            cpu = summarize(values.get("cpu", []))
            if not cpu:
                continue
            mem = summarize(values.get("memory", []))
            print(f"{run.label:<30}{source:<17}{cpu['n']:>7}"
                  f"{cpu['mean']:>10.2f}{cpu['median']:>9.2f}{cpu['cv']:>7.3f}"
                  f"{mem.get('mean', 0):>11.0f}{mem.get('median', 0):>10.0f}")

        # The claim under test: in a single run both tools read the same kernel
        # counter (sum_exec_runtime), so probe overhead sits inside both
        # readings and cancels. Agreement here is the artefact, not the result.
        if len(run.demands) > 1:
            medians = {
                source: summarize(values.get("cpu", [])).get("median")
                for source, values in run.demands.items()
            }
            medians = {k: v for k, v in medians.items() if v}
            if len(medians) > 1:
                lo, hi = min(medians.values()), max(medians.values())
                print(f"{'':<30}{'-> ratio':<17}{hi / lo:>7.2f}x"
                      f"   ({' vs '.join(f'{v:.2f}' for v in medians.values())} ms median)")


def print_ladder(runs: list[Run], baseline: str) -> None:
    """Mean response time of each variant relative to the baseline."""
    by_variant: dict[str, list[float]] = {}
    for run in runs:
        if run.elapsed:
            by_variant.setdefault(run.variant, []).extend(run.elapsed)
    if baseline not in by_variant:
        return

    base = st.mean(by_variant[baseline])
    print(f"\n=== overhead ladder vs '{baseline}' (pooled runs) ===")
    for variant, values in sorted(by_variant.items(), key=lambda kv: st.mean(kv[1])):
        mean = st.mean(values)
        print(f"  {variant:<10} {mean:>9.2f} ms   {mean / base:>6.2f}x   {mean - base:>+9.2f} ms")


def print_paired(runs: list[Run], baseline: str) -> None:
    """Wilcoxon signed-rank on per-repetition differences against the baseline.

    Pairs within a repetition: each variant run is compared with the baseline
    run from the same block, so every difference uses its own baseline and the
    differences stay independent across repetitions.
    """
    blocks = _blocks_by_repetition(runs, lambda r: bool(r.elapsed))

    usable = [rep for rep, variants in blocks.items() if baseline in variants]
    if not usable:
        print(f"\n(no complete blocks containing '{baseline}'; paired stats skipped)")
        return

    variants = sorted({v for rep in usable for v in blocks[rep] if v != baseline})
    print(f"\n=== paired vs '{baseline}', per-repetition medians, n={len(usable)} block(s) ===")
    if len(usable) < 6:
        print("    NOTE: a two-sided exact test cannot reach p<0.05 below 6 pairs")
    # Both are reported: d_z is mean/sd so it pairs with the mean, while the
    # Wilcoxon is a rank test and the median is the estimate that matches it.
    # A single column labelled "median delta" that carried the mean is exactly
    # the kind of error that survives into a table in a paper.
    print(f"{'variant':<12}{'pairs':>6}{'nonzero':>8}{'mean delta':>12}{'median delta':>14}"
          f"{'d_z':>8}{'rank-r':>9}{'p':>10}")

    for variant in variants:
        differences = []
        for rep in sorted(usable):
            if variant not in blocks[rep]:
                continue
            treated = st.median(blocks[rep][variant].elapsed)
            control = st.median(blocks[rep][baseline].elapsed)
            differences.append(treated - control)
        if not differences:
            continue

        dz = cohens_dz(differences)
        rb = rank_biserial(differences)
        test = wilcoxon_signed_rank(differences)
        nonzero = sum(1 for d in differences if d != 0)
        print(f"{variant:<12}{len(differences):>6}{nonzero:>8}"
              f"{st.mean(differences):>+12.2f}{st.median(differences):>+14.2f}"
              f"{(f'{dz:.2f}' if dz is not None else '-'):>8}"
              f"{(f'{rb:+.2f}' if rb is not None else '-'):>9}"
              f"{(f'{test[1]:.4f}' if test else '-'):>10}")


def _blocks_by_repetition(runs: list[Run], keep) -> dict[int, dict[str, Run]]:
    """Index runs by repetition and variant, warning on duplicates.

    A campaign writes exactly one run per (repetition, variant). A duplicate
    means a run was repeated by hand, or an old directory was left in the output
    folder -- and keeping the last silently would make every pairing depend on
    directory sort order rather than on the design.
    """
    blocks: dict[int, dict[str, Run]] = {}
    for run in runs:
        if run.repetition is None or not keep(run):
            continue
        slot = blocks.setdefault(run.repetition, {})
        if run.variant in slot:
            previous = slot[run.variant].path
            print(f"    WARNING: repetition {run.repetition} has more than one "
                  f"'{run.variant}' run; ignoring {previous.name if previous else '?'}")
        slot[run.variant] = run
    return blocks


def _demand_median(run: Run, source: str, metric: str) -> float | None:
    """Per-run median for one source and one metric, or None when absent."""
    values = run.demands.get(source, {}).get(metric, [])
    return st.median(values) if values else None


def _sole_source_median(run: Run, metric: str) -> float | None:
    """Per-run median for a run carrying exactly one demand source.

    A `both` run carries two, and which one the caller meant would be a guess.
    Returning None keeps it out of the between-run comparison, which is
    specifically about runs where each tool observed the system on its own.
    """
    if len(run.demands) != 1:
        return None
    return _demand_median(run, next(iter(run.demands)), metric)


def _fmt(value: float) -> str:
    """Two decimals for milliseconds, none for byte counts."""
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.2f}"


def _print_demand_table(title: str, left: str, right: str, rows: list[tuple], note: str) -> None:
    """One paired-statistics table over per-repetition demand medians."""
    print(f"\n=== {title} ===")
    if not rows:
        print("    (no pairs)")
        return

    print(f"{'metric':<16}{'pairs':>6}{left[:9]:>11}{right[:9]:>11}"
          f"{'mean delta':>12}{'ratio':>8}{'d_z':>8}{'rank-r':>9}{'p':>10}")
    for label, left_values, right_values, differences, ratios in rows:
        dz = cohens_dz(differences)
        rb = rank_biserial(differences)
        test = wilcoxon_signed_rank(differences)
        print(f"{label:<16}{len(differences):>6}"
              f"{_fmt(st.mean(left_values)):>11}{_fmt(st.mean(right_values)):>11}"
              f"{('+' if st.mean(differences) >= 0 else '') + _fmt(st.mean(differences)):>12}"
              f"{(f'{st.median(ratios):.2f}x' if ratios else '-'):>8}"
              f"{(f'{dz:.2f}' if dz is not None else '-'):>8}"
              f"{(f'{rb:+.2f}' if rb is not None else '-'):>9}"
              f"{(f'{test[1]:.4f}' if test else '-'):>10}")
    if len(rows[0][3]) < 6:
        print("    NOTE: a two-sided exact test cannot reach p<0.05 below 6 pairs")
    print(f"    {note}")


def print_demand_paired(runs: list[Run], pair: tuple[str, str]) -> None:
    """Paired statistics on per-transaction CPU and memory demand.

    Two tables, answering two questions that are easy to confuse.

    *Measured separately* pairs a run of one variant with a run of the other
    from the same repetition. Each tool observed an otherwise-unperturbed
    system, so the difference between them contains whatever the other tool's
    instrumentation would have cost.

    *Measured together* differences the two sources inside a single ``both``
    run: one JVM, the same transactions, both agents attached. Probe overhead is
    then common to both readings, because both tools ultimately read the same
    kernel field -- the jAgent computes ``se.sum_exec_runtime`` plus the current
    slice in BPF, and OTJAE's ``io.retit.*cputime`` comes from
    ``ThreadMXBean.getCurrentThreadCpuTime()``, which resolves to the same field.

    The contrast between the two tables is the result. Agreement in the second
    is not evidence that either tool is accurate; it is what reading one counter
    guarantees. Reporting only that table would reproduce the original claim and
    its error.

    Per-repetition observations are run medians, matching print_paired(), so
    each repetition contributes one number and the differences stay independent.
    """
    left, right = pair

    blocks = _blocks_by_repetition(runs, lambda r: bool(r.demands))

    separate_rows = []
    for metric, unit in (("cpu", "ms"), ("memory", "bytes")):
        left_values, right_values, differences, ratios = [], [], [], []
        for rep in sorted(blocks):
            a, b = blocks[rep].get(left), blocks[rep].get(right)
            if not a or not b:
                continue
            a_med = _sole_source_median(a, metric)
            b_med = _sole_source_median(b, metric)
            if a_med is None or b_med is None:
                continue
            left_values.append(a_med)
            right_values.append(b_med)
            differences.append(a_med - b_med)
            if b_med:
                ratios.append(a_med / b_med)
        if differences:
            separate_rows.append((f"{metric} ({unit})", left_values, right_values,
                                  differences, ratios))

    _print_demand_table(
        f"per-transaction demand, measured SEPARATELY ({left} vs {right}, paired by repetition)",
        left, right, separate_rows,
        "Each tool observed an unperturbed system; the gap includes the probe\n"
        "    overhead the other tool would have added.")

    together_rows = []
    for metric, unit in (("cpu", "ms"), ("memory", "bytes")):
        left_values, right_values, differences, ratios = [], [], [], []
        for run in sorted((r for r in runs if r.repetition is not None),
                          key=lambda r: r.repetition):
            if SOURCE_JAGENT not in run.demands or SOURCE_OTJAE not in run.demands:
                continue
            a_med = _demand_median(run, SOURCE_JAGENT, metric)
            b_med = _demand_median(run, SOURCE_OTJAE, metric)
            if a_med is None or b_med is None:
                continue
            left_values.append(a_med)
            right_values.append(b_med)
            differences.append(a_med - b_med)
            if b_med:
                ratios.append(a_med / b_med)
        if differences:
            together_rows.append((f"{metric} ({unit})", left_values, right_values,
                                  differences, ratios))

    _print_demand_table(
        "per-transaction demand, measured TOGETHER (both agents on one JVM, per run)",
        "jAgent", "OTJAE", together_rows,
        "Both tools read the same kernel counter here, so probe overhead sits\n"
        "    inside both readings and cancels. Agreement is structural, not accuracy.")

    cpu_separate = next((r for r in separate_rows if r[0].startswith("cpu")), None)
    cpu_together = next((r for r in together_rows if r[0].startswith("cpu")), None)
    if cpu_separate and cpu_together and cpu_separate[4] and cpu_together[4]:
        print(f"\n    CPU agreement: {st.median(cpu_separate[4]):.2f}x apart when measured "
              f"separately, {st.median(cpu_together[4]):.2f}x when measured together.")


def main() -> None:
    """Load every run and print the report."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT),
                        help="directory holding the run folders")
    parser.add_argument("--full", action="store_true",
                        help="use the whole run instead of the steady-state window")
    parser.add_argument("--baseline", default="none",
                        help="variant to compare against (default: none)")
    parser.add_argument("--filter", default="",
                        help="only runs whose directory name contains this")
    parser.add_argument("--demand-pair", default="jagent,otjae",
                        help="two variants to compare on demand (default: jagent,otjae)")
    args = parser.parse_args()

    demand_pair = tuple(part.strip() for part in args.demand_pair.split(","))
    if len(demand_pair) != 2:
        raise SystemExit("--demand-pair needs exactly two comma-separated variants")

    root = Path(args.output_dir)
    if not root.is_dir():
        raise SystemExit(f"output directory not found: {root}")

    runs = []
    for entry in sorted(os.listdir(root)):
        directory = root / entry
        if not directory.is_dir() or args.filter not in entry:
            continue
        run = load_run(directory, steady_only=not args.full)
        if run:
            runs.append(run)

    if not runs:
        raise SystemExit("no runs found")

    print_runs(runs, steady_only=not args.full)
    print_ladder(runs, args.baseline)
    print_paired(runs, args.baseline)
    print_demand_paired(runs, demand_pair)


if __name__ == "__main__":
    main()
