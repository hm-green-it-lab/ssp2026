@echo off
setlocal enabledelayedexpansion
rem
rem Run the experiment variants back-to-back (Windows counterpart of run.sh).
rem
rem This is an EXAMPLE campaign, not a fixed protocol: adjust the block of
rem variables below to match what you want to measure.
rem
rem Ordering note: the repetition loop is OUTERMOST and the configuration loop
rem innermost, so one repetition runs every variant before the next repetition
rem starts. Running 3x none, then 3x usdt, then 3x jagent would load any thermal
rem or background drift onto whichever variant happens to run last -- and since
rem the whole point is comparing latency between variants, that drift would be
rem indistinguishable from instrumentation overhead. Interleaving spreads it
rem evenly instead.
rem
rem Prerequisites: .env and paths.env configured, setup\sut_setup.py run once,
rem and inbound 4318/4317 reachable on this controller for the collector.

set "HERE=%~dp0"
set "MAIN=%HERE%main.py"
set "CONFIG_DIR=%HERE%configuration"

rem ── campaign definition ─────────────────────────────────────────────────────

rem Variant order within one repetition: the overhead ladder, each step adding
rem exactly one thing over the previous one, so each gap attributes cleanly.
rem
rem   none   uninstrumented baseline
rem   usdt   + JVM DTrace probes enabled, nothing attached (sites stay nop-patched)
rem   noop   + eBPF attached with empty handlers  -^> cost of the probes FIRING
rem   jagent + the real agent's bookkeeping       -^> cost of the agent's WORK
rem   otjae  the in-process comparison baseline
rem
rem Baseline first also means a broken SUT is caught on the cheapest run.
rem
rem   both   BOTH agents on one JVM, measuring the same transactions
rem
rem `both` is NOT a rung on that ladder: the two agents perturb each other, so
rem its response times belong in no ladder table and its `paired vs none` row is
rem the cost of running two agents, not a step. It is in the campaign anyway,
rem because it has to run under the same conditions as the jagent and otjae runs
rem it is compared against. As a separate session, drift between sessions would
rem be confounded with the very effect it exists to demonstrate -- that both
rem tools read the same kernel counter (se.sum_exec_runtime), so probe overhead
rem sits inside both readings and a CPU comparison taken within one run cannot
rem see it.
rem
rem A failed run does not invalidate its block: the paired analysis pairs each
rem variant with the baseline of its own repetition, independently per variant.
set "CONFIGS=spring_remote_none spring_remote_usdt spring_remote_noop spring_remote_jagent spring_remote_otjae spring_remote_both"

set "LOAD_LEVELS=50"

rem How many times to repeat the whole block of variants.
rem
rem 10 leaves the paired Wilcoxon plenty of room: the two-sided exact floor is
rem 2/2^10 = 0.002, well under the Bonferroni-corrected 0.0125 for the four
rem ladder comparisons against `none`.
rem
rem Positional balance is a separate question from power. A Latin square
rem completes every n repetitions, so six variants x 12 repetitions is exactly
rem two complete squares: every variant occupies every position exactly twice,
rem with the square re-randomised between replicates so the design is balanced
rem without being a fixed rotation. At 10 the second square would be truncated
rem after four rows and the balance would be approximate.
set "REPETITIONS=12"

rem Iterations inside a single main.py invocation. Kept at 1 so this script
rem controls the interleaving.
set "ITERATIONS_PER_RUN=1"

rem Idle time between runs, so the SUT returns to a comparable state.
set "COOLDOWN_SECONDS=120"

rem Set to 1 to keep going after a failed run instead of aborting.
rem
rem 1 for an unattended overnight campaign. Aborting is the right default when
rem someone is watching, but here a single transient failure -- a JMeter hiccup,
rem a slow SUT start -- would otherwise throw away the remaining seven hours.
rem Losing one run is cheap: the paired analysis pairs each variant with the
rem baseline of its own repetition independently, so a missing run drops that
rem one variant from that one block and nothing else.
rem
rem The failure this does not protect against is a systemic one (port stuck,
rem jar missing, SUT unreachable), where every subsequent run fails too and the
rem night produces nothing. Watch the first repetition -- six runs, about
rem 45 minutes -- before leaving it.
set "CONTINUE_ON_ERROR=1"

rem Seed for the within-repetition variant order. A fixed value makes the whole
rem campaign reproducible; 0 draws a fresh order each time.
rem
rem The order is randomised per repetition because a fixed sequence would
rem confound run position with variant: the baseline always first and coldest,
rem the last variant always ~30 minutes into a sustained-load session. Any
rem monotone drift would then land entirely on the instrumented variants and be
rem indistinguishable from instrumentation overhead.
set "ORDER_SEED=42"

rem ── plumbing ────────────────────────────────────────────────────────────────

where python >nul 2>&1
if errorlevel 1 (
    echo Python executable not found in PATH. Activate your venv or install Python.
    exit /b 1
)

set /a CONFIG_COUNT=0
for %%C in (%CONFIGS%) do set /a CONFIG_COUNT+=1
set /a LOAD_COUNT=0
for %%L in (%LOAD_LEVELS%) do set /a LOAD_COUNT+=1
set /a TOTAL_RUNS=CONFIG_COUNT*LOAD_COUNT*REPETITIONS

rem 4 minutes of load plus start-up, teardown and artifact collection.
set /a SECONDS_PER_RUN=330
set /a ESTIMATE=TOTAL_RUNS*(SECONDS_PER_RUN+COOLDOWN_SECONDS)
set /a EST_H=ESTIMATE/3600
set /a EST_M=(ESTIMATE%%3600)/60

echo ======================================================================
echo  Campaign: %CONFIG_COUNT% variant(s) x %LOAD_COUNT% load level(s) x %REPETITIONS% repetition(s)
echo  Total runs      : %TOTAL_RUNS%
echo  Cooldown        : %COOLDOWN_SECONDS%s between runs
echo  Rough estimate  : %EST_H%h %EST_M%m
echo ======================================================================

rem Resolve every configuration before committing hours to the campaign.
echo.
echo [~] Validating configurations ...
rem A flag rather than `exit /b` inside the loop: cmd does not propagate the
rem exit code of an `exit /b` executed inside a parenthesised for-block, so the
rem script would stop but report success.
set "VALIDATION_FAILED="
for %%C in (%CONFIGS%) do (
    python "%MAIN%" --config "%CONFIG_DIR%\%%C.yml" --dry-run >nul 2>&1
    if errorlevel 1 (
        echo [x] %%C.yml failed to resolve. Run it with --dry-run to see why.
        set "VALIDATION_FAILED=1"
    ) else (
        echo     [v] %%C.yml
    )
)
if defined VALIDATION_FAILED exit /b 1

set /a RUN=0
set /a FAILED=0
set "FAILURES="
set "START_TIME=%TIME%"

for /L %%R in (1,1,%REPETITIONS%) do (
    rem Fresh variant order for this block; echoed so the campaign is auditable.
    set "ORDER="
    for /f "usebackq delims=" %%O in (`python "%HERE%campaign_order.py" --seed %ORDER_SEED% --repetition %%R %CONFIGS%`) do set "ORDER=%%O"
    echo.
    echo ### repetition %%R/%REPETITIONS% order: !ORDER!

    for %%L in (%LOAD_LEVELS%) do (
        set /a POSITION=0
        for %%C in (!ORDER!) do (
            set /a RUN+=1
            set /a POSITION+=1
            echo.
            echo ----------------------------------------------------------------------
            echo  [!RUN!/%TOTAL_RUNS%] %%C ^| load=%%L req/s ^| repetition %%R/%REPETITIONS% ^| position !POSITION!
            echo  %DATE% %TIME%
            echo ----------------------------------------------------------------------

            python "%MAIN%" --config "%CONFIG_DIR%\%%C.yml" --total-rate %%L --iterations %ITERATIONS_PER_RUN% --repetition %%R --position !POSITION!
            if errorlevel 1 (
                echo [x] %%C ^(load=%%L, rep=%%R^) FAILED
                set /a FAILED+=1
                set "FAILURES=!FAILURES! [%%C load=%%L rep=%%R]"
                if not "%CONTINUE_ON_ERROR%"=="1" (
                    echo [x] Aborting campaign ^(set CONTINUE_ON_ERROR=1 to keep going^).
                    rem goto, not `exit /b`: see the note on the validation loop.
                    goto :summary
                )
            ) else (
                echo [v] %%C ^(load=%%L, rep=%%R^) finished
            )

            rem No cooldown after the final run.
            if !RUN! LSS %TOTAL_RUNS% (
                if %COOLDOWN_SECONDS% GTR 0 (
                    echo [~] Cooling down %COOLDOWN_SECONDS%s ...
                    call :cooldown %COOLDOWN_SECONDS%
                )
            )
        )
    )
)

:summary
set /a SUCCEEDED=RUN-FAILED
echo.
echo ======================================================================
echo  Campaign finished: %SUCCEEDED%/%TOTAL_RUNS% runs succeeded
echo  Started %START_TIME% -- ended %TIME%
if %FAILED% GTR 0 echo  Failures:!FAILURES!
echo  Results: %HERE%output\
echo ======================================================================

if %FAILED% GTR 0 exit /b 1
exit /b 0

rem ── cooldown helper ─────────────────────────────────────────────────────────
rem timeout.exe is called by absolute path on purpose: if Git's usr\bin is on
rem PATH -- which is common -- GNU `timeout` shadows the Windows one, rejects
rem `/t`, and the cooldown silently does not happen. ping is the fallback for
rem the case where stdin is redirected and timeout.exe refuses to run.
:cooldown
set /a _COOLDOWN_SECS=%~1
"%SystemRoot%\System32\timeout.exe" /t %_COOLDOWN_SECS% /nobreak >nul 2>&1
if errorlevel 1 (
    set /a _COOLDOWN_PINGS=_COOLDOWN_SECS+1
    "%SystemRoot%\System32\ping.exe" -n !_COOLDOWN_PINGS! 127.0.0.1 >nul 2>&1
)
goto :eof
