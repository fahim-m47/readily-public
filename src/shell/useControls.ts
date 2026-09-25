import { useCallback, useEffect, useState } from "react";
import type { ControlSettings, Overrides } from "../engine/advanced";
import type { EngineClient, VoiceSelection } from "../engine/client";

// The selected Voice's Overrides, held once for the whole shell.
export type ControlsBinding = {
  // What the Engine last said about this Voice, or `null` while nothing has
  // been read for it yet.
  settings: ControlSettings | null;
  // One sentence about a read that did not arrive, or `null`. Saving has its
  // own outcome and belongs to whoever asked for it.
  failure: string | null;
  // Replace this Voice's Overrides with `overrides` and keep the Engine's
  // answer. Rejects when the Engine refuses, so the surface that asked can
  // say so where the reader is looking.
  save: (overrides: Overrides) => Promise<void>;
};

type Answer = { settings: ControlSettings | null; failure: string | null };

const keyOf = (modelId: string | null, voiceId: string | null) =>
  modelId === null || voiceId === null ? null : `${modelId}/${voiceId}`;

// Binds the selected Voice's Overrides for every surface that edits them.
//
// The Engine keeps one set of Overrides per Voice, and the Controls route
// carries all of them — prepare-first included. So this is the single owner,
// and the Advanced panel is the one surface that writes through it.
//
// Answers are kept per Voice, so a reply that lands after the reader has
// moved on updates the Voice it was about and nothing else.
export const useControls = (
  client: Pick<EngineClient, "controls" | "setControls">,
  voice: VoiceSelection | null,
): ControlsBinding => {
  const [answers, setAnswers] = useState<ReadonlyMap<string, Answer>>(new Map());
  const modelId = voice?.modelId ?? null;
  const voiceId = voice?.voiceId ?? null;
  const wanted = keyOf(modelId, voiceId);

  const answerFor = (tag: string, answer: Answer) =>
    setAnswers((previous) => new Map(previous).set(tag, answer));

  useEffect(() => {
    const tag = keyOf(modelId, voiceId);
    if (tag === null || modelId === null || voiceId === null) return;
    let live = true;

    void client.controls({ modelId, voiceId }).then(
      (settings) => {
        if (live) answerFor(tag, { settings, failure: null });
      },
      () => {
        if (live) {
          answerFor(tag, {
            settings: null,
            failure: "Controls could not be read. Select a different Voice and come back to try again.",
          });
        }
      },
    );

    return () => {
      live = false;
    };
  }, [client, modelId, voiceId]);

  const save = useCallback(
    async (overrides: Overrides) => {
      const tag = keyOf(modelId, voiceId);
      if (tag === null || modelId === null || voiceId === null) return;
      const settings = await client.setControls({ modelId, voiceId }, overrides);
      answerFor(tag, { settings, failure: null });
    },
    [client, modelId, voiceId],
  );

  const mine = wanted === null ? undefined : answers.get(wanted);
  return { settings: mine?.settings ?? null, failure: mine?.failure ?? null, save };
};
