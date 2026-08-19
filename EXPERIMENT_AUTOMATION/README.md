# Experiment Automation — eBPF jAgent vs. OTJAE

Reruns the measurements behind the SSP paper.

> **Only reading the paper's numbers?** You do not need any of this. The
> measured data ships in [`../EXPERIMENT_RESULTS/`](../EXPERIMENT_RESULTS/README.md)
> and needs two commands and no Linux host. This directory is for repeating the
> measurement; see the [root README](../README.md) for how the pieces fit
> together.

| Role | Runs |
|---|---|
| **Controller** | this automation — credentials, artifact staging, result collection, **JMeter, the OpenTelemetry Collector and (optionally) the http-logger** |
| **SUT** (`${SUT_HOST}`) | `spring-rest-service` + the instrumentation under test |
| **Load driver** (`${JMETER_HOST}`) | JMeter, *only* with `jmeter.location: remote` |

The SUT is driven over SSH. Unlike the ASE template the application runs as a
plain `java -jar` process rather than in a container.

What matters methodologically is that the load generator is not on the SUT: the
measured quantity is request latency, and a co-located generator would compete
for the same CPUs as the traced JVM — hitting the traced (`jagent`/`noop`) configurations
hardest, which is exactly the number under scrutiny. Running JMeter on the
controller satisfies this, since the controller is a separate machine. Use
`jmeter.location: remote` if you want a third, dedicated driver.

Each run is **4 minutes**: 1 minute ramp-up, 2 minutes steady state, 1 minute
ramp-down.

## Experiment variants

Four of the six form an **overhead ladder**, each step adding exactly one thing
over the one before it, so every gap attributes to a single cause. `otjae` is
the comparison baseline and `both` a control that is deliberately not a rung.

| Config | JVM on the SUT | Attached | Isolates | Paper |
|---|---|---|---|---|
| `spring_remote_none.yml` | plain | — | the baseline | `none` |
| `spring_remote_usdt.yml` | `-XX:+DTrace*Probes` | — | cost of the probe *flags* | `usdt` |
| `spring_remote_noop.yml` | `-XX:+DTrace*Probes` | empty eBPF handlers | cost of the probes *firing* | `noop` |
| `spring_remote_jagent.yml` | `-XX:+DTrace*Probes` | eBPF jAgent | cost of the agent's *work* | `jagent` |
| `spring_remote_otjae.yml` | plain | OTJAE | the in-process baseline | `otjae` |
| `spring_remote_both.yml` | `-XX:+DTrace*Probes` | jAgent **and** OTJAE | whether the tools' agreement is structural | `both` |

`spring_remote_usdt` separates the JVM-side cost of the probe flags from the
cost of tracing: the probes are emitted but nothing is attached, so their sites
stay nop-patched. `spring_remote_noop` then attaches eBPF programs whose
handlers do nothing, which turns those nop-patched sites into traps — the gap
from `usdt` to `noop` is what the probes cost merely by firing, and the gap from
`noop` to `jagent` is the agent's own bookkeeping. The `noop` agent is built
from [`noop_agent/`](noop_agent/) via `jagent.provision: noop`.

`spring_remote_both` is a **negative control, not a deployable configuration**:
the two agents perturb each other, so its response times belong in no ladder. It
runs inside the same campaign anyway, because it is compared against the
`jagent` and `otjae` runs and a separate session would confound drift with the
effect it exists to demonstrate. Its full rationale, including the falsifiable
prediction it tests, is at the top of
[`configuration/spring_remote_both.yml`](configuration/spring_remote_both.yml).

## Prerequisites

**Controller** — Python 3.9+, `pip install -r requirements.txt`. Works on
Windows; only `--dry-run` is limited to nothing.

**JMeter host** — Apache JMeter 5.5+ (the plan uses the built-in
`OpenModelThreadGroup`; no plugins), a JRE, and network reach to
`${SUT_HOST}:8081`. By default this is the controller, so point `JMETER_BIN` at
the local install (`jmeter.bat` on Windows). With `jmeter.location: remote` it is
a separate host and also needs SSH access.

**SUT** — Ubuntu (or another Linux) with a BTF-enabled kernel, the JDK matching
`JVM_LIB_PATH`, SSH access, and passwordless `sudo` for the jAgent variant.
The prebuilt agent is dynamically linked, so it needs:

```bash
sudo apt-get install -y libbpf1 libelf1 zlib1g libcurl4
```

The application must be built there, since it has no published release (the
RETIT releases carry only the extension JAR). `setup/sut_setup.py` does that for
you — see [One-time SUT preparation](#one-time-sut-preparation).

Everything else — the eBPF jAgent release, the OpenTelemetry Java agent, the
RETIT extension JAR and the JMeter test plan — is fetched by the controller into
`artifacts/` and uploaded to the host that needs it, so all three machines stay
on the same versions.

Each of those is pinned to a version in `configuration/base.yml`, and to the
version the paper's campaign measured:

| Component | Pinned to | Key |
|---|---|---|
| eBPF jAgent | `v0.0.7-alpha` | `jagent.release_url` |
| OpenTelemetry Java agent | `v2.30.0` | `otjae.otel_agent_url` |
| RETIT extension (OTJAE) | `v0.1.1-beta` | `otjae.extension_url` |
| OpenTelemetry Collector (contrib) | `0.158.0` | `collector.image` |
| JMeter plan (stock, fallback only) | `v0.1.1-beta` | `jmeter.test_plan_url` |

Staging one build to every host keeps the machines consistent *within* a
campaign; pinning is what keeps them consistent *between* campaigns. A `latest`
URL resolves to whatever is current on the day, so a rerun months from now would
silently measure a different agent and nothing in the results would show it.
Bump a version by editing the URL — the manifest of every run records what was
actually used.

The JDK, JMeter and the operating system come from the hosts, not from the
configuration, so those stay yours to match; the versions the paper used are in
the [root README](../README.md#environment-of-record).

### How the jAgent reaches the SUT

`jagent.provision` selects one of four modes:

| Mode | What happens | When to use it |
|---|---|---|
| `release` (default) | Download the published `linux-x86_64` tarball, ship it to the SUT, unpack, locate the binary | Ubuntu targets; reproducible against a fixed tag |
| `noop` | Ship `noop_agent/` and `make` it on the SUT — the same probes with empty handlers | The `noop` control variant |
| `source` | Ship this controller's working tree (`jagent.source_dir`) and `make` it on the SUT | Measuring uncommitted agent changes |
| `preinstalled` | Assume a binary at `jagent.binary` | Manually provisioned hosts |

`noop` and `source` compile on the SUT and therefore need its toolchain;
`release` and `preinstalled` do not.

The agent cannot be cross-built and copied as a binary from an arbitrary host:
its userspace part links libbpf/libelf/libcurl, and the BPF object compiles
against the SUT's kernel headers. `release` sidesteps this by shipping a build
made for Ubuntu 24.04 / glibc 2.39 — so in that mode the SUT needs **no**
toolchain. `noop` and `source` mode do, and are checked for `make`, `clang`,
`gcc` and `bpftool` up front. Since the campaign includes the `noop` control,
the full campaign does need that toolchain on the SUT; `setup/sut_setup.py`
installs it.

The release archive unpacks into a version-stamped directory, so the binary is
*discovered* rather than hardcoded and a version bump needs only
`jagent.release_url` changed. After unpacking, `ldd` is checked for unresolved
libraries and the binary is smoke-tested with `-h`, so a library mismatch fails
in seconds rather than at attach time. (The `.bpf.o` in the archive is for
inspection only — the BPF object is embedded in the binary.)

## Setup

```bash
cp .env.template .env            # SSH credentials (SUT always; JMeter only if remote)
cp paths.env.template paths.env  # hosts and paths
$EDITOR .env paths.env
pip install -r requirements.txt
```

### One-time SUT preparation

```bash
python setup/sut_setup.py --check-only   # report what is missing, change nothing
python setup/sut_setup.py                # install, verify, build
```

It checks the distribution and tools, apt-installs the jAgent's runtime
libraries plus `binutils`/`curl`/`git`/`maven`, verifies passwordless sudo, BTF,
the configured JDK **and that its `libjvm.so` carries the HotSpot USDT probes**,
creates `${SUT_BASE_DIR}/work`, then clones the RETIT repository at a pinned tag
(`--tag`, default `v0.1.1-beta` to match the extension JAR the automation
downloads) and builds `spring-rest-service.jar` into
`${SPRING_REST_SERVICE_JAR}`. Re-running is cheap: the build is skipped when the
JAR exists, `--force-rebuild` redoes it, `--skip-build` leaves it alone.

**No container is built on the SUT.** The application runs as a plain
`java -jar` process, and the only container in this setup is the collector, which
runs on the controller. The eBPF jAgent is not installed by setup either — the
automation ships the published release on every run, so the measured build is
always the configured one.

If the Maven build trips over a JDK-version error, build with an older JDK via
`JAVA_HOME`: only the *runtime* JDK needs the DTrace probes, not the build one.

`JVM_LIB_PATH` must be the `libjvm.so` belonging to `JAVA_BIN`. If they
disagree the USDT probes cannot be resolved and the agent attaches to nothing.

That library must also *carry* the HotSpot probes, which exist only if the JDK
was configured with `--enable-dtrace`. Preflight verifies this, since a JDK
without them fails silently — the service runs, `-XX:+DTraceMethodProbes` is
accepted, the agent attaches, and the trace stays empty. To check by hand:

```bash
readelf -n /path/to/libjvm.so | grep -oE 'Name: [A-Za-z_]+' | sort -u
# must include method__entry, method__return and object__alloc
```

## Running

```bash
python -m main --config configuration/spring_remote_jagent.yml --dry-run
python -m main --config configuration/spring_remote_none.yml
python -m main --config configuration/spring_remote_usdt.yml
python -m main --config configuration/spring_remote_noop.yml
python -m main --config configuration/spring_remote_jagent.yml
python -m main --config configuration/spring_remote_otjae.yml
python -m main --config configuration/spring_remote_both.yml
```

Flags: `--iterations N`, `--total-rate N`, `--skip-downloads`, `--dry-run`.

### Running a whole campaign

`run.bat` (Windows) and `run.sh` (Linux) run every variant back-to-back:

```bat
run.bat
```
```bash
./run.sh
```

Edit the variable block at the top to set `CONFIGS`, `LOAD_LEVELS`,
`REPETITIONS`, `COOLDOWN_SECONDS` and `CONTINUE_ON_ERROR`. Both print a run
count and time estimate up front, validate every configuration with `--dry-run`
before committing hours to the campaign, and finish with a pass/fail summary.
They exit non-zero if any run failed.

The values committed here are the ones the paper's campaign ran with — all six
variants, `LOAD_LEVELS=(50)`, `REPETITIONS=12`, `ITERATIONS_PER_RUN=1`,
`COOLDOWN_SECONDS=120`, `ORDER_SEED=42` — so running the script unedited
repeats it: 72 runs in about nine hours. Its output is what
[`../EXPERIMENT_RESULTS/`](../EXPERIMENT_RESULTS/README.md) ships, one zip per
repetition.

**Ordering matters here, in two ways.**

*Between* repetitions: the repetition loop is outermost, so one repetition runs
every variant before the next begins. Running 3x `none`, then 3x `usdt`, then 3x
`jagent` would load any thermal or background drift onto whichever variant ran
last — and since the whole point is comparing latency *between* variants, that
drift would be indistinguishable from instrumentation overhead.

*Within* a repetition: the variant order is drawn from a **Latin square**
(`campaign_order.py`), so run position is not confounded with variant either. A
fixed sequence would make the baseline always first and coldest, and the last
variant always ~30 minutes into a sustained-load session. Over each group of *n*
repetitions every variant occupies every position exactly once; free shuffling
leaves noticeable imbalance at these sample sizes (`--design random` if you want
it anyway). `ORDER_SEED` makes the whole campaign reproducible; `0` draws fresh
orders.

Each run records its block and position, so the design is recoverable from the
artifacts: the output directory is named
`{timestamp}_rep{NN}_{experiment_type}`, and the manifest carries
`campaign_repetition` and `campaign_position`.

### Pairing the results

`campaign_repetition` is the **blocking factor**. To compare a variant against
the baseline, pair each variant run with the `none` run from the *same*
repetition and test the per-repetition differences — each difference then uses
its own baseline, so the differences are independent across repetitions and an
exact Wilcoxon signed-rank p-value carries its conventional meaning.
`campaign_position` is there so a residual order effect can be checked rather
than assumed away.

`ITERATIONS_PER_RUN=1` keeps the interleaving under the script's control rather
than repeating a variant inside a single `main.py` invocation.

Before the first iteration every host is preflighted — JAR, JVM, free port,
`sudo`, BTF, `libjvm.so` *and its USDT probes*, the agent binary and its shared
libraries, JMeter and the test plan — so a misconfiguration fails in seconds
rather than after a 4-minute run.

## What one iteration does

1. Start the SUT — the agent variant is already in its JVM arguments.
2. Wait until port 8081 accepts connections (probed on the SUT itself, so a
   firewall between the machines cannot cause a false negative).
3. Warm up (100 requests by default, issued on the SUT against loopback —
   the point is settling JIT, which does not depend on the request's origin).
4. Attach the eBPF jAgent — only possible now, since it needs the JVM's PID.
5. Run JMeter for `2 × ramp + steady` seconds — on the controller, or on the
   remote driver.
6. SIGINT the agent so it flushes, then stop the SUT.
7. Collect every artifact and write a manifest. A controller-side JMeter writes
   straight into the output directory, so only the SUT's files need downloading.

The collector starts *before* the SUT (so no export is missed) and stops *after*
it (so the final batches flush), with the http-logger bracketing only the load
phase.

Step 3 deliberately precedes step 4: warming up with the tracer attached would
both distort the overhead measurement and fill the trace with transactions the
paper excludes.

Remote processes are launched as
`setsid bash -c 'echo $$ > pidfile; exec <cmd>'` so they survive the SSH
channel and the recorded PID is the real process. Signals go to the whole
process **group**, which matters because `sudo` forks before running the agent.

## Output

Everything lands on the controller under
`output/{timestamp}_rep{NN}_{experiment_type}/` (the `rep{NN}` part is added by
the campaign scripts), named
`{tool}_{experiment_type}_{timestamp}_{iteration}_{total}.{ext}`:

| File | From | Contents |
|---|---|---|
| `manifest_*.json` | controller | Resolved config, hostnames, timings, status, `campaign_repetition` / `campaign_position`. |
| `sut_*.log` | SUT | Service stdout/stderr, incl. the JVM and agent versions actually loaded. |
| `jmeter_*.jtl` | JMeter host | Per-request results (CSV) — the source for the latency table. |
| `jmeter_*.log`, `*.stdout.log` | JMeter host | JMeter's log and console output incl. the summariser. The stdout log carries the schedule-start timestamp the steady-state window is anchored on. |
| `collector_telemetry_*.jsonl` | controller | **Data of record** — lossless OTLP JSON from the collector's `file` exporter: every metric and span both tools emitted. |
| `collector_*.log`, `collector_config_*.yaml` | controller | Collector stdout and the exact config used. |
| `jagent_*.log` | SUT | jAgent stdout: libbpf attach diagnostics, OTLP export errors. |
| `jagent_trace_*.txt` | SUT | One line per transaction with wall/CPU ns and tx/rx/io/alloc bytes. **Only with `jagent.write_trace: true`**, which is off by default — the formatting and file I/O sit on the hot path and OTJAE pays no equivalent cost, so leaving it on inflates the measured overhead. The same values come out of the cumulative OTLP counters. |
| `http_logger_*.csv` | controller | Sampled Prometheus exposition (`DATA:<url> at <ts>` + body), comparable with the ASE experiment. **Only with `http_logger.enabled: true`**, off by default. |

Which files a run produces depends on what it ran: `none` and `usdt` have
nothing exporting, so they get no collector output, and only the three
jAgent-attached variants get a `jagent_*.log`.

Per-run remote directories are deleted after download
(`experiment.cleanup_remote: false` keeps them).

## Analysing what you measured

[`../EXPERIMENT_RESULTS/analyze.py`](../EXPERIMENT_RESULTS/analyze.py) reads
these directories directly and defaults to `../EXPERIMENT_AUTOMATION/output`, so
after a campaign:

```bash
python ../EXPERIMENT_RESULTS/analyze.py
```

It restricts everything to the steady-state window and prints the response-time
ladder, the paired comparison against the baseline, and the per-transaction
demand tables. See
[`../EXPERIMENT_RESULTS/README.md`](../EXPERIMENT_RESULTS/README.md) for how its
output maps onto the paper's tables.

## How metrics are collected

Both tools export OTLP, so a collector on the controller is the common point
that keeps the comparison fair — OTJAE and the jAgent travel one path instead of
one writing logs and the other posting metrics. The automation rewrites
`jagent.otlp_endpoint` and OTJAE's `otel.exporter.otlp.*` properties to that
endpoint at run time, resolving the controller's address from the route toward
the SUT (override with `collector.controller_host`).

Each pipeline fans out to two exporters, so you do not have to choose:

- **`file`** — the data of record. Lossless OTLP JSON, every point that arrives,
  no scrape quantisation.
- **`prometheus`** — sampled by the http-logger, matching the ASE experiment.

`debug` is enabled at `basic` verbosity as a sanity check only. It is what the
old `logging` exporter was renamed to, and its output is a human-readable dump,
not a stable format — do not parse it.

Both `file` and `prometheus` are **contrib-only** exporters, so the core
collector image will not work.

### Why sample at all, given the file exporter

Both tools emit **cumulative monotonic** sums, so the totals at the end of a run
also contain ramp-up and ramp-down traffic, which the paper excludes. A sampled
series lets the steady state be isolated by subtracting the counter at t=60s
from the counter at t=180s. A single final reading cannot do that.

Note Prometheus munges names: `ebpf.jagent.resource.demand.cpu.ms` appears as
`ebpf_jagent_resource_demand_cpu_ms`.

### What the metrics pipeline cannot give you

Cumulative counters cannot produce the per-transaction distributions in the
paper's CPU table (mean, std. dev., CV). Those need per-transaction records:

- **jAgent** — only in `jagent-trace_*.txt`; its OTLP output is four cumulative
  sums per `(class, method)`.
- **OTJAE** — carried on **spans**, which is why the traces pipeline is enabled
  and not just metrics.

Treat the metrics path as the totals/SCI cross-check, and the trace file plus
spans as the source for distributions.

## Notes and caveats

- **Agent configuration goes through the environment, not `.env`.** The agent's
  `env_loader.c` overlays real environment variables on top of its `.env`, so
  the automation passes `JVM_LIB_PATH` and `OTLP_ENDPOINT` via
  `sudo -n env VAR=... ebpf-jagent`. The checkout on the SUT is left untouched.
  `sudo -n env` rather than `sudo -E`, so it does not depend on the sudoers
  `env_keep` policy.
- **`total_rate` is 10 req/s in `base.yml`; the paper's campaign ran at 50.**
  The rate comes from `LOAD_LEVELS` in `run.sh` / `run.bat`, which overrides the
  config. Pick a rate the uninstrumented baseline sustains comfortably: the
  traced configuration is ~5× slower, so a rate that saturates it distorts the
  comparison. Check the JMeter summariser for errors before trusting a result.
- **Debug output in the agent affects overhead.** Earlier builds had
  `bpf_printk` calls in `method_return` that fired on every return with
  `alloc > 100` or `net_tx > 0`; writing to the trace pipe on that hot path
  inflates the `jagent` numbers. They are gone as of `v0.0.7-alpha`, the
  release the paper measures, but check for them before quoting a figure from a
  `provision: source` build.
- **The empty-agent control is `spring_remote_noop.yml`.** It builds the
  stripped agent in [`noop_agent/`](noop_agent/) — same three USDT probes, empty
  handlers — via `jagent.provision: noop`, so nothing has to be assembled by
  hand. Everything else is identical to the `jagent` variant.
- **The release build is what the paper's numbers should come from.** Pinning
  `jagent.release_url` to a tag makes a rerun reproducible; `provision: source`
  measures whatever is in your checkout, which is useful for development but
  not citable.
- **The SUT must reach the controller's collector ports.** A host firewall on
  the controller (the Windows default blocks inbound) silently drops every
  export and you get an empty `.jsonl`. Allow inbound 4318/4317, or set
  `collector.controller_host` if the controller is multi-homed.
- **Clock drift between the machines is not checked.** The ASE template has a
  `helper/clock_drift.py` for this; it matters if you later correlate the JTL
  timestamps with the SUT-side traces.
