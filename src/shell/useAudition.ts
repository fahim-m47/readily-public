import { useCallback, useEffect, useRef, useState } from "react";

export type Audition = {
  // The clip playing right now, by the key the caller passed, or `null`.
  playing: string | null;
  // The clip that would not play, by key. A bundled clip that fails to load
  // is a curation gap, not a reader's problem to solve, so it is said once
  // beside the Voice and never as an alert.
  failed: string | null;
  // Starts `url`, stopping whatever was playing. Called with the key that
  // is already playing, it stops and starts nothing — the same button is
  // Play and Stop, so pressing it twice means "enough".
  toggle: (key: string, url: string) => void;
  // Stops whatever is playing, letting it fade rather than cutting it off.
  stop: () => void;
  // How loud the playing clip is right now, 0–1 on the same decibel scale
  // the Engine reports for a Narration. A getter rather than state: it
  // moves every frame, and only the orb's own render loop needs to see it.
  level: () => number;
};

// Where a clip's RMS reads as silence, matching the Engine's `level`.
const LEVEL_FLOOR_DB = -50;

// How long a stopped clip takes to reach silence. Pausing mid-waveform steps
// the output straight to zero, which is the click the clips themselves are
// dressed to avoid (`dress_for_audition` in `curation/capture.py`); 40ms is
// short enough that Stop still lands on the press and long enough that
// nothing steps. The ramp is a GainNode's own, so it runs at audio rate
// however busy the main thread is.
const STOP_FADE_SECONDS = 0.04;

// The Engine's `loudness()` on this side of the wire: RMS on a 0-1 decibel
// scale, silence at `LEVEL_FLOOR_DB`, so both orbs read the same scale.
export const loudness = (rms: number) =>
  rms <= 0 ? 0 : Math.min(1, Math.max(0, 1 - (20 * Math.log10(rms)) / LEVEL_FLOOR_DB));

// Taps a clip's audio into a shared AnalyserNode so its loudness can be read
// while it plays, giving each clip a GainNode of its own so that one clip
// can be faded out while the next is already playing: every clip is heard as
// source → its gain → the analyser → the output. Made once, lazily, on the
// first clip: an AudioContext is only allowed to start under a user gesture,
// and pressing Hear is one. Returns null where Web Audio is unavailable; the
// orb then breathes on its own.
const createTap = () => {
  try {
    const context = new AudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    analyser.connect(context.destination);
    const samples = new Float32Array(analyser.fftSize);
    return {
      // Routes `element` into the graph through nodes of its own, and hands
      // back the two calls that clip needs. Nothing here reaches another
      // clip's nodes, so starting a Voice cannot cut one still fading.
      attach(element: HTMLAudioElement) {
        const source = context.createMediaElementSource(element);
        const gain = context.createGain();
        source.connect(gain);
        gain.connect(analyser);
        // Unconditional: a `suspend()` from the last clip's fade may still
        // be in flight, and the context reads as "running" until it lands.
        void context.resume();
        return {
          // Ramps this clip alone down to silence over `STOP_FADE_SECONDS`.
          // The hold anchors the ramp at the gain it is being heard at; a
          // ramp with nothing before it starts from the beginning of time.
          fade() {
            const now = context.currentTime;
            gain.gain.setValueAtTime(gain.gain.value, now);
            gain.gain.linearRampToValueAtTime(0, now + STOP_FADE_SECONDS);
          },
          // Takes this clip back out of the graph once it has gone quiet.
          release() {
            source.disconnect();
            gain.disconnect();
          },
        };
      },
      level() {
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const sample of samples) sum += sample * sample;
        return loudness(Math.sqrt(sum / samples.length));
      },
      // Lets go of the output device between clips; `attach` resumes it.
      rest() {
        if (context.state === "running") void context.suspend();
      },
      close() {
        void context.close().catch(() => undefined);
      },
    };
  } catch {
    return null;
  }
};

type Attachment = ReturnType<NonNullable<ReturnType<typeof createTap>>["attach"]>;

// Fades a clip out, then pauses it and takes it out of the graph, calling
// `done` at that point. The element and its nodes are already off the hook's
// refs by the time this runs, so the fade holds its own references and the
// clip started in the meantime is left alone.
//
// An untapped clip is paused outright: it is heard straight from the
// element, with no graph to ramp, and that only happens where Web Audio
// refused us.
const fadeOut = (
  element: HTMLAudioElement,
  attachment: Attachment | null,
  done: () => void,
) => {
  const end = () => {
    element.pause();
    attachment?.release();
    done();
  };
  if (attachment === null) {
    end();
    return;
  }
  attachment.fade();
  window.setTimeout(end, STOP_FADE_SECONDS * 1000);
};

// Plays the bundled Voice Preview clips, one at a time.
//
// One at a time is the whole state machine: two Voices talking over each
// other is not an audition. The element is created per clip rather than
// kept and re-pointed, because a stopped element that is then re-`src`ed
// can fire a stale `ended` for the clip it used to hold.
//
// The clips are app resources loaded from the webview's own origin (see
// `catalog.ts`), so nothing here reaches the Engine or the network — which
// is what makes auditioning work offline, before any model exists on disk.
export const useAudition = (): Audition => {
  const [playing, setPlaying] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  // `undefined` until the first clip; `null` once Web Audio has refused, so
  // a refusal is not retried on every press.
  const tap = useRef<ReturnType<typeof createTap> | undefined>(undefined);
  // The playing clip's own nodes, or null when it is heard around the tap.
  const attached = useRef<Attachment | null>(null);
  // How many clips are still fading. Counted because the output device is
  // let go by the last one to go quiet, and only if nothing has started in
  // the meantime — a fade left over from the previous Voice must not
  // suspend the context out from under the one now playing.
  const fading = useRef(0);

  const stop = useCallback(() => {
    const element = audio.current;
    const attachment = attached.current;
    audio.current = null;
    attached.current = null;
    setPlaying(null);
    if (element === null) return;
    fading.current += 1;
    fadeOut(element, attachment, () => {
      fading.current -= 1;
      if (fading.current === 0 && audio.current === null) tap.current?.rest();
    });
  }, []);

  // A sheet that closes mid-clip must not leave a Voice talking to an empty
  // room, nor an AudioContext holding the output device. The fade `stop`
  // starts is left to run and the context is closed behind it: closing it
  // now would cut the graph mid-waveform, which is the click the fade is
  // there to avoid. Both timers outlive the hook by those 40ms, and by then
  // they can only reach the clip they were made for — `tap` is already let
  // go, so the fade's own ending finds nothing to suspend.
  useEffect(
    () => () => {
      stop();
      const closing = tap.current;
      tap.current = undefined;
      window.setTimeout(() => closing?.close(), STOP_FADE_SECONDS * 1000);
    },
    [stop],
  );

  const toggle = useCallback(
    (key: string, url: string) => {
      const wasPlaying = playing === key;
      stop();
      if (wasPlaying) return;

      setFailed(null);
      const element = new Audio(url);
      element.addEventListener("ended", () => {
        // Guarded because a clip that ends after another has started must
        // not clear the new one's state.
        if (audio.current === element) stop();
      });
      element.addEventListener("error", () => {
        if (audio.current !== element) return;
        stop();
        setFailed(key);
      });
      audio.current = element;
      // Once an element is routed through the tap it is heard only through
      // it, so a tap that could not be made, or refuses this element,
      // leaves the element as it was: audible, with the orb breathing on
      // its own.
      if (tap.current === undefined) tap.current = createTap();
      try {
        attached.current = tap.current?.attach(element) ?? null;
      } catch {
        attached.current = null; /* untapped, still audible */
      }
      setPlaying(key);
      // `play()` rejects when the source will not decode, which is the same
      // failure the `error` event reports; both land on the same sentence.
      void element.play().catch(() => {
        if (audio.current !== element) return;
        stop();
        setFailed(key);
      });
    },
    [playing, stop],
  );

  const level = useCallback(
    () => (audio.current === null ? 0 : (tap.current?.level() ?? 0)),
    [],
  );

  return { playing, failed, toggle, stop, level };
};
