// jsdom 30 parses <dialog> and reflects its `open` attribute but does not
// implement modality: `showModal` and `close` are absent.
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
}

// jsdom has no media stack: `play()` and `pause()` both throw "not
// implemented". Only `pause` is stubbed, so a test that plays has to say so.
if (!("__readilyPause" in HTMLMediaElement.prototype)) {
  Object.defineProperty(HTMLMediaElement.prototype, "__readilyPause", { value: true });
  HTMLMediaElement.prototype.pause = () => {};
}

// Node 22+ defines its own `localStorage` global, which shadows jsdom's and
// reads as `undefined` unless Node is started with `--localstorage-file`.
// The shell's per-model Voice memory lives there, so tests get a Storage
// that holds for one file and is cleared between tests by the suites that
// write to it.
if (globalThis.localStorage === undefined) {
  const items = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return items.size;
    },
    key: (index) => [...items.keys()][index] ?? null,
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, String(value)),
    removeItem: (key) => void items.delete(key),
    clear: () => items.clear(),
  };
  Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true });
}
