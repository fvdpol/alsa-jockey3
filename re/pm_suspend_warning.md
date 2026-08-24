# Open finding: "PM: parent 1-13:1.0 should not be sleeping"

Seen four times per run, during `JT-PM-001`, on **two separate runs on a
production kernel** (2026-08-09 and 2026-08-10). The lines name this
device's own interfaces (`1-13:1.0`, `1-13:1.1`) and its own endpoints --
`ep_05` playback, `ep_86` capture, `ep_83` MIDI in, plus `ep_02`. It is a
`dev_warn` from `drivers/base/power/main.c`, emitted when a child device is
resumed while its parent is still suspended.

## Status: unresolved, deliberately left unclassified

It is deliberately left **unclassified** in `lib/rules.yaml` so it keeps
being surfaced. Putting it in `unrelated` would hide a message about our
own device during the one case that exercises suspend; putting it in
`driver_fail` would assert a driver defect nobody has established -- the
ordering may be usbcore's rather than ours.

Note it does not match the "ours" pattern, since the lines say `ep_05`
rather than the module name, which is why it is not attributed
automatically.

## What is needed

A look at the suspend and resume callbacks against the USB PM model, and
then a decision: fix, or allowlist with a reason.
