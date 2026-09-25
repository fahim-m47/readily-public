import { useCallback, useEffect, useRef, useState } from "react";
import type {
  Connection,
  EngineClient,
  Mode,
  NarrationState,
  VoiceSelection,
} from "./client";

// A screen's live view of the Engine, plus the two things it can ask for.
export type EngineBinding = {
  connection: Connection;
  // The Engine's own snapshot of the active Narration, and `null` both
  // before the first one arrives and whenever the connection is not ready.
  // Every screen reads playback state from here — the shell is a remote
  // control, never a second source of truth, and a remote control for an
  // Engine that is gone reports nothing rather than the last thing it saw.
  narration: NarrationState | null;
  // The last request that failed, as a sentence, and only ever the newest
  // one. Cleared by the next request. Connection failures live on
  // `connection`, not here.
  actionError: string | null;
  // Narrate with `voice`, or with the Engine's own default when the shell
  // has not read a selection yet.
  narrate: (
    source: string,
    voice: VoiceSelection | undefined,
    mode: Mode,
  ) => void;
  // Resolves `true` once the Engine has taken the stop, `false` if the
  // request failed: the one action something else waits on, since the
  // Engine reads one Narration at a time.
  stop: () => Promise<boolean>;
  // The transport, mirrored straight through to the Engine. Nothing here
  // predicts a phase: what a reader sees after pressing Pause is whatever
  // the next `/v1/events` snapshot says, which is the only playhead there is.
  pause: () => void;
  play: () => void;
  seek: (sourceOffset: number) => void;
  seekTime: (positionSec: number) => void;
  // Read a stored Narration again: from its playhead if it was interrupted
  // or stopped, from the top if it finished. `paused` opens it silent.
  // Resolves like `stop` does — `true` once the Engine has taken it — since
  // a seek on a finished Narration waits on this before it can move.
  replay: (
    narrationId: string,
    mode: Mode,
    options?: { paused: boolean },
  ) => Promise<boolean>;
  setSpeed: (speed: number) => void;
  // Start the Engine over after the supervisor gave up. The only action
  // here that is about the connection rather than about a Narration, and
  // the only one worth offering while `connection` is failed.
  retry: () => void;
};

// Binds a screen to one EngineClient for the life of the component.
//
// The single subscription is deliberate: `watch` is a reconnecting loop that
// owns the Engine's port and token, and a second one would open a second SSE
// connection to the same Engine. Every component that needs Engine state
// calls this once, at the top, and passes what it needs down.
export const useEngine = (client: EngineClient): EngineBinding => {
  const [connection, setConnection] = useState<Connection>({
    state: "starting",
    detail: "launching",
  });
  const [narration, setNarration] = useState<NarrationState | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Which request is still the current one. Every request takes the next
  // number, and only the holder of the newest may publish a failure. Two
  // requests are in flight whenever a reader presses twice, and the Engine
  // answers them in whatever order it finishes them — an older rejection
  // landing last would report a refusal the reader has already moved past.
  const latest = useRef(0);

  // An Engine that is not ready is not reporting a Narration, so the last
  // snapshot it sent stops being true the moment the connection leaves
  // `ready`. Dropped here rather than guarded at each reader, because the
  // snapshot is the only thing that says a Narration is playing and the
  // only thing Stop is offered from: keeping a stale one would put "Reading
  // aloud…" and a live Stop in the main pane while the sidebar says the
  // Engine died, and the Stop could only ever answer that the Engine is not
  // ready. Cleared at the source, that pairing cannot be built.
  const observe = useCallback((next: Connection) => {
    setConnection(next);
    if (next.state !== "ready") setNarration(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    void client
      .watch(
        { onConnection: observe, onNarration: setNarration },
        controller.signal,
      )
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        observe({
          state: "failed",
          message:
            error instanceof Error ? error.message : "The Engine disconnected.",
        });
      });

    return () => controller.abort();
  }, [client, observe]);

  // Requests fire from event handlers that must not wait, so the reply is
  // handled here rather than by the caller: it resolves `true` when the
  // Engine took the request and `false` when it failed. The failure lands on
  // `actionError` unless a newer request has since replaced it. A reply that
  // lands after the screen is gone sets state nobody reads, which React
  // treats as a no-op.
  const request = useCallback((action: () => Promise<unknown>) => {
    const ticket = (latest.current += 1);
    setActionError(null);
    return action().then(
      () => true,
      (error: unknown) => {
        if (ticket === latest.current) {
          setActionError(
            error instanceof Error ? error.message : "The Engine request failed.",
          );
        }
        return false;
      },
    );
  }, []);

  const narrate = useCallback(
    (source: string, voice: VoiceSelection | undefined, mode: Mode) =>
      request(() => client.narrate(source, voice, mode)),
    [client, request],
  );
  const stop = useCallback(() => request(() => client.stop()), [client, request]);
  const pause = useCallback(() => request(() => client.pause()), [client, request]);
  const play = useCallback(() => request(() => client.play()), [client, request]);
  const seek = useCallback(
    (sourceOffset: number) => request(() => client.seek(sourceOffset)),
    [client, request],
  );
  const seekTime = useCallback(
    (positionSec: number) => request(() => client.seekTime(positionSec)),
    [client, request],
  );
  const replay = useCallback(
    (narrationId: string, mode: Mode, options?: { paused: boolean }) =>
      request(() => client.resumeNarration(narrationId, mode, options)),
    [client, request],
  );
  const setSpeed = useCallback(
    (speed: number) => request(() => client.setSpeed(speed)),
    [client, request],
  );
  const retry = useCallback(
    () => request(() => client.retryEngine()),
    [client, request],
  );

  return {
    connection,
    narration,
    actionError,
    narrate,
    stop,
    pause,
    play,
    seek,
    seekTime,
    replay,
    setSpeed,
    retry,
  };
};
