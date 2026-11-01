// Action layer: every command has a stable name. The UI (and later other inputs, see inputs/)
// only calls execute(name, ...args) and never talks to the G6 directly.
// Setters are sent exactly once: no automatic retries.

import * as P from './g6-protocol.js';
import { G6Midi, sleep } from './midi.js';

const PATCH_LIST_KEY = 'g6.patchList';
const WAV_LOAD_UNCERTAIN_KEY = 'g6.wavLoadUncertain';
const LOOPER_KEY = 'g6.looper';
const LOOPER_LISTEN_MS = 1000; // Looper notifications after play/stop (nothing is waited for)
const CONFIRM_TIMEOUT_MS = 1000;
const WAV_LOAD_TIMEOUT_MS = 10000; // the device copies the file before answering
const WAV_LOAD_LINGER_MS = 1500; // late notifications after the reply

export class G6Actions {
  constructor(midi, store) {
    this.midi = midi;
    this.store = store;
    this.wantConnected = false;
    this.inPcMode = false;
    this.wavLoadTimeoutMs = WAV_LOAD_TIMEOUT_MS;
    this.wavLoadLingerMs = WAV_LOAD_LINGER_MS;
    this.looperListenMs = LOOPER_LISTEN_MS;

    this.registry = {
      connect: () => this.connect(),
      'patch.refresh': () => this.#queued(() => this.#refreshPatch()),
      'patch.select': (slot) => this.#queued(() => this.#selectSlot(slot)),
      'patch.next': () => this.#queued(() => this.#step(+1)),
      'patch.previous': () => this.#queued(() => this.#step(-1)),
      'patch.loadList': () => this.#queued(() => this.#loadPatchList()),
      'rhythm.refresh': () => this.#queued(() => this.#readRhythmState()),
      'rhythm.start': () => this.#queued(() => this.#transport('start')),
      'rhythm.stop': () => this.#queued(() => this.#transport('stop')),
      'rhythm.setBpm': (bpm) => this.#queued(() => this.#setRhythm('bpm', bpm)),
      'rhythm.nudgeBpm': (delta) => this.#queued(() => this.#nudgeBpm(delta)),
      'rhythm.setPattern': (index) => this.#queued(() => this.#setRhythm('pattern', index)),
      'rhythm.setVolume': (volume) => this.#queued(() => this.#setRhythm('volume', volume)),
      'rhythm.setCount': (enabled) => this.#queued(() => this.#setRhythm('count', enabled ? 1 : 0)),
      'storage.refresh': () => this.#queued(() => this.#readStorage()),
      'storage.loadWav': (name, looperStoppedConfirmed) =>
        this.#queued(() => this.#loadWav(name, looperStoppedConfirmed)),
      // No MIDI: only after the user has checked the Looper screen.
      'storage.clearUncertainLoad': (looperCheckedConfirmed) => this.#clearUncertainLoad(looperCheckedConfirmed),
      // Looper play/stop, gated: load by this page, confirmation on the display, one play, then one stop.
      'looper.arm': (wavLoadedAndStoppedConfirmed) => this.#armPlay(wavLoadedAndStoppedConfirmed),
      'looper.play': () => this.#queued(() => this.#looperPlay()),
      'looper.stop': () => this.#queued(() => this.#looperStop()),
      'looper.setStopMode': (mode) => this.#queued(() => this.#setStopMode(mode)),
      'looper.setVolume': (volume) => this.#queued(() => this.#setLooperVolume(volume)),
      // Stops and resets the Looper: only with the user's confirmation, never resent.
      'looper.setPosition': (position, resetConfirmed) => this.#queued(() => this.#setPosition(position, resetConfirmed)),
    };

    midi.addEventListener('message', (e) => this.#onDeviceMessage(e.detail.body));
    midi.addEventListener('ports', (e) => this.#onPorts(e.detail.present));
    this.store.set({ patchList: loadPatchList() });
    this.#updateStorage({ loadUncertain: readJson(WAV_LOAD_UNCERTAIN_KEY) });
    const looper = readJson(LOOPER_KEY) || {};
    // The play permission is never restored from storage: a reload always needs a new confirmation.
    this.store.set({ looper: { loaded: looper.loaded ?? null, armed: false, stopAvailable: looper.stopAvailable === true } });
  }

  get names() {
    return Object.keys(this.registry);
  }

  /** Run a named action. Errors are shown in the UI, never thrown to the caller. */
  async execute(name, ...args) {
    const action = this.registry[name];
    if (!action) {
      this.store.set({ error: `Unknown action: ${name}` });
      return;
    }
    try {
      this.store.set({ error: null, notice: null });
      await action(...args);
    } catch (err) {
      this.store.set({ error: err.message });
    }
  }

  // ---------- connection ----------

  async connect() {
    if (!G6Midi.isSupported()) throw new Error('This browser does not support Web MIDI: use Chrome.');
    this.wantConnected = true;
    this.store.set({ connection: 'connecting' });
    try {
      await this.midi.requestAccess();
    } catch {
      this.store.set({ connection: 'disconnected' });
      throw new Error('MIDI access (with SysEx) was denied in Chrome.');
    }
    if (!this.midi.attach()) {
      this.store.set({ connection: 'disconnected' });
      throw new Error('G6 not found: is it powered on and connected via USB?');
    }
    try {
      await this.midi.run(async () => {
        const name = await this.#setupLiveSession();
        const slot = await this.#readActiveSlot();
        this.store.set({ connection: 'connected', slot, patchName: name });
        await this.#readRhythmState();
      });
    } catch (err) {
      this.store.set({ connection: 'disconnected' });
      throw err;
    }
  }

  #onPorts(present) {
    if (!present) {
      this.store.set({ connection: 'disconnected', error: 'G6 disconnected.' });
    } else if (this.wantConnected && this.store.get().connection === 'disconnected') {
      sleep(500).then(() => this.execute('connect'));
    }
  }

  /** 50 → 53 → 64 13 → 00 00: editor mode, out of PC Mode, live commands enabled. Returns the active patch name. */
  async #setupLiveSession() {
    const ok = await this.midi.request(P.MSG.editorMode, P.isOk, 1000);
    if (!ok) throw new Error('The G6 does not respond: is it powered on and connected via USB?');
    this.midi.sendSysex(P.MSG.exitPcMode);
    await sleep(100);
    return this.#readActivePatchName();
  }

  // ---------- patch ----------

  async #readActivePatchName() {
    const dump = await this.midi.request(P.MSG.requestActivePatch, P.isActivePatchDump, 1500);
    this.midi.sendSysex(P.MSG.hostAck);
    return dump ? P.parseActivePatchName(dump) : null;
  }

  async #readActiveSlot() {
    let bank = null;
    const programChange = this.midi.waitFor((bytes) => {
      const status = bytes[0] & 0xf0;
      if (status === 0xb0 && bytes[1] === 32) bank = bytes[2];
      return status === 0xc0;
    }, 1000);
    this.midi.sendSysex(P.MSG.requestActiveSlot);
    const pc = await programChange;
    return pc && bank !== null ? P.bankPosToSlot(bank, pc[1]) : null;
  }

  async #refreshPatch() {
    const slot = await this.#readActiveSlot();
    const name = await this.#readActivePatchName();
    this.store.set({ slot, patchName: name ?? '?' });
  }

  async #selectSlot(slot) {
    slot = Number(slot);
    if (!P.isValidSlot(slot)) throw new Error(`Slot out of range (${P.SLOT_MIN}-${P.SLOT_MAX}).`);
    for (const message of P.patchSelectMessages(slot)) this.midi.sendChannel(message);
    await sleep(400);
    const name = await this.#readActivePatchName();
    this.store.set({ slot, patchName: name ?? '?' });
  }

  async #step(delta) {
    const slot = (await this.#readActiveSlot()) ?? this.store.get().slot;
    if (!slot) throw new Error('Cannot read the active slot.');
    const target = ((slot - 1 + delta + P.SLOT_MAX) % P.SLOT_MAX) + 1;
    await this.#selectSlot(target);
  }

  /** Read-only: enters PC Mode, reads the 240 names, leaves PC Mode, restores the live session. */
  async #loadPatchList() {
    const list = {};
    this.inPcMode = true;
    this.store.set({ connection: 'busy', listProgress: { done: 0, total: P.SLOT_MAX } });
    try {
      if (!(await this.midi.request(P.MSG.enterPcMode, P.isOk, 1000))) throw new Error('The G6 did not enter PC Mode.');
      if (!(await this.midi.request(P.MSG.editorMode, P.isOk, 1000))) throw new Error('The G6 does not respond.');
      for (let slot = P.SLOT_MIN; slot <= P.SLOT_MAX; slot++) {
        const dump = await this.midi.request(P.readStoredPatchMsg(slot), (b) => P.isStoredPatchDump(b, slot), 1000);
        if (!dump) throw new Error(`No reply while reading slot ${slot}.`);
        list[slot] = P.parseStoredPatchName(dump);
        if (slot % 8 === 0) this.store.set({ listProgress: { done: slot, total: P.SLOT_MAX } });
      }
    } finally {
      this.midi.sendSysex(P.MSG.exitPcMode);
      await sleep(100);
      this.inPcMode = false;
      this.store.set({ listProgress: null });
      await this.#setupLiveSession().catch(() => {});
      this.store.set({ connection: this.midi.attached ? 'connected' : 'disconnected' });
    }
    savePatchList(list);
    this.store.set({ patchList: list });
  }

  // ---------- rhythm ----------

  /** 64 0A → 64 09: BPM, pattern, volume and COUNT as stored on the G6. */
  async #readRhythmState() {
    const frame = await this.midi.requestFrame(P.MSG.requestRhythmState, P.isRhythmStateFrame, 1000);
    if (!frame) throw new Error('No reply to the rhythm state request.');
    const { bpm, pattern, volume, count } = P.parseRhythmState(frame);
    this.#updateRhythm({ bpm, pattern, volume, count, readAt: Date.now() });
    this.store.set({ looperSettings: P.parseLooperSettings(frame) });
  }

  async #transport(command) {
    this.midi.sendSysex(command === 'start' ? P.MSG.rhythmStart : P.MSG.rhythmStop);
    this.#updateRhythm({ lastCommand: command });
  }

  async #nudgeBpm(delta) {
    const { bpm } = this.store.get().rhythm;
    if (bpm === null) throw new Error('BPM unknown: refresh the rhythm state first.');
    await this.#setRhythm('bpm', Math.min(P.BPM_MAX, Math.max(P.BPM_MIN, bpm + delta)));
  }

  /** Send one setter, wait for its confirmation, then read the state back. Never resends. */
  async #setRhythm(name, value) {
    value = Math.round(Number(value));
    if (!P.isValidRhythmValue(name, value)) {
      const { min, max } = P.RHYTHM_PARAMS[name];
      throw new Error(`${name} must be between ${min} and ${max}.`);
    }
    const confirmed = await this.midi.request(
      P.setRhythmParamMsg(name, value),
      (b) => P.isRhythmConfirmation(b, name),
      CONFIRM_TIMEOUT_MS,
    );
    if (!confirmed) this.store.set({ notice: `${name} = ${value}: not confirmed by the G6 (not resent).` });
    await this.#readRhythmState();
  }

  // ---------- SD card and WAV files (read-only) ----------

  /**
   * SD space, then WAV count, then the names in ranges of at most 50 (count first: the list needs it).
   * Each request is sent once; the first failure stops the sequence and nothing is retried.
   */
  async #readStorage() {
    const sdFrame = await this.midi.requestFrame(P.MSG.requestSdSpace, P.isSdSpaceFrame, 1500);
    if (!sdFrame) throw new Error('No reply to the SD space request.');
    const { totalBytes, freeBytes } = P.parseSdSpace(sdFrame);

    const countFrame = await this.midi.requestFrame(P.MSG.requestWavCount, P.isWavCountFrame, 1500);
    if (!countFrame) throw new Error('No reply to the WAV count request.');
    const wavCount = P.parseWavCount(countFrame);

    const wavNames = [];
    for (let start = 0; start < wavCount; start += P.WAV_LIST_MAX_RECORDS) {
      const end = Math.min(start + P.WAV_LIST_MAX_RECORDS, wavCount) - 1;
      const listFrame = await this.midi.requestFrame(P.wavListMsg(start, end, wavCount), P.isWavListFrame, 3000);
      if (!listFrame) throw new Error(`No reply to the WAV list request (${start}-${end}).`);
      wavNames.push(...P.parseWavList(listFrame, start, end));
    }

    this.#updateStorage({ totalBytes, freeBytes, wavCount, wavNames, readAt: Date.now(), stale: false });
  }

  /**
   * WAV load by file name. Requires the user's explicit confirmation that the Looper is stopped and holds nothing
   * to keep. Re-reads the count and the whole list, resolves the name (missing or ambiguous: nothing sent), checks
   * the entry once more, then sends 64 39 once.
   *
   * The generic reply 00 RR has no opcode, index or transaction id, so it is never reported as the certain result of
   * this load. With no reply in time, or more than one reply, the outcome is uncertain: further loads are blocked
   * (also across page reloads) until the user checks the Looper screen and clears the state. Waiting or draining
   * does not protect against a reply that arrives later.
   */
  async #loadWav(name, looperStoppedConfirmed) {
    const pending = this.store.get().storage.loadUncertain;
    if (pending) {
      throw new Error(
        `The previous WAV load ("${pending.name}") has an uncertain outcome (${pending.reason}). ` +
          'Check the Looper screen, then press "I checked the Looper" before loading again.',
      );
    }
    if (looperStoppedConfirmed !== true) throw new Error('Confirm that the Looper is stopped and holds nothing to keep.');
    await this.#readStorage(); // fresh count and names; throws on any inconsistency
    const { wavNames } = this.store.get().storage;
    const index = P.findWavIndex(wavNames, name);
    const count = wavNames.length;
    const frame = await this.midi.requestFrame(P.wavListMsg(index, index, count), P.isWavListFrame, 3000);
    if (!frame) throw new Error('No reply when re-reading the selected entry: not loaded.');
    const again = P.parseWavList(frame, index, index)[0];
    if (again.toLowerCase() !== wavNames[index].toLowerCase()) {
      throw new Error(`Index ${index} now reads "${again}": the list changed, not loaded.`);
    }
    const wanted = wavNames[index];
    const bpmBefore = this.store.get().rhythm.bpm;

    // Recorded before sending: if anything fails from here on, the outcome stays uncertain.
    this.#setUncertainLoad({ name: wanted, index, reason: 'sent, outcome not evaluated', at: new Date().toISOString() });
    this.#setLooper({ loaded: null, armed: false, stopAvailable: false });
    const bodies = [];
    const collect = (event) => event.detail.body && bodies.push(event.detail.body);
    this.midi.addEventListener('message', collect);
    try {
      const reply = await this.midi.request(
        P.wavLoadMsg(index, count),
        (b) => P.ackResult(b) !== null,
        this.wavLoadTimeoutMs,
      );
      if (reply) await sleep(this.wavLoadLingerMs);
    } finally {
      this.midi.removeEventListener('message', collect);
    }
    await this.#readRhythmState().catch(() => {});

    const results = bodies.map(P.ackResult).filter((r) => r !== null);
    const announced = bodies.map(P.parseLooperFileEvent).filter((n) => n !== null && n !== 'New Loop File');
    const other = announced.filter((n) => n.toLowerCase() !== wanted.toLowerCase());
    const hex = (r) => `0x${r.toString(16).padStart(2, '0').toUpperCase()}`;
    const seconds = this.wavLoadTimeoutMs / 1000;
    const parts = [`WAV load sent once for "${wanted}" (index ${index}).`];
    let uncertain = null;
    if (!results.length) {
      uncertain = `no reply within ${seconds} s`;
      parts.push(
        `No generic reply within ${seconds} s: not resent. The load may still be running or may have happened; a reply can arrive later.`,
      );
    } else if (results.length > 1) {
      uncertain = `${results.length} generic replies`;
      parts.push(
        `${results.length} generic replies arrived (${results.map(hex).join(', ')}): at least one belongs to something else. Outcome uncertain.`,
      );
    } else {
      parts.push(
        `One generic reply: ${hex(results[0])} (${P.WAV_LOAD_RESULTS[results[0]] ?? 'undocumented code'}). It cannot be tied to this load with certainty and is not a proof.`,
      );
    }
    if (other.length) parts.push(`WARNING: the G6 announced a different file, "${other.at(-1)}". Check before playing.`);
    else if (announced.length) parts.push(`Announced: "${announced.at(-1)}".`);
    else parts.push('No file name announced by the G6.');
    const bpmAfter = this.store.get().rhythm.bpm;
    if (bpmBefore !== null && bpmAfter !== null && bpmBefore !== bpmAfter) parts.push(`BPM ${bpmBefore} → ${bpmAfter}.`);
    if (uncertain) {
      this.#setUncertainLoad({ name: wanted, index, reason: uncertain, at: new Date().toISOString() });
      parts.push('Further loads are blocked until you check the Looper screen and confirm.');
    } else {
      this.#setUncertainLoad(null);
      this.#setLooper({ loaded: { name: wanted, at: Date.now() }, armed: false, stopAvailable: false });
    }
    parts.push('Confirm on the Looper screen.');
    this.#updateStorage({ stale: true, lastLoad: { name: wanted, at: Date.now() } });
    this.store.set({ notice: parts.join(' ') });
  }

  /** No MIDI. Allowed only after the user has looked at the Looper screen. */
  #clearUncertainLoad(looperCheckedConfirmed) {
    const pending = this.store.get().storage.loadUncertain;
    if (!pending) return;
    if (looperCheckedConfirmed !== true) throw new Error('Check the Looper screen first.');
    this.#setUncertainLoad(null);
    this.store.set({
      notice: `Loads allowed again after checking "${pending.name}". A late reply to that load can still arrive and cannot be told apart from a new one.`,
    });
  }

  // ---------- Looper play / stop (gated) ----------
  // Block 5C, parameter 0 is the Looper's main foot-switch gesture: it plays a loaded, stopped loop, but can record
  // (empty Looper) or overdub (while playing). Play needs a WAV load by this page with a non-uncertain outcome and a
  // confirmation on the display; the permission is used up before sending, and a Looper event from the device
  // withdraws it. Stop is available only after a play sent by this page.

  #armPlay(confirmed) {
    if (confirmed !== true) {
      this.#setLooper({ armed: false });
      return;
    }
    if (this.store.get().storage.loadUncertain) throw new Error('The last WAV load has an uncertain outcome.');
    if (!this.store.get().looper.loaded) throw new Error('Load a WAV first: play needs a load from this page.');
    this.#setLooper({ armed: true, stopAvailable: false });
  }

  async #looperPlay() {
    const { looper } = this.store.get();
    if (!looper.armed) throw new Error('Play is not allowed: confirm on the display that the WAV is loaded and the Looper stopped.');
    this.#setLooper({ armed: false, stopAvailable: true }); // used up before sending
    const events = await this.#sendLooperGesture(P.MSG.looperPlay);
    this.store.set({
      notice: `Play sent once (Looper with "${looper.loaded?.name ?? '?'}"). ${events} Play is disabled until a new confirmation; Stop is available. Check the display and listen.`,
    });
  }

  async #looperStop() {
    if (!this.store.get().looper.stopAvailable) throw new Error('Stop is available only after a play sent by this page.');
    this.#setLooper({ stopAvailable: false }); // one send
    const events = await this.#sendLooperGesture(P.MSG.looperStop);
    this.store.set({
      notice: `Stop sent once. ${events} If the loop is still playing, stop it from the foot switch (not resent).`,
    });
  }

  /** One send, then listen briefly for Looper notifications. Nothing is waited for or resent. */
  async #sendLooperGesture(body) {
    const names = [];
    const collect = (event) => {
      const name = P.parseLooperFileEvent(event.detail.body);
      if (name !== null) names.push(name);
    };
    this.midi.addEventListener('message', collect);
    try {
      this.midi.sendSysex(body);
      await sleep(this.looperListenMs);
    } finally {
      this.midi.removeEventListener('message', collect);
    }
    return names.length ? `Looper notification: ${names.join(', ')}.` : 'No Looper notification.';
  }

  /** STOP MODE 0/1/2: one setter, wait for its confirmation, then read the settings back. A setting, not a stop. */
  async #setStopMode(mode) {
    mode = Number(mode);
    if (!Number.isInteger(mode) || !(mode in P.STOP_MODES)) throw new Error('STOP MODE must be 0, 1 or 2.');
    if (this.store.get().looper.stopAvailable) {
      throw new Error('Stop the play sent by this page first (Stop or the foot switch), then change STOP MODE.');
    }
    const confirmed = await this.midi.request(
      P.setStopModeMsg(mode),
      (b) => P.parseStopModeChange(b)?.confirmed === true,
      CONFIRM_TIMEOUT_MS,
    );
    if (!confirmed) this.store.set({ notice: `STOP MODE ${P.STOP_MODES[mode]}: not confirmed by the G6 (not resent).` });
    await this.#readRhythmState();
  }

  /** Looper volume 0-100: one setter, wait for its confirmation, then read the settings back. */
  async #setLooperVolume(volume) {
    volume = Math.round(Number(volume));
    if (!Number.isInteger(volume) || volume < 0 || volume > P.LOOPER_VOLUME_MAX) {
      throw new Error(`Looper volume must be between 0 and ${P.LOOPER_VOLUME_MAX}.`);
    }
    const confirmed = await this.midi.request(
      P.setLooperVolumeMsg(volume),
      (b) => P.parseLooperVolumeChange(b)?.confirmed === true,
      CONFIRM_TIMEOUT_MS,
    );
    if (!confirmed) this.store.set({ notice: `Looper volume ${volume}: not confirmed by the G6 (not resent).` });
    await this.#readRhythmState();
  }

  /**
   * POSITION 0/1: read the current value (nothing is sent if it already matches), then one setter, wait for its
   * confirmation and read back. The setter stops and resets the Looper, so the loaded WAV and Play are dropped first.
   */
  async #setPosition(position, resetConfirmed) {
    position = Number(position);
    if (!Number.isInteger(position) || !(position in P.LOOPER_POSITIONS)) throw new Error('POSITION must be 0 or 1.');
    if (this.store.get().looper.stopAvailable) {
      throw new Error('Stop the play sent by this page first (Stop or the foot switch), then change POSITION.');
    }
    if (resetConfirmed !== true) {
      throw new Error('Changing POSITION stops and resets the Looper: tick the confirmation first.');
    }
    await this.#readRhythmState();
    const wanted = P.LOOPER_POSITIONS[position];
    if (this.store.get().looperSettings.position === position) {
      this.store.set({ notice: `POSITION is already ${wanted}: nothing sent.` });
      return;
    }
    this.#setLooper({ loaded: null, armed: false, stopAvailable: false });
    const confirmed = await this.midi.request(
      P.setPositionMsg(position),
      (b) => P.parsePositionChange(b)?.confirmed === true,
      CONFIRM_TIMEOUT_MS,
    );
    this.store.set({
      notice: confirmed
        ? `POSITION ${wanted}: confirmed. The Looper was reset: load a WAV again before a play.`
        : `POSITION ${wanted}: not confirmed by the G6 (not resent). The Looper may have been reset.`,
    });
    await this.#readRhythmState();
  }

  #setLooper(patch) {
    const looper = { ...this.store.get().looper, ...patch };
    writeJson(LOOPER_KEY, { loaded: looper.loaded, stopAvailable: looper.stopAvailable });
    this.store.set({ looper });
  }

  #setUncertainLoad(state) {
    writeJson(WAV_LOAD_UNCERTAIN_KEY, state);
    this.#updateStorage({ loadUncertain: state });
  }

  #updateStorage(patch) {
    this.store.set({ storage: { ...this.store.get().storage, ...patch } });
  }

  #updateRhythm(patch) {
    this.store.set({ rhythm: { ...this.store.get().rhythm, ...patch } });
  }

  // ---------- notifications from the G6 ----------

  #onDeviceMessage(body) {
    if (!body || this.inPcMode) return;
    // A Looper event while play is allowed (e.g. a foot switch): the confirmed state may no longer hold.
    if (P.parseLooperFileEvent(body) !== null && this.store.get().looper.armed) {
      this.#setLooper({ armed: false });
      this.store.set({ notice: 'Looper event from the device: play permission withdrawn. Check the display and confirm again.' });
      return;
    }
    const slot = P.parsePatchChanged(body);
    if (slot !== null) {
      this.store.set({ slot, patchName: '…' });
      this.midi
        .run(async () => {
          await sleep(50);
          if (this.store.get().slot !== slot) return;
          const name = await this.#readActivePatchName();
          this.store.set({ patchName: name ?? '?' });
        })
        .catch((err) => this.store.set({ error: err.message }));
      return;
    }
    const looperVolume = P.parseLooperVolumeChange(body);
    if (looperVolume && looperVolume.value <= P.LOOPER_VOLUME_MAX) {
      const current = this.store.get().looperSettings;
      if (current) this.store.set({ looperSettings: { ...current, volume: looperVolume.value } });
      return;
    }
    const position = P.parsePositionChange(body);
    if (position && position.value in P.LOOPER_POSITIONS) {
      // Another client (e.g. the MCP server) changed POSITION: the Looper was reset.
      const looper = this.store.get().looper;
      if (looper.loaded || looper.armed) this.#setLooper({ loaded: null, armed: false });
      const current = this.store.get().looperSettings;
      if (current) {
        const name = P.LOOPER_POSITIONS[position.value];
        this.store.set({ looperSettings: { ...current, position: position.value, positionName: name } });
      }
      return;
    }
    const stopMode = P.parseStopModeChange(body);
    if (stopMode && stopMode.value in P.STOP_MODES) {
      const current = this.store.get().looperSettings;
      if (current) {
        const name = P.STOP_MODES[stopMode.value];
        this.store.set({ looperSettings: { ...current, stopMode: stopMode.value, stopModeName: name } });
      }
      return;
    }
    // Confirmations also arrive when another client (e.g. the MCP server) changes a rhythm parameter.
    const change = P.parseRhythmParam(body);
    if (change && P.isValidRhythmValue(change.name, change.value)) this.#updateRhythm({ [change.name]: change.value });
  }

  // ---------- helpers ----------

  #queued(task) {
    const { connection } = this.store.get();
    if (connection !== 'connected' && connection !== 'busy') {
      return Promise.reject(new Error('G6 not connected: press "Connect".'));
    }
    return this.midi.run(task);
  }
}

function loadPatchList() {
  try {
    return JSON.parse(localStorage.getItem(PATCH_LIST_KEY)) || {};
  } catch {
    return {};
  }
}

function savePatchList(list) {
  try {
    localStorage.setItem(PATCH_LIST_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable: the list stays in memory only */
  }
}

function readJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key)) || null;
  } catch {
    return null;
  }
}

function writeJson(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the state holds for this page only */
  }
}
