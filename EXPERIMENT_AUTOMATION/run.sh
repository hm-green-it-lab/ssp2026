#!/bin/bash
#
# Run the experiment variants back-to-back (bash counterpart of run.bat).
#
# This is an EXAMPLE campaign, not a fixed protocol: adjust the block of
# variables below to match what you want to measure.
#
# Ordering note: the repetition loop is OUTERMOST and the configuration loop
# innermost, so one repetition runs every variant before the next repetition
# starts. Running 3x none, then 3x usdt, then 3x jagent would load any thermal
# or background drift onto whichever variant happens to run last -- and since
# the whole point is comparing latency between variants, that drift would be
# indistinguishable from instrumentation overhead. Interleaving spreads it
# evenly instead.
#
# Prerequisites: `.env` and `paths.env` configured, `setup/sut_setup.py` run
# once, and inbound 4318/4317 reachable on this controller for the collector.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MAIN="${HERE}/main.py"
CONFIG_DIR="${HERE}/configuration"

# ── campaign definition ───────────────────────────────────────────────────────

# Variant order within one repetition: the overhead ladder, each step adding
# exactly one thing over the previous one, so each gap attributes cleanly.
#
#   none   uninstrumented baseline
#   usdt   + JVM DTrace probes enabled, nothing attached (sites stay nop-patched)
#   noop   + eBPF attached with empty handlers  -> cost of the probes FIRING
#   jagent + the real agent's bookkeeping       -> cost of the agent's WORK
#   otjae  the in-process comparison baseline
#
# Baseline first also means a broken SUT is caught on the cheapest run.
#
#   both   BOTH agents on one JVM, measuring the same transactions
#
# `both` is NOT a rung on that ladder: the two agents perturb each other, so its
# response times belong in no ladder table. It is in the campaign anyway,
# because it has to run under the same conditions as the jagent and otjae runs
# it is compared against -- as a separate session, drift between sessions would
# be confounded with the effect it exists to demonstrate.
CONFIGS=(
    "spring_remote_none"
    "spring_remote_usdt"
    "spring_remote_noop"
    "spring_remote_jagent"
    "spring_remote_otjae"
    "spring_remote_both"
)

LOAD_LEVELS=(50)

# How many times to repeat the whole block of variants.
REPETITIONS=12

# Iterations inside a single main.py invocation. Kept at 1 so this script
# controls the interleaving; raise it only if you want consecutive repeats of
# the same variant.
ITERATIONS_PER_RUN=1

# Idle time between runs, so the SUT returns to a comparable state.
COOLDOWN_SECONDS=120

# Set to 1 to keep going after a failed run instead of aborting the campaign.
CONTINUE_ON_ERROR=1

# Seed for the within-repetition variant order. A fixed value makes the whole
# campaign reproducible; 0 draws a fresh order each time.
#
# The order is randomised per repetition because a fixed sequence would confound
# run position with variant: the baseline always first and coldest, the last
# variant always ~30 minutes into a sustained-load session. Any monotone drift
# would then land entirely on the instrumented variants and be
# indistinguishable from instrumentation overhead.
ORDER_SEED=42

# ── plumbing ──────────────────────────────────────────────────────────────────

# Probe each candidate rather than trusting `command -v`: on Windows (Git Bash,
# WSL interop) `python3` is often an App Execution Alias that resolves but fails
# to run.
PYTHON_CMD=""
for candidate in python3 python py; do
    if command -v "${candidate}" > /dev/null 2>&1 \
        && "${candidate}" --version > /dev/null 2>&1; then
        PYTHON_CMD="${candidate}"
        break
    fi
done
if [[ -z "${PYTHON_CMD}" ]]; then
    echo "No working Python found in PATH. Activate your venv or install Python."
    exit 1
fi
echo "[~] Using ${PYTHON_CMD} ($(${PYTHON_CMD} --version 2>&1))"

TOTAL_RUNS=$(( ${#CONFIGS[@]} * ${#LOAD_LEVELS[@]} * REPETITIONS ))
# 4 minutes of load plus start-up, teardown and artifact collection.
SECONDS_PER_RUN=330
ESTIMATE=$(( TOTAL_RUNS * (SECONDS_PER_RUN + COOLDOWN_SECONDS) ))

echo "======================================================================"
echo " Campaign: ${#CONFIGS[@]} variant(s) x ${#LOAD_LEVELS[@]} load level(s) x ${REPETITIONS} repetition(s)"
echo " Total runs      : ${TOTAL_RUNS}"
echo " Cooldown        : ${COOLDOWN_SECONDS}s between runs"
echo " Rough estimate  : $(( ESTIMATE / 3600 ))h $(( (ESTIMATE % 3600) / 60 ))m"
echo "======================================================================"

# Resolve every configuration before committing hours to the campaign.
echo ""
echo "[~] Validating configurations ..."
for config in "${CONFIGS[@]}"; do
    if ! "${PYTHON_CMD}" "${MAIN}" --config "${CONFIG_DIR}/${config}.yml" --dry-run > /dev/null 2>&1; then
        echo "[x] ${config}.yml failed to resolve. Run it with --dry-run to see why."
        exit 1
    fi
    echo "    [v] ${config}.yml"
done

START_TS=$(date +%s)
RUN=0
FAILED=0
FAILURES=()

trap 'echo ""; echo "[!] Interrupted after ${RUN}/${TOTAL_RUNS} runs."; exit 130' INT

for rep in $(seq 1 "${REPETITIONS}"); do
    # Fresh variant order for this block; recorded so the campaign is auditable.
    ORDER=$("${PYTHON_CMD}" "${HERE}/campaign_order.py" \
        --seed "${ORDER_SEED}" --repetition "${rep}" "${CONFIGS[@]}")
    echo ""
    echo "### repetition ${rep}/${REPETITIONS} order: ${ORDER}"

    for load in "${LOAD_LEVELS[@]}"; do
        POSITION=0
        for config in ${ORDER}; do
            RUN=$(( RUN + 1 ))
            POSITION=$(( POSITION + 1 ))
            echo ""
            echo "----------------------------------------------------------------------"
            echo " [${RUN}/${TOTAL_RUNS}] ${config} | load=${load} req/s | repetition ${rep}/${REPETITIONS} | position ${POSITION}"
            echo " $(date '+%Y-%m-%d %H:%M:%S')"
            echo "----------------------------------------------------------------------"

            if "${PYTHON_CMD}" "${MAIN}" \
                --config "${CONFIG_DIR}/${config}.yml" \
                --total-rate "${load}" \
                --iterations "${ITERATIONS_PER_RUN}" \
                --repetition "${rep}" \
                --position "${POSITION}"; then
                echo "[v] ${config} (load=${load}, rep=${rep}) finished"
            else
                echo "[x] ${config} (load=${load}, rep=${rep}) FAILED"
                FAILED=$(( FAILED + 1 ))
                FAILURES+=("${config} load=${load} rep=${rep}")
                if [[ "${CONTINUE_ON_ERROR}" != "1" ]]; then
                    echo "[x] Aborting campaign (set CONTINUE_ON_ERROR=1 to keep going)."
                    exit 1
                fi
            fi

            # No cooldown after the final run.
            if (( RUN < TOTAL_RUNS )) && (( COOLDOWN_SECONDS > 0 )); then
                echo "[~] Cooling down ${COOLDOWN_SECONDS}s ..."
                sleep "${COOLDOWN_SECONDS}"
            fi
        done
    done
done

ELAPSED=$(( $(date +%s) - START_TS ))
echo ""
echo "======================================================================"
echo " Campaign finished: $(( TOTAL_RUNS - FAILED ))/${TOTAL_RUNS} runs succeeded"
echo " Elapsed: $(( ELAPSED / 3600 ))h $(( (ELAPSED % 3600) / 60 ))m"
if (( FAILED > 0 )); then
    echo " Failures:"
    for failure in "${FAILURES[@]}"; do
        echo "   - ${failure}"
    done
fi
echo " Results: ${HERE}/output/"
echo "======================================================================"

exit $(( FAILED > 0 ? 1 : 0 ))
