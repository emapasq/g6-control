// Entry point: wires transport, actions, store and UI.

import { G6Midi } from './midi.js';
import { G6Actions } from './actions.js';
import { createStore, initialState } from './store.js';
import { bindUi } from './ui.js';

const store = createStore(initialState);
const midi = new G6Midi();
const actions = new G6Actions(midi, store);

bindUi({ actions, store, midi });

// Future inputs (e.g. a MIDI foot controller) will only need `actions`: see js/inputs/README.md
window.g6 = { actions, store, midi };

if (!G6Midi.isSupported()) {
  store.set({ error: 'This browser does not support Web MIDI: open the page in Chrome.' });
} else {
  autoConnect();
}

/** Connect on load only if Chrome already granted MIDI with SysEx (no permission prompt). */
async function autoConnect() {
  try {
    const status = await navigator.permissions.query({ name: 'midi', sysex: true });
    if (status.state === 'granted') actions.execute('connect');
  } catch {
    /* permissions API unavailable: wait for the Connect button */
  }
}
