import { useState } from "react";
import type { TakeAction } from "../engine/advanced";
import type { EngineClient, NarrationState } from "../engine/client";

export default function AdvancedDiagnostics({ client, narration, supportModels, onTakeSelected }: {
  client: Pick<EngineClient, "selectTake">;
  narration: NarrationState | null;
  supportModels: { id: string; name: string }[];
  // A take the Engine accepted has replaced this Block's audio, so the
  // Read-along's offsets are stale until its detail is read again.
  onTakeSelected: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const report = narration?.diagnostics;
  const block = report?.playingBlock;
  const take = async (action: TakeAction) => {
    if (!block || !narration?.narrationId) return;
    setBusy(true);
    setNotice("");
    try {
      await client.selectTake(narration.narrationId, block.ordinal, action);
      onTakeSelected();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "That take could not be played.");
    } finally {
      setBusy(false);
    }
  };
  return <section className="advanced-diagnostics" aria-label="Diagnostics">
    <h2>Diagnostics</h2>
    {!report ? <p>Start a Narration to see measurements.</p> : <>
      {narration?.generationBehind && <p role="status">Generation is behind playback. Turn “Prepare the whole Narration first” back on in Controls, then start a new Narration.</p>}
      <dl className="advanced-diagnostics__metrics">
        <div><dt>Generation throughput</dt><dd>{report.audioSecondsPerSecond === null ? "Waiting for generation" : `${report.audioSecondsPerSecond.toFixed(2)} seconds audio / second`}</dd></div>
        <div><dt>Ready ahead</dt><dd>{report.readySecondsAhead.toFixed(1)} seconds at {narration?.speed}×</dd></div>
        <div><dt>Preparing</dt><dd>{report.preparingBlock === null ? "Nothing" : `Block ${report.preparingBlock + 1}`}</dd></div>
        <div><dt>Retries / cutoffs</dt><dd>{report.retries} / {report.cutoffs}</dd></div>
        <div><dt>Ring starvation callbacks</dt><dd>{report.ringStarvations}</dd></div>
        <div><dt>Device underflows</dt><dd>{report.deviceUnderflows}</dd></div>
      </dl>
      <p className="setting__note">Generation measurements cover this playback run. Audio-device counters cover this Engine session.</p>
      {block && <div className="advanced-diagnostics__block">
        <h3>Block {block.ordinal + 1} · Take {block.take}</h3>
        <p>Word timing: {block.wordTiming}{block.supportModel && ` · ${supportModels.find((model) => model.id === block.supportModel)?.name ?? block.supportModel}`}</p>
        <p>{block.cacheHit ? "Stored Segment" : "Newly generated Segment"} · Seed {block.seed}</p>
        <p className="advanced-diagnostics__hash">Generation Record: <code>{block.recordHash}</code></p>
        <div className="advanced-controls__actions">
          <button disabled={busy} onClick={() => { void take("reroll"); }}>Regenerate this Block</button>
          {block.hasComparison && <>
            <button disabled={busy} onClick={() => { void take("A"); }}>Play A · stored take</button>
            <button disabled={busy} onClick={() => { void take("B"); }}>Play B · re-roll</button>
          </>}
        </div>
        <p className="setting__note">Regeneration re-rolls this Block’s seed. A/B plays from this Block and keeps the selected take in History.</p>
      </div>}
    </>}
    {notice && <p role="alert">{notice}</p>}
  </section>;
}
