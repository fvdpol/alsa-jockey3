
# Instrumented run capturing bfptrace and ftrace (trace_printk)

> **Driver-side `trace_printk()` instrumentation has been removed from
> `jockey3.c`.** The `wdcheck` and per-callback "phantom completion" trace
> points these scripts were built alongside were stripped during merge-prep of
> the `dev/streaming-overhead` branch. The last commit that still carries them
> is **`750f18e`** ("ALSA: jockey3: E2d -- choose sub-packets per URB (N) from
> period size"); recover the exact code with `git show 750f18e:jockey3.c`.
> The bpftrace/kprobe scripts here are kept intact and need no driver rebuild
> — they are a ready starting point for any future analysis, and step 1 below
> already tracks the ground-truth completion age on its own probes.



enable the ftrace logging

``` bash
# Enable the tracing
echo 1 | sudo tee /sys/kernel/tracing/tracing_on

# Align ftrace's clock with dmesg/bpftrace's boot-relative timestamps
echo mono | sudo tee /sys/kernel/tracing/trace_clock

# Clear any old buffer content, then continuously drain trace_pipe to a
# file for the duration of the run (same pattern as bpftrace's -o: a
# blocking read that keeps up, not a fixed-size snapshot that could wrap)
sudo sh -c 'echo > /sys/kernel/tracing/trace'
sudo cat /sys/kernel/tracing/trace_pipe > wdcheck_trace.log &

```



1. Attach the bpftrace script before the run (kprobes only, no driver rebuild needed — already non-perturbation-validated for this exact use):

``` bash
cd alsa-jockey3
sudo bpftrace re/bpftrace/rate_stall_trace.bt -o rate_stall_trace.log &
# wait for "tracing armed -- rate_val=Hz..." to confirm it's up
``` 

This gives you STOP_URBS/ploytec_set_rate/START_URBS timing, every WD_CHECK tick with ground-truth real_age_since_completion_ms (tracked independently via its own completion-callback probes, so you don't need the driver's own trace_printk for this), and RECOVER lines naming which context triggered recovery.


2. Run the case as usual:
``` bash
cd tests/hw && ./runner.py --case JT-RATE-001 --unattended
```
Given N=1's 8.75% hit rate on arm64, ~100-150 changes should already catch several — no need for the full 240+.

3. Stop bpftrace cleanly (SIGINT, not kill -9, so it flushes):

``` bash 
sudo pkill -INT bpftrace
```

4. Move the log into the run's result directory so the correlator can find it alongside dmesg.txt:
mv rate_stall_trace.log tests/hw/results/<target>/<run-id>/

5. Correlate:
``` bash 
python3 re/bpftrace/correlate_trace.py onsets tests/hw/results/<target>/<run-id>/
# distribution of onset-after-JT-MARK timing across the whole run


python3 re/bpftrace/correlate_trace.py window tests/hw/results/<target>/<run-id>/ <CENTER_S> \
    --before 0.05 --after 0.3 --with-dmesg
# CENTER_S = boot-relative timestamp of one specific onset from dmesg.txt or step 5's output;
# dumps every trace + dmesg line around it
```

Non-perturbation check (the .bt file's own usage note, worth doing since this is a new N): run once with bpftrace attached and once without, same build, confirm stalls_per_change_pct/resets_per_change_pct don't move outside the existing noise band.

Not needed this time, skip it: the driver's own trace_printk/tracing_on dance (echo 1 | sudo tee /sys/kernel/tracing/tracing_on) — that was for the urbs_in_flight/disconnected detail specifically, but WD_CHECK's real_age_since_completion_ms already gives the ground-truth age number we actually need here. Only bother with it if the bpftrace data turns out ambiguous and you want that extra detail too.

Optional cross-check once you have result.json: python3 re/bpftrace/audit_resets.py against the run — recomputes resets_total_device from raw dmesg.txt independent of the metric, catches a repeat of the counting bug found on 2026-08-26.