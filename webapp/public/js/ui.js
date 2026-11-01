// DOM binding: controls call named actions, the page renders from the store.

import { toHex } from './midi.js';
import { EMPTY_PATCH_NAME, RHYTHM_PATTERNS } from './g6-protocol.js';

const MAX_LOG_LINES = 200;
const SLIDER_DEBOUNCE_MS = 200;

const CONNECTION_TEXT = {
  disconnected: 'Not connected',
  connecting: 'Connecting…',
  connected: 'Connected',
  busy: 'Reading patches…',
};

export function bindUi({ actions, store, midi }) {
  const $ = (id) => document.getElementById(id);
  const run = (name, ...args) => actions.execute(name, ...args);

  // Buttons with data-action="name" [data-arg="number"]
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button || button.disabled) return;
    const arg = button.dataset.arg;
    run(button.dataset.action, ...(arg === undefined ? [] : [Number(arg)]));
  });

  // WAV load: one explicit confirmation per load, then the box is cleared again
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-wav-name]');
    if (!button || button.disabled) return;
    const confirmed = $('looper-confirm').checked;
    $('looper-confirm').checked = false;
    updateLoadButtons(store.get());
    run('storage.loadWav', button.dataset.wavName, confirmed);
  });
  $('looper-confirm').addEventListener('change', () => updateLoadButtons(store.get()));
  $('btn-clear-uncertain').addEventListener('click', () => run('storage.clearUncertainLoad', true));
  $('looper-armed').addEventListener('change', (event) => run('looper.arm', event.target.checked));
  $('btn-looper-play').addEventListener('click', () => run('looper.play'));
  $('btn-looper-stop').addEventListener('click', () => run('looper.stop'));
  $('stop-mode-select').addEventListener('change', (event) => run('looper.setStopMode', Number(event.target.value)));
  $('position-confirm').addEventListener('change', () => renderLooper(store.get()));
  $('position-select').addEventListener('change', (event) => {
    const confirmed = $('position-confirm').checked;
    $('position-confirm').checked = false;
    event.target.blur();
    run('looper.setPosition', Number(event.target.value), confirmed);
  });
  let looperVolumeTimer = null;
  $('looper-volume-slider').addEventListener('input', (event) => {
    $('looper-volume-value').textContent = event.target.value;
    clearTimeout(looperVolumeTimer);
    looperVolumeTimer = setTimeout(() => run('looper.setVolume', Number(event.target.value)), SLIDER_DEBOUNCE_MS);
  });

  // Patch: the number field wins over the list
  $('patch-go').addEventListener('submit', (event) => {
    event.preventDefault();
    const slot = Number($('slot-input').value || $('patch-select').value);
    if (slot) run('patch.select', slot);
    $('slot-input').value = '';
  });
  $('patch-select').addEventListener('change', (event) => {
    if (event.target.value) run('patch.select', Number(event.target.value));
  });

  // Rhythm: BPM typed and confirmed with Enter
  $('bpm-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const bpm = Number($('bpm-input').value);
    $('bpm-input').blur();
    if (bpm) run('rhythm.setBpm', bpm);
  });
  $('bpm-input').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      $('bpm-input').blur();
      render(store.get());
    }
  });

  // Pattern list, numbered as in the owner's manual (protocol value + 1)
  $('pattern-select').replaceChildren(
    ...RHYTHM_PATTERNS.map((name, index) => new Option(`${index + 1} · ${name}`, String(index))),
  );
  $('pattern-select').addEventListener('change', (event) => run('rhythm.setPattern', Number(event.target.value)));

  // Volume: live number while dragging, one setter after a short pause
  let volumeTimer = null;
  $('volume-slider').addEventListener('input', (event) => {
    $('volume-value').textContent = event.target.value;
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => run('rhythm.setVolume', Number(event.target.value)), SLIDER_DEBOUNCE_MS);
  });

  // Keep the user's choice visible until the setter has been confirmed and the state read back
  let countPending = false;
  $('count-toggle').addEventListener('change', (event) => {
    countPending = true;
    run('rhythm.setCount', event.target.checked).finally(() => {
      countPending = false;
      render(store.get());
    });
  });

  // Re-read the rhythm state when the page comes back to the foreground
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && store.get().connection === 'connected') run('rhythm.refresh');
  });

  // Keyboard shortcuts (ignored while typing in a field)
  document.addEventListener('keydown', (event) => {
    if (event.target.closest('input, select, textarea') || event.metaKey || event.ctrlKey || event.altKey) return;
    const step = event.shiftKey ? 5 : 1;
    const keyActions = {
      ArrowLeft: ['patch.previous'],
      ArrowRight: ['patch.next'],
      ArrowUp: ['rhythm.nudgeBpm', step],
      ArrowDown: ['rhythm.nudgeBpm', -step],
      s: ['rhythm.start'], // rhythm only: Looper play/stop have no keyboard shortcut
      S: ['rhythm.start'],
      x: ['rhythm.stop'],
      X: ['rhythm.stop'],
      Escape: ['rhythm.stop'],
      r: ['rhythm.refresh'],
      R: ['rhythm.refresh'],
    };
    const entry = keyActions[event.key];
    if (!entry) return;
    event.preventDefault();
    run(...entry);
  });

  // MIDI log
  const logLines = [];
  const logPanel = $('log').closest('details');
  midi.addEventListener('log', (event) => {
    const { dir, bytes } = event.detail;
    const time = new Date().toLocaleTimeString('en-GB', { hour12: false });
    logLines.push(`${time} ${dir === 'out' ? '→' : '←'} ${toHex(bytes)}`);
    if (logLines.length > MAX_LOG_LINES) logLines.shift();
    if (logPanel.open) $('log').textContent = logLines.join('\n');
  });
  logPanel.addEventListener('toggle', () => {
    $('log').textContent = logLines.join('\n');
  });

  let renderedListKey = null;

  function render(state) {
    const connected = state.connection === 'connected';
    const { rhythm } = state;

    $('conn').dataset.state = state.connection;
    $('conn-text').textContent = CONNECTION_TEXT[state.connection] ?? state.connection;
    $('btn-connect').hidden = connected || state.connection === 'busy';
    $('btn-connect').disabled = state.connection === 'connecting';

    for (const el of document.querySelectorAll('main button, main input, main select')) el.disabled = !connected;

    showBanner($('error'), state.error);
    showBanner($('notice'), state.notice);

    // Patch
    $('slot').textContent = state.slot ?? '—';
    $('patch-name').textContent = state.patchName ?? '—';

    const listKey = JSON.stringify(state.patchList);
    if (listKey !== renderedListKey) {
      renderedListKey = listKey;
      renderPatchList($('patch-select'), state.patchList);
    }
    $('patch-select').value = state.patchList[state.slot] !== undefined ? String(state.slot) : '';
    const count = Object.values(state.patchList).filter((name) => name !== EMPTY_PATCH_NAME).length;
    $('list-status').textContent = state.listProgress
      ? `${state.listProgress.done}/${state.listProgress.total}`
      : count
        ? `${count} patches`
        : '';

    // Rhythm: never overwrite a control the user is editing
    const editing = document.activeElement;
    if (editing !== $('bpm-input')) $('bpm-input').value = rhythm.bpm ?? '';
    if (editing !== $('pattern-select')) $('pattern-select').value = rhythm.pattern ?? '';
    if (editing !== $('volume-slider') && rhythm.volume !== null) $('volume-slider').value = rhythm.volume;
    $('volume-value').textContent = rhythm.volume ?? '—';
    if (!countPending) {
      $('count-toggle').checked = rhythm.count === 1;
      $('count-toggle').indeterminate = rhythm.count === null;
    }

    renderStorage(state.storage);
    updateLoadButtons(state);
    renderLooper(state);

    const playback = rhythm.playingVerified ? (rhythm.playing ? 'playing' : 'stopped') : 'unknown';
    const last = rhythm.lastCommand ? ` · last sent: ${rhythm.lastCommand}` : '';
    $('rhythm-state').textContent = `Playback: ${playback}${last}`;
  }

  store.subscribe(render);
}

const formatGb = (bytes) => `${(bytes / 1e9).toFixed(2)} GB`;

function renderLooper(state) {
  const $ = (id) => document.getElementById(id);
  const { looper } = state;
  const connected = state.connection === 'connected';
  const loadable = Boolean(looper.loaded) && !state.storage.loadUncertain;
  $('looper-loaded').textContent = looper.loaded ? `· loaded by this page: ${looper.loaded.name}` : '· no WAV loaded by this page';
  $('looper-armed').checked = looper.armed;
  $('looper-armed').disabled = !connected || !loadable;
  $('btn-looper-play').disabled = !connected || !looper.armed;
  $('btn-looper-stop').disabled = !connected || !looper.stopAvailable;

  // Settings read from the device (64 09), not the transport state
  const settings = state.looperSettings;
  if (document.activeElement !== $('stop-mode-select')) {
    $('stop-mode-select').value = settings && settings.stopMode in { 0: 1, 1: 1, 2: 1 } ? String(settings.stopMode) : '';
  }
  $('stop-mode-select').disabled = !connected || looper.stopAvailable;
  if (document.activeElement !== $('looper-volume-slider') && settings) $('looper-volume-slider').value = settings.volume;
  $('looper-volume-value').textContent = settings ? settings.volume : '—';
  $('looper-volume-slider').disabled = !connected || !settings;
  if (document.activeElement !== $('position-select')) {
    $('position-select').value = settings && settings.position in { 0: 1, 1: 1 } ? String(settings.position) : '';
  }
  $('position-confirm').disabled = !connected || !settings || looper.stopAvailable;
  if ($('position-confirm').disabled) $('position-confirm').checked = false;
  $('position-select').disabled = $('position-confirm').disabled || !$('position-confirm').checked;
}

function updateLoadButtons(state) {
  const $ = (id) => document.getElementById(id);
  const uncertain = state.storage.loadUncertain;
  const enabled = state.connection === 'connected' && $('looper-confirm').checked && !uncertain;
  for (const button of document.querySelectorAll('button[data-wav-name]')) button.disabled = !enabled;
  $('load-uncertain').hidden = !uncertain;
  $('btn-clear-uncertain').disabled = !uncertain; // works offline too: it sends nothing
  if (uncertain) {
    const at = new Date(uncertain.at).toLocaleString('en-GB', { hour12: false });
    $('load-uncertain-text').textContent =
      `Uncertain outcome for the WAV load of "${uncertain.name}" (${uncertain.reason}, ${at}). ` +
      'Loads are blocked: look at the Looper screen (which file is loaded, is it stopped?) before allowing them again. ' +
      'A late reply to that load can still arrive.';
  }
}

let renderedWavKey = null;

function renderStorage(storage) {
  const $ = (id) => document.getElementById(id);
  if (storage.totalBytes === null) return;
  const used = storage.totalBytes - storage.freeBytes;
  $('sd-space').textContent = `${formatGb(storage.freeBytes)} free of ${formatGb(storage.totalBytes)} · ${formatGb(used)} used`;
  $('sd-used').style.width = `${storage.totalBytes ? Math.max(0.5, (used / storage.totalBytes) * 100) : 0}%`;
  const time = new Date(storage.readAt).toLocaleTimeString('en-GB', { hour12: false });
  const loaded = storage.lastLoad ? ` Last load sent: ${storage.lastLoad.name}.` : '';
  $('storage-meta').textContent = storage.stale
    ? `List read at ${time}; a WAV load was sent since then, so it may have changed: read the SD card again.${loaded}`
    : `Read at ${time} from the device's file cache (may not reflect the latest changes).${loaded}`;
  $('wav-count').textContent = `(${storage.wavCount})`;

  const wavKey = storage.readAt;
  if (wavKey === renderedWavKey) return;
  renderedWavKey = wavKey;
  const items = storage.wavNames.map((name) => {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = name;
    const button = document.createElement('button');
    button.className = 'btn ghost small';
    button.textContent = 'Load';
    button.dataset.wavName = name;
    button.title = `Load ${name} into the Looper`;
    li.append(label, button);
    return li;
  });
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No WAV files.';
    items.push(li);
  }
  $('wav-list').replaceChildren(...items);
}

function showBanner(el, text) {
  el.hidden = !text;
  el.textContent = text ?? '';
}

function renderPatchList(select, list) {
  const slots = Object.keys(list)
    .map(Number)
    .filter((slot) => list[slot] !== EMPTY_PATCH_NAME)
    .sort((a, b) => a - b);
  select.replaceChildren(new Option(slots.length ? 'Choose a patch…' : 'Patch list not loaded', ''));
  const groups = [
    ['User patches', slots.filter((s) => s > 100)],
    ['Factory', slots.filter((s) => s <= 100)],
  ];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    const group = document.createElement('optgroup');
    group.label = label;
    for (const slot of items) group.append(new Option(`${slot} · ${list[slot]}`, String(slot)));
    select.append(group);
  }
}
