// Minimal state container: the UI renders from the state, actions update it.

export function createStore(initial) {
  let state = structuredClone(initial);
  const subscribers = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      for (const fn of subscribers) fn(state);
    },
    subscribe(fn) {
      subscribers.add(fn);
      fn(state);
      return () => subscribers.delete(fn);
    },
  };
}

export const initialState = {
  connection: 'disconnected', // disconnected | connecting | connected | busy
  error: null,
  notice: null, // non-error information, e.g. an unconfirmed setter
  slot: null,
  patchName: null,
  rhythm: {
    bpm: null, // values read from the G6 (64 0A) or confirmed by it; null = unknown
    pattern: null,
    volume: null,
    count: null,
    playing: null, // never verified: no read command for playback state is known
    playingVerified: false,
    lastCommand: null, // 'start' | 'stop': what this app sent last, not the device state
    readAt: null, // time of the last successful 64 0A read
  },
  storage: {
    totalBytes: null, // SD card, from the device (freshness of its file cache unknown)
    freeBytes: null,
    wavCount: null,
    wavNames: [], // device order; indices are not stable identifiers
    readAt: null,
    stale: false, // true after a WAV load: the device may have changed the list
    lastLoad: null, // { name, at }: the last WAV load sent by this page (not a device state)
    loadUncertain: null, // { name, index, reason, at }: a load with no reply or several replies; blocks further loads
  },
  looperSettings: null, // { stopMode, stopModeName, position, positionName, loopMode, loopModeName } from 64 09
  looper: {
    loaded: null, // { name, at }: last WAV loaded by this page with a non-uncertain outcome (not a device state)
    armed: false, // one play allowed after a confirmation on the display; never restored after a reload
    stopAvailable: false, // after a play sent by this page, until one stop is sent
  },
  patchList: {}, // slot -> name, loaded on demand
  listProgress: null, // { done, total } while loading the patch list
};
