import { Unzip, UnzipInflate } from "fflate";

// The most a DOCX or EPUB may inflate to, summed across the entries read out
// of it. A zip's headers state each entry's size, but a zip bomb states
// whatever it likes, so the cap counts the bytes the inflater actually
// produces.
export const INFLATE_CAP = 32 * 1024 * 1024;

// Which entries to inflate, by exact name, and how many bytes earlier passes
// over the same archive already inflated, so an archive read in several
// passes shares one cap rather than getting a fresh one each pass.
// Everything else is skipped without being inflated.
export type Want = { names: string[]; inflated?: number };

// On success, `inflated` is the running total for the next pass to carry.
export type Unzipped =
  | { ok: true; files: Map<string, string>; inflated: number }
  | { ok: false; why: "too-big" | "damaged" };

// Compressed bytes are fed to the inflater this many at a time. DEFLATE
// expands at most about 1,032 to 1, so one slice can add at most a few
// megabytes before the running total is checked against the cap.
const SLICE = 4 * 1024;

// Reads the wanted entries of a zip as text, each decoded the way its own
// bytes say (`decode`); a zip with no entries at all is damaged. Synchronous
// and CPU-bound, so the app calls it through `unzip`, which runs it in a
// worker; tests call it directly.
export const unzipEntries = (bytes: Uint8Array, want: Want): Unzipped => {
  let entries = 0;
  const chunks = new Map<string, Uint8Array[]>();
  let inflated = want.inflated ?? 0;
  let failed: Error | null = null;

  const wanted = (name: string) => want.names.includes(name);

  const unzipper = new Unzip((file) => {
    entries += 1;
    if (!wanted(file.name)) return;
    const parts: Uint8Array[] = [];
    chunks.set(file.name, parts);
    file.ondata = (error, data) => {
      if (error) failed = error;
      else {
        inflated += data.length;
        if (inflated <= INFLATE_CAP) parts.push(data);
      }
    };
    file.start();
  });
  unzipper.register(UnzipInflate);

  try {
    for (let offset = 0; offset < bytes.length; offset += SLICE) {
      unzipper.push(bytes.subarray(offset, offset + SLICE));
      if (inflated > INFLATE_CAP) return { ok: false, why: "too-big" };
      if (failed) return { ok: false, why: "damaged" };
    }
    unzipper.push(new Uint8Array(0), true);
  } catch {
    return { ok: false, why: "damaged" };
  }
  if (inflated > INFLATE_CAP) return { ok: false, why: "too-big" };
  if (failed || entries === 0) return { ok: false, why: "damaged" };

  const files = new Map([...chunks].map(([name, parts]) => [name, decode(concat(parts))] as const));
  return { ok: true, files, inflated };
};

// An entry's bytes as text, told apart the way XML 1.0 (Appendix F) says: a
// byte order mark first, then the byte pattern of a UTF-16 `<?` with no mark,
// then the encoding the declaration names when the platform knows the label.
// Anything else, a non-XML entry included, is UTF-8.
const decode = (bytes: Uint8Array) => {
  const [a, b, c, d] = bytes;
  const label =
    a === 0xef && b === 0xbb && c === 0xbf ? "utf-8"
    : a === 0xff && b === 0xfe ? "utf-16le"
    : a === 0xfe && b === 0xff ? "utf-16be"
    : a === 0x3c && b === 0 && c === 0x3f && d === 0 ? "utf-16le"
    : a === 0 && b === 0x3c && c === 0 && d === 0x3f ? "utf-16be"
    : (/^<\?xml[^>]*encoding=["']([A-Za-z][\w.-]*)["']/.exec(String.fromCharCode(...bytes.subarray(0, 128)))?.[1] ??
      "utf-8");
  try {
    return new TextDecoder(label).decode(bytes);
  } catch {
    return new TextDecoder().decode(bytes);
  }
};

const concat = (parts: Uint8Array[]) => {
  const whole = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let at = 0;
  for (const part of parts) {
    whole.set(part, at);
    at += part.length;
  }
  return whole;
};

// `unzipEntries` in a worker of its own, so a hostile archive burns a
// background thread rather than freezing the window. One worker per call,
// ended when it answers. The bytes are transferred, not copied.
export const unzip = (bytes: Uint8Array, want: Want) =>
  new Promise<Unzipped>((resolve) => {
    const worker = new Worker(new URL("./unzip.worker.ts", import.meta.url), { type: "module" });
    const finish = (result: Unzipped) => {
      worker.terminate();
      resolve(result);
    };
    worker.onmessage = (event: MessageEvent<Unzipped>) => finish(event.data);
    worker.onerror = () => finish({ ok: false, why: "damaged" });
    worker.postMessage({ bytes, want }, [bytes.buffer]);
  });
