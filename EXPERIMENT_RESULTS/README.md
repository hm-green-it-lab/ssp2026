# Measurement data and analysis

The raw output of the campaign behind the paper, and the script that turns it
back into the paper's tables. Nothing here needs a Linux host, an eBPF-capable
kernel or the agent itself — the data is already measured; this directory is
about reading it.

- **`rep01.zip` … `rep12.zip`** — one archive per repetition, six run
  directories each: **72 runs, all successful.**
- **`analyze.py`** — steady-state analysis. Python 3.9+, standard library only.
- **`campainresult.txt`** — the campaign script's closing summary
  (72/72 succeeded, 2026-08-18 22:21 → 2026-08-19 07:39 CEST).

## Quick start

```bash
mkdir -p runs && for z in rep*.zip; do unzip -q -o "$z" -d runs; done
python analyze.py --output-dir runs
```

On Windows:

```powershell
New-Item -ItemType Directory -Force runs
Get-ChildItem rep*.zip | ForEach-Object { Expand-Archive $_ -DestinationPath runs -Force }
python analyze.py --output-dir runs
```

All twelve archives unpack into **one flat directory**: `analyze.py` looks for
run folders one level below `--output-dir`, and the paired statistics need every
repetition present at once. Expect ~90 s of runtime — it parses 72 JTL files and
48 OTLP JSON streams — and about 190 MB unpacked.

## Reproducing the paper's tables

### Table 1 — response-time overhead (`tab:overhead`)

The paper's **Median** column is the median of the twelve per-run medians, which
are the `median` column of the report's first block (`response time, steady state
only`). The **Δ**, **rel.** and ***p*** columns come from the block
`paired vs 'none', per-repetition medians`.

| Configuration | Paper median | Paper Δ | `analyze.py` `median delta` | `analyze.py` `p` |
|---|---|---|---|---|
| `otjae` | 27.0 ms | −0.5 | `-0.50` | `0.0312` |
| `none` | 28.0 ms | — | — | — |
| `usdt` | 30.5 ms | +3.0 | `+3.00` | `0.0005` |
| `noop` | 159.0 ms | +131.5 | `+131.50` | `0.0005` |
| `jagent` | 170.5 ms | +142.0 | `+142.00` | `0.0005` |
| `both` | 194.0 ms | +166.5 | `+166.50` | `0.0005` |

`0.0005` is the exact floor of a two-sided Wilcoxon signed-rank test at n = 12,
not a rounded value. `otjae` is the one comparison that does not clear the
Bonferroni-corrected threshold of 0.0125: six of its twelve differences are
exactly zero, so the exact test runs on six pairs.

### Table 2 — per-transaction demand (`tab:demand`)

Directly from the report's last two blocks; the paper's **means** are the columns
`jagent` / `otjae` (separately) and `jAgent` / `OTJAE` (together).

| | eBPF jAgent | OTJAE | Paper ratio |
|---|---|---|---|
| *measured separately* | | | |
| CPU (ms) | 118.99 | 15.99 | 7.44 |
| Memory (B) | 14,329 | 16,059 | 0.89 |
| *measured together* | | | |
| CPU (ms) | 131.56 | 127.21 | 1.03 |
| Memory (B) | 18,621 | 16,199 | 1.15 |

One number is reported differently in the two places: the paper's **Ratio** is
the quotient of the two means it prints beside it (118.99 / 15.99 = 7.44),
whereas the report's `ratio` column is the *median of the per-repetition
ratios* (`7.40x`). Both describe the same data; neither is derived from the
other.

### Useful flags

| Flag | Effect |
|---|---|
| `--full` | Analyse the whole run instead of the steady-state window. Spread widens noticeably — the ramps contribute samples taken at a different offered load. |
| `--filter rep03` | Restrict to run directories whose name contains this. Handy for looking at a single repetition. |
| `--baseline otjae` | Compare the ladder against a different configuration. |
| `--demand-pair jagent,otjae` | The two configurations compared on demand (the default). |

## What is in one run directory

Directories are named `{timestamp}_rep{NN}_{configuration}`, files
`{tool}_{configuration}_{timestamp}_{iteration}_{total}.{ext}`.

| File | Written by | Contents |
|---|---|---|
| `manifest_*.json` | controller | Fully resolved configuration, hostnames, timings, status, and the campaign coordinates `campaign_repetition` / `campaign_position`. **Start here** — it records exactly what the run was. |
| `jmeter_*.jtl` | JMeter | Per-request results (CSV). The source of every response-time number. |
| `jmeter_*.stdout.log` | JMeter | Console output incl. the summariser. Carries the `Starting standalone test @ … (epoch_ms)` line the steady-state window is anchored on. |
| `jmeter_*.log` | JMeter | JMeter's own log. |
| `sut_*.log` | SUT | Application stdout/stderr. Records the JVM and agent versions actually loaded. |
| `collector_telemetry_*.jsonl` | controller | **The demand data of record** — lossless OTLP JSON from the collector's `file` exporter: every metric datapoint and span both tools emitted. |
| `collector_*.log` | controller | Collector stdout, incl. its version. |
| `collector_config_*.yaml` | controller | The exact collector configuration used for that run. |
| `jagent_*.log` | SUT | jAgent stdout: libbpf attach diagnostics and OTLP export errors. |

Which files a run has depends on what it ran, so their absence is informative
rather than a gap:

| Configuration | JMeter + SUT | Collector | jAgent log |
|---|---|---|---|
| `none`, `usdt` | ✓ | — (nothing exports) | — |
| `otjae` | ✓ | ✓ | — |
| `noop`, `jagent`, `both` | ✓ | ✓ | ✓ |

No `jagent_trace_*.txt` or `http_logger_*.csv` files exist in this campaign;
both outputs were deliberately disabled. See the root
[README § Deviations](../README.md#deviations-and-caveats) for why, and why
nothing is lost by it.

## How the numbers are extracted

### The steady-state window

The test plan ramps the arrival rate 0 → 50/s over 60 s, holds it for 120 s,
then ramps back down. Samples from the ramps were taken at a *different offered
load* than the ones in between, so including them inflates the spread with
variation that is a property of the schedule rather than of the
instrumentation. `analyze.py` therefore keeps only the middle 120 s — about 67 %
of roughly 9,000 samples per run, so ~6,000 remain.

The window is anchored on the schedule start JMeter logs, not on the first
sample: at the beginning of the ramp the rate is still near zero, so the first
arrival can be seconds late and would shift the window.

### Per-transaction demand

The two tools carry it in different places, so `analyze.py` reads each in its own
way:

- **eBPF jAgent** — the OTLP metrics `ebpf.jagent.resource.demand.cpu.ms` and
  `ebpf.jagent.resource.demand.memory.bytes`. These are **cumulative monotonic**
  sums with one datapoint per transaction; consecutive datapoints are
  differenced to recover the per-transaction values.
- **OTJAE** — span attributes on the `veryComplexBusinessFunction` span:
  `io.retit.startcputime` / `io.retit.endcputime` and the matching
  heap-allocation pair. This is why the traces pipeline is enabled and not just
  metrics: OTJAE's *metrics* are cumulative totals and cannot yield a
  per-transaction distribution.

The `both` runs carry **two** sources in a single run, which is the entire point
of that configuration — comparing the two tools inside one process, where the
probe overhead sits inside both readings and cancels.

Because the counters are cumulative, a single final reading would also contain
ramp-up and ramp-down traffic. Differencing a series is what makes the
steady-state restriction possible at all.

## Regenerating the data

The archives are the output of `EXPERIMENT_AUTOMATION/run.sh` (or `run.bat`)
with `REPETITIONS=12`, `LOAD_LEVELS=(50)` and `ORDER_SEED=42`, zipped one
repetition per file. To repeat the campaign, see
[`../EXPERIMENT_AUTOMATION/README.md`](../EXPERIMENT_AUTOMATION/README.md); it
takes roughly nine hours plus a prepared Linux SUT. `analyze.py` defaults its
`--output-dir` to `../EXPERIMENT_AUTOMATION/output`, so a fresh campaign can be
analysed without unpacking anything:

```bash
python analyze.py
```
