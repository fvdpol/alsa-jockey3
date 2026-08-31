# tests/

Everything used to test this driver, in three parts.

| Path | What it is |
|---|---|
| `codec/` | Codec correctness and benchmarking. Runs anywhere, needs no hardware. Guide: **[../docs/codec_testing.md](../docs/codec_testing.md)** |
| `hw/` | The test runner, catalog and cases that exercise the driver against real hardware. Guide: **[../docs/test_strategy.md](../docs/test_strategy.md)** |
| `build/` | Syncing sources into a kernel tree, the L1 gates, and building target kernels and modules. Guide: **[../docs/environments.md](../docs/environments.md)** |
| `scripts-alsa-dev/` | Archive of the ad-hoc scripts this suite grew out of. Kept for reference; not maintained. |

Before building anything, read **[../docs/environments.md](../docs/environments.md)**:
the driver lives in five trees at once, and which one you build in decides
whether you get a verdict, a loadable module, or a module that silently will
not insert.

---

## `codec/`

| Path | What it is |
|---|---|
| `ploytec_model.py` | Independent model of the wire format, derived structurally rather than copied from the C. The oracle everything else is anchored to. |
| `genvectors.py` | Turns the model into `../../ploytec_codec_test_vectors.h`, the golden vectors shared by the KUnit suite and the bench. |
| `run_kunit.sh` | Runs the in-kernel KUnit suite, under UML or cross-built for QEMU. |
| `codecbench.py` | Builds, validates and benchmarks the codec in user space. |
| `harness/` | The C bench: `main.c`, the implementation registry, and the kernel-header stubs that let driver source compile in user space. |
| `candidates/` | Experimental codec implementations. Drop a file in, rerun `./codecbench.py test`. |
| `build/` | Generated; git-ignored. Holds the verbatim copy of the driver codec plus its SHA-256 manifest. |

```sh
cd tests/codec
./run_kunit.sh                 # KUnit under UML
./run_kunit.sh --list          # which cross targets are runnable

./codecbench.py test           # correctness, every implementation
./codecbench.py bench --quick  # fast, noisy benchmark
./codecbench.py bench          # 30s per measurement, trustworthy
./codecbench.py check-sync     # is build/ still the current driver code?
```

### Two things worth knowing

**The driver source is copied, not symlinked.** `codecbench.py build` takes a
verbatim copy into `build/src/` and records its SHA-256. `check-sync` fails if
the driver has moved on since, so a benchmark number cannot be attributed to
the wrong revision. Run it before quoting figures.

**Nothing here requires changes to the driver.** All three codec variants are
built from that single copy by compiling it three times with different
`CONFIG_*` defines and renaming its public symbols on the command line. The
driver has no exports, `#ifdef`s or hooks added for testing, and it must stay
that way.

---

## `hw/`

| Path | What it is |
|---|---|
| `runner.py` | Coordinator. Runs a profile on this machine and writes a result record. |
| `catalog.yaml` | The register of every test case, implemented or not. |
| `targets.yaml` | The kernel builds tests run against, and the capability vocabulary. |
| `lib/capabilities.py` | What this machine can actually do right now. |
| `profiles.yaml` | Which cases run, how many times, with what parameters, per target. |
| `lib/` | Environment capture, dmesg classification, ALSA helpers, result schema. |
| `cases/` | One executable per automated case. |
| `actions/` | Small reusable steps (load the driver, play a tone, change rate). |
| `checklist.py` | Renders manual cases to a checklist and reads the answers back. |
| `ledger.py` | What has been tested, on what, how recently — and metric trends. |
| `restart_timing.py` | Accumulates URB cold/warm-restart timings from runs into `data/restart_timing.json`, for sizing the driver's grace periods. |
| `selftest.py` | Tests for the framework itself. No hardware, no root. |
| `priv/` | The suite's entire privileged surface: one root-owned helper, one sudoers entry. Guide: **[priv/README.md](hw/priv/README.md)** |
| `results/` | Generated; git-ignored. |

```sh
cd tests/hw
sudo priv/install.sh                      # once per machine; the only password
./runner.py --list                        # cases and profiles
./runner.py --profile smoke --dry-run     # what would run here
./runner.py --profile smoke               # run it -- as an ordinary user
./ledger.py                               # coverage and staleness
./restart_timing.py report                # cold/warm restart latency, per arch/stream
./selftest.py                             # check the framework itself
```

`restart_timing.py` folds each prod-kernel run that had dynamic debug on into a
growing histogram keyed by architecture, stream and start type:

  - **cold** — a URB ring (re)start: first open, rate change, USB reset, resume.
    Latency to the first real completion; what `cold_start_grace_ms` is sized
    against.
  - **warm** — the stall watchdog's own light restart of a running ring.
  - **liveness** — `prepare()`/`hw_params()` found the (still-running) ring
    mid-stall and waited it out. Not a restart latency; kept separate so it
    does not inflate the cold tail.

`runner.py` ingests automatically at the end of a run; `ingest` / `rebuild` are
there for back-filling and for re-reading the source runs after the extractor
changes (`EXTRACTOR_VERSION` in `lib/restart_timing.py`, then
`rebuild --reparse`). It stores per-run histograms plus the run's identity, not
the raw samples, so any percentile band is one `report` away and the source
runs stay listed for re-derivation. Every sample is a "dynamic debug on" sample
by construction (the measurement is a `dev_dbg` line), which reads slightly slow
versus production — the safe direction for sizing a timeout.

Parameters come from `catalog.yaml`, then the profile entry, then the
per-target override, and `--param KEY=VALUE` last — the operator at the bench
is the most specific source there is. It applies to every case in the plan, so
it is normally paired with `--case`, and the value is parsed as JSON when it
can be, so `capture=false` is the boolean and `rates=[44100,96000]` is a list:

```sh
./runner.py --case JT-RATE-001 --unattended \
    --param rate_change_stream=playback --param gap_seconds=3
```

This is how a parameter sweep is run without editing the catalog between arms.
`run.json` records the **resolved** parameters, so each arm identifies itself
afterwards and two runs can be told apart without reconstructing what was
edited when.

`--note "free text"` records that text in `run.json` too, for the details
`--param` cannot express -- which bench build was loaded, that a bpftrace
script was attached alongside the case, why this particular run exists. It
keeps that context inside the run record itself rather than in chat history
or a separate log that can drift out of sync with which run it was about:

```sh
./runner.py --case JT-RATE-001 --unattended \
    --param rates=[96000,48000] --param sweep_order=as-given \
    --note "N=8 driver, rate_stall_trace.bt attached"
```

The runner is **not** run under sudo. Playback, capture, MIDI and sysfs need no
privilege; the few operations that do go through `priv/jockey3-testctl`. The
test user needs to be in the `audio` group, and nothing more.

The runner executes **on the machine with the hardware attached**, not on a
build host. That is what lets the same suite run on a Raspberry Pi you plugged
in five minutes ago.

### Test machine prerequisites

Beyond `priv/install.sh`, one boot-time setting matters and is easy to miss
because nothing fails loudly when it is wrong — cases just run with degraded
diagnostics.

**`log_buf_len=4M` on the kernel command line.** The stock printk ring buffer
is `CONFIG_LOG_BUF_SHIFT=17`, 128 KiB, sized for ordinary boot-to-shutdown
logging. A marker-heavy case blows through that long before it finishes: a
20000-change JT-RATE-003 run writes 40000 `JT-MARK` lines, and at the default
size the buffer wraps within about the first 1050 changes, so by the time the
case reads the log at the end only the tail survives. That showed up as "94%
of kernel-log markers are missing" on both `alsa-test` and `pi4test` on
2026-08-20, which suppresses every per-change figure the case reports (steady
Hz, plateau/xrun attribution) — the run's overall pass/fail is unaffected, but
the diagnostics needed to explain a fail are not. `4M` was sized to hold a
full JT-RATE-003 run's markers plus ordinary driver chatter with margin; add
it to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub` (then
`sudo update-grub`) on Debian test hosts, or append it to
`/boot/firmware/cmdline.txt` on a Raspberry Pi, and reboot. Verify with
`cat /proc/cmdline | grep -o 'log_buf_len=[^ ]*'` after the reboot.

This is a property of the physical test machine, not of a kernel build, so it
is not part of any `tests/configs/*.config` and is not something
`derive-prod.sh` touches — it has to be set once per machine and survives
across every kernel it boots.

`4M` still is not enough for a full 20000-change soak with dynamic debug on
(that is closer to 70 MB of log). For those, `runner.py` also streams the whole
kernel log to `<run>/kmsg.log` as it is emitted — immune to ring-buffer wrap,
and cheap on memory because the follower writes straight to the file. `dmesg.txt`
is taken from that capture when it is present, and from the live buffer
otherwise. The capture needs the `kmsg-follow` helper verb, so re-run
`priv/install.sh` after updating the suite.

### Live feedback while a case runs

Hardware cases are slow. `JT-AUDIO-005` takes about 125 seconds, because it
power-cycles the device ten times and captures two seconds from each. A case
that prints nothing for that long leaves the operator watching `dmesg` in
another window to find out whether anything is still happening, which is not
good enough. **Every case that iterates reports once per iteration.**

The shape, and it is worth following exactly:

```
    cycle 1/10  ....  power cycling            <- transient, rewritten in place
    cycle 1/10  ....  waiting for the card
    cycle 1/10  ....  capturing 2s
    cycle 1/10  pass  100.00% non-zero of 88200 frames, card ready in 20.4 ms
    cycle 2/10  FAIL    0.00% non-zero of 88200 frames, card ready in 20.4 ms
```

Three properties earn their keep:

- **The iteration and the total**, so a long run has a visible end.
- **The phase, named**, while the iteration is in flight. "Power cycling",
  "waiting for the card", "capturing 2s" — the operator can tell a slow step
  from a hung one, and it is obvious which part of the rig to look at.
- **A verdict per iteration, replacing the phase line.** One permanent line
  per iteration and no scrollback churn, ending in a summary that says how
  many passed.

`Case` provides both halves and they compose:

| | |
|---|---|
| `c.status(text)` | ends in a carriage return. The runner treats it as transient and `lib/term.py` redraws it in place, padding to erase a longer previous line. |
| `c.progress(text)` | ends in a newline. Wipes the held transient line first, then prints permanently. |

On a pipe or in CI the terminal handling switches off and each update becomes
an ordinary line, so a log keeps the whole history rather than a line that
overwrote itself.

**The ordering trap.** The runner derives a case's failure reason from the
**last line of stderr**. A per-iteration line printed *after* `c.fail()`
therefore becomes the recorded verdict, quietly replacing the real reason.
Report first, fail second — always. In `JT-AUDIO-005` this is why
`reenumerate()` hands its failure reason back to the caller instead of calling
`c.fail()` itself.

### Capabilities, and cases that fall back to being done by hand

A case declares what it needs from its surroundings — a device, a loopback
cable, a person. Those are resolved for **this machine, at run time**, not
declared per target: `x86_64-debug` and `x86_64-prod` are the same EliteDesk
booted differently, so a cable plugged into it belongs to both.

Three sources, and only one of them is a file you edit:

| | |
|---|---|
| **probed** | the machine is asked — device, root, sox, qemu, the ppps hub |
| **declared** | `~/.config/jockey3/machine.yaml` — speakers, loopback cable, signal source, relay, second unit, quiet machine |
| **invocation** | `human`, from whether `--unattended` was passed |

That file lives outside the repository on purpose: the working tree is a
Seafile share, so a file inside it would sync one machine's rig to every
other — and this driver is headed for mainline, where a personal hostname or
home directory would be plainly wrong. Absent file means everything false,
which costs coverage and never invents it. A declaration can only take a
capability **away** — `usb-switch: false` is a useful "do not power-cycle
today"; `loopback-cable: true` does not put a cable in the socket.

### machine.yaml

Capabilities are one section of it. The same file carries everything else that
is a property of this machine rather than of the driver — see
`hw/machine.yaml.example` for the full schema, and `lib/machineconf.py` for
the loader.

```yaml
capabilities:      # declared-only; can subtract, never add
  loopback-cable: true
power_switch:      # mains relay on the device's own supply, for JT-AUDIO-005
  url: http://relay.example.com
paths:             # where a built module is fetched from, per machine
  build_host: alsa-dev
  build_path: ~/kbuild/{target}/sound/usb/jockey3/snd-reloop-jockey3.ko
profiles:
  default: smoke                                    # what a bare run does here
  applicable: [smoke, functional, regression, soak] # advisory, not a gate
```

Every setting can be overridden by an environment variable for a single run,
and the variable wins. `profiles.default` is what makes `./runner.py` with no
arguments do the right thing on the build server and on the bench without
anyone having to remember which.

`profiles.applicable` is deliberately **advisory**: it warns and continues,
and `--force` silences it. What can actually run here is already settled by
capabilities, case by case and with a reason attached; a second list would be
a second source of truth and would silently drop cases as soon as the two
drifted.

The older `capabilities.yaml` name is still read when `machine.yaml` is
absent, so machines already set up keep working.

```sh
./runner.py --profile smoke              # attended: a person is here
./runner.py --profile smoke --unattended # CI: withhold 'human'
```

When a capability is missing the case does not simply vanish. If what is
missing is an **actuator** — the ppps hub, the mains relay, something a person
can do by hand — and the case carries manual steps, it demotes to manual,
lands in the checklist, and the coverage survives with only the labor
changing. Anything else blocks: no controller attached means there is nothing
for an operator to test either, and a checklist politely asking someone to
unplug a device that is not there is worse than an honest gap.

Which capabilities held is recorded in every run. Without that, a pass from a
day the loopback cable was connected reads identically to one taken with it
coiled on the bench, and `ledger.py` cannot tell coverage from its absence.

### Manual cases

Some cases need a human: nobody has wired a photodiode to the LEDs, and no
metric catches "slight crackle on headphone right, only at 96 kHz" as well as
an ear does. Those live in the same catalog and the same profiles as the
automated ones, so coverage is one picture rather than two — the runner marks
them `PENDING` and moves on.

`checklist.py` renders them to markdown, you fill it in, and it reads the
answers back into the same run record. No paths, in either direction:

```sh
./runner.py --profile smoke                      # JT-MIDI-002 -> PENDING
./checklist.py --profile smoke > /tmp/checklist.md
$EDITOR /tmp/checklist.md                        # do the tests, record verdicts
./checklist.py --import /tmp/checklist.md
```

The target is detected from the running kernel, and the run is the most recent
one for that target and profile. The rendered file records which run it belongs
to, so `--import` needs nothing else — and answers cannot land in a different
run than the one they were written for, which is exactly what typing paths
invites. `--import -` reads standard input.

Two things it refuses to do quietly:

- **No matching run** is an error, not an empty checklist. Otherwise you find
  out after doing the work rather than before.
- **A run older than 24 hours** gets a warning naming its age, because manual
  verdicts would be attached to it.

The target is never assumed. It selects the per-target overrides in
`profiles.yaml`, so a wrong one renders the wrong cases with the wrong
parameters — on `armhf-prod` the audio cases run at two sample rates, not four.
Override either with `--target` or `--run` when you need to.

On import the completed checklist is copied into the run directory beside
`dmesg.txt`. The verdicts go into `run.json`, but the comments and the steps
actually followed are evidence too, and a month later "slight crackle on
headphone right, only at 96 kHz" is the part worth having.

Each case ends with one machine-readable line. Edit the `result:` and add a
comment; everything else in the file is for you:

```
- id: JT-MIDI-002 | result: pass | comment: all LEDs lit, attract mode stopped
```

`result:` takes `pass`, `fail`, `skip` or `blocked`. Leave it as `?` and that
case stays pending. On import the run's outcome is re-derived — with one
exception: a run already marked `INVESTIGATE` stays that way, because a kernel
defect outranks any number of manual passes. An answer for a case that was not
in the run is appended rather than discarded, since coverage that happened is
coverage.

A comment on a *passing* case is not wasted effort. It is how the next bug gets
found early.
