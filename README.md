# Replication package — *eBPF jAgent*

Everything behind the measurements in

> **eBPF jAgent: Transaction-level Resource Demand Tracing for Java
> Applications Using eBPF and OpenTelemetry** (SSP 2026)

— the automation that produced them, the raw data of all 72 runs, and the
analysis script that turns that data back into the paper's two tables.

## The three repositories

| Repository | Contains | Link |
|---|---|---|
| **`ssp2026`** (this one) | Experiment automation, raw measurement data, analysis | <https://github.com/hm-green-it-lab/ssp2026> |
| **`ebpf-jagent`** | The tool under evaluation: eBPF programs, userspace controller, OTLP exporter. The measured build is release **`v0.0.7-alpha`** | <https://github.com/hm-green-it-lab/ebpf-jagent> |
| Paper sources | LaTeX manuscript | `gitlab.lrz.de/green-it/papers/2025/tracing-transaction-level-resource-consumption-with-ebpf-and-opentelemetry` |

Two further projects are used unmodified and are *not* vendored here; the
automation downloads pinned versions of both at run time:

- **OTJAE** — [RETIT OpenTelemetry Java agent extension](https://github.com/RETIT/opentelemetry-javaagent-extension)
  `v0.1.1-beta`, the in-process comparison baseline. Its example
  `spring-rest-service` is also the workload.
- **[http-logger](https://github.com/hm-green-it-lab/http-logger)** — Prometheus
  sampler carried over from the earlier ASE experiment. Disabled in this
  campaign (see [Deviations](#deviations-and-caveats)).

## Where to start

| If you want to … | Go to | Effort |
|---|---|---|
| **Check the paper's numbers against the raw data** | [`EXPERIMENT_RESULTS/`](EXPERIMENT_RESULTS/README.md) | 2 commands, ~3 min |
| Understand how one measurement is produced | [`EXPERIMENT_AUTOMATION/`](EXPERIMENT_AUTOMATION/README.md) | reading |
| Re-run the campaign on your own hardware | [`EXPERIMENT_AUTOMATION/` § Setup](EXPERIMENT_AUTOMATION/README.md#setup) | ~9 h + a Linux SUT |

Nothing outside `EXPERIMENT_RESULTS/` is needed to verify a published number.
The automation is there so the measurement can be repeated, not so it has to be.

## Repository map

```
ssp2026/
├── EXPERIMENT_AUTOMATION/     how a measurement is taken
│   ├── main.py                one run: start SUT, warm up, attach, load, collect
│   ├── orchestrator/          the run loop and preflight checks
│   ├── helper/                SSH, SUT, jAgent, JMeter, collector, tunnel, ...
│   ├── setup/sut_setup.py     one-time preparation of the Linux SUT
│   ├── configuration/         base.yml + one file per measured configuration
│   ├── jmeter/                the GET-only test plan actually used
│   ├── noop_agent/            empty-handler control agent (the `noop` variant)
│   ├── campaign_order.py      Latin-square variant order within a repetition
│   └── run.sh / run.bat       the full 72-run campaign
│
└── EXPERIMENT_RESULTS/        what was measured
    ├── rep01.zip … rep12.zip  12 repetitions × 6 configurations = 72 runs
    ├── analyze.py             raw data  ->  the paper's tables
    └── campainresult.txt      campaign console summary (72/72 succeeded)
```

## Verifying the paper's numbers

```bash
cd EXPERIMENT_RESULTS
mkdir -p runs && for z in rep*.zip; do unzip -q -o "$z" -d runs; done
python analyze.py --output-dir runs
```

Runtime is about 90 seconds — the script parses 72 JTL files and 48 OTLP JSON
streams. Only the Python standard library is required.

The report ends with the three blocks that back the paper:

| Report block | Backs |
|---|---|
| `paired vs 'none', per-repetition medians` | **Table 1** (`tab:overhead`) — response-time overhead |
| `per-transaction demand, measured SEPARATELY` | **Table 2**, upper half — CPU/memory with each tool alone |
| `per-transaction demand, measured TOGETHER` | **Table 2**, lower half — both agents on one JVM |

For the correspondence value by value, see
[`EXPERIMENT_RESULTS/README.md` § Reproducing the paper's tables](EXPERIMENT_RESULTS/README.md#reproducing-the-papers-tables).

## The six measured configurations

Five of them form an **overhead ladder** in which each step adds exactly one
thing over the one before it, so each gap attributes to a single cause. The
sixth is a control that is deliberately *not* on the ladder.

| Config file | JVM on the SUT | Attached | Isolates | Paper |
|---|---|---|---|---|
| `spring_remote_none.yml` | plain | — | the baseline | `none` |
| `spring_remote_usdt.yml` | `-XX:+DTrace*Probes` | — | cost of the probe *flags* (sites stay nop-patched) | `usdt` |
| `spring_remote_noop.yml` | `-XX:+DTrace*Probes` | empty eBPF handlers | cost of the probes *firing* | `noop` |
| `spring_remote_jagent.yml` | `-XX:+DTrace*Probes` | eBPF jAgent `v0.0.7-alpha` | cost of the agent's *work* | `jagent` |
| `spring_remote_otjae.yml` | plain | OTJAE javaagent | the in-process baseline | `otjae` |
| `spring_remote_both.yml` | `-XX:+DTrace*Probes` | jAgent **and** OTJAE | whether the two tools' agreement is structural | `both` |

`both` is a **negative control, not a deployable configuration**: the two agents
perturb each other, so its response time belongs in no ladder. It exists to test
whether the agreement between the two tools' CPU readings is real or an artefact
of both reading the same kernel field on the same thread — a falsifiable test
rather than an assertion. The rationale is written out in full at the top of
[`configuration/spring_remote_both.yml`](EXPERIMENT_AUTOMATION/configuration/spring_remote_both.yml).

## Experimental design

- **12 repetitions × 6 configurations = 72 runs**, all successful.
- Each run lasts **4 minutes**: 60 s ramp-up, 120 s steady state, 60 s ramp-down.
  Only the steady-state window is evaluated; `analyze.py` anchors it on the exact
  schedule start JMeter logs, not on the first sample.
- **Open workload of 50 transactions/s** against the GET endpoint only, so both
  tools observe the same population of calls.
- The **repetition loop is outermost**, so one repetition runs all six
  configurations before the next begins — thermal or background drift is spread
  across configurations instead of loading onto whichever ran last.
- Within a repetition the order is drawn from a **Latin square**
  (`campaign_order.py`, `ORDER_SEED=42`), so every configuration occupies every
  position exactly twice over the 12 repetitions and run position is not
  confounded with configuration.
- Comparisons are **paired within a repetition** and tested with a two-sided
  exact Wilcoxon signed-rank test over the 12 per-run medians. `p = 0.0005` is
  the exact floor at n = 12; Bonferroni over the four comparisons against `none`
  gives a threshold of 0.0125.
- The load generator does **not** run on the SUT. The measured quantity is
  request latency, and a co-located generator would compete for the CPUs of the
  traced JVM — hitting the traced configurations hardest, which is exactly the
  number under scrutiny.

Every run records its `campaign_repetition` and `campaign_position` in its
manifest, so the design is recoverable from the artifacts alone and a residual
order effect can be *checked* rather than assumed away.

## Environment of record

The values below are what the shipped data was measured on; each is recoverable
from the run manifests and logs in `EXPERIMENT_RESULTS/`.

| | |
|---|---|
| **SUT hardware** | Bare metal, 2 × Intel Xeon E5-2630 v4 (10 cores each, 2.20 GHz base / 3.10 GHz turbo), hyper-threading disabled, 256 GB RAM |
| **SUT OS** | Proxmox VE (Debian-based) used directly as the OS, all VMs stopped, so no virtualization indirection distorts the measurement. BTF-enabled kernel |
| **JVM** | OpenJDK 25.0.4, `-Xms1g -Xmx1g`, built with `--enable-dtrace` (the HotSpot USDT probes must be present in `libjvm.so`) |
| **Workload** | `spring-rest-service` from the OTJAE repository (tag `v0.1.1-beta`), Spring Boot 4.0.6; measured transaction `TestService.veryComplexBusinessFunction` |
| **eBPF jAgent** | `v0.0.7-alpha`, published `linux-x86_64` release, `--probes cpu,memory`, filter `veryComplexBusinessFunction`, `--min-duration-us 1000` |
| **OTJAE** | RETIT extension `v0.1.1-beta` on OpenTelemetry Java agent **2.30.0** |
| **Collector** | `otel/opentelemetry-collector-contrib` **0.158.0**, on the controller |
| **Load driver** | Apache JMeter 5.6.3 on JDK 21, on the controller |
| **Campaign** | 2026-08-18 22:21 → 2026-08-19 07:39 CEST, 72/72 runs succeeded |

The collector runs on the controller rather than beside the application, so it
does not compete for the CPU cycles being measured. Both tools export OTLP to
it, which is what keeps the comparison fair: one export path instead of one tool
writing logs and the other posting metrics.

## Deviations and caveats

These are the points at which a re-run may legitimately differ from the shipped
data. None affects a published number, but a reviewer reproducing the campaign
should know about them up front.

- **Every downloaded component is pinned to the version that was measured.**
  `configuration/base.yml` names the eBPF jAgent `v0.0.7-alpha`, the OpenTelemetry
  Java agent `v2.30.0`, the RETIT extension `v0.1.1-beta` and the collector image
  `0.158.0` — no `latest` URLs, so a rerun fetches what the paper measured rather
  than whatever is current on the day. The JDK, JMeter and the operating system
  come from the host and are *not* pinned by the configuration; see
  [Environment of record](#environment-of-record) for the versions used.
- **The jAgent's per-transaction trace file is off** (`jagent.write_trace:
  false`). Formatting a line per transaction and writing it costs I/O on the hot
  path, which OTJAE does not pay, so leaving it on would have inflated the
  jAgent's measured overhead. The same per-transaction values are recoverable by
  differencing the cumulative OTLP counters, which is what `analyze.py` does —
  so no `jagent_trace_*.txt` files are present, and none are needed.
- **The http-logger is off** (`http_logger.enabled: false`). Its Prometheus
  sampling exists for comparability with the earlier ASE experiment; the
  collector's `file` exporter records every point losslessly, which is strictly
  more information. So no `http_logger_*.csv` files are present either.
- **Network and storage probes are not collected.** Restricting the jAgent to
  `cpu,memory` matches what OTJAE collects by default, and it matters beyond
  reporting: the network and storage probes hook kernel-wide functions, so
  leaving them attached would make every socket operation and `write()` on the
  whole machine trap — a cost OTJAE never pays, which would surface as
  instrumentation overhead that is really an artefact of collecting dimensions
  the comparison does not use.
- **Clock drift between controller and SUT is not measured.** It does not affect
  any published number, since latency comes from the JTL alone and the demands
  from the SUT alone, but it would matter if you correlate the two.

## Licensing

The eBPF jAgent's license is in its own repository. This package contains
measurement automation and data; the third-party components it downloads
(OpenTelemetry Java agent, OTJAE, JMeter test plan, collector image) remain
under their respective licenses.
