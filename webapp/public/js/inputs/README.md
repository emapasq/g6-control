# inputs/ — future control sources (not implemented)

A MIDI foot controller (or any other input) will be a module here that:

1. opens its own MIDI input through the same `MIDIAccess` (`window.g6.midi.access`);
2. translates incoming messages with a mapping table (see `mapping.example.json`);
3. calls `window.g6.actions.execute(name, ...args)` — the same named actions used by the page.

It must never send anything to the G6 directly: only actions do.

Action names: `connect`, `patch.refresh`, `patch.select(slot)`, `patch.next`, `patch.previous`, `patch.loadList`,
`rhythm.refresh`, `rhythm.start`, `rhythm.stop`, `rhythm.setBpm(bpm)`, `rhythm.nudgeBpm(delta)`,
`rhythm.setPattern(index)`, `rhythm.setVolume(volume)`, `rhythm.setCount(enabled)`, `storage.refresh`,
`storage.loadWav(name, looperStoppedConfirmed)` (never bind it to a foot switch without a separate confirmation),
`storage.clearUncertainLoad(looperCheckedConfirmed)` (only after looking at the Looper screen),
`looper.arm(wavLoadedAndStoppedConfirmed)`, `looper.play`, `looper.stop` (never bind play to a foot switch: it bypasses
the confirmation on the display), `looper.setStopMode(mode)` (0 INSTANT, 1 FINISH, 2 FADE OUT), `looper.setVolume(volume)` (0-100),
`looper.setPosition(position, resetConfirmed)` (0 PRE-EFFECTS, 1 POST-EFFECTS; stops and resets the Looper: never bind
it to a foot switch without a separate confirmation).

`../tap-tempo.js` computes a BPM from taps, for a future tap footswitch (the G6 has no "tap" message: send the BPM).
