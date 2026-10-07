import type { EngineClient } from "../engine/client";
import { budgetChoices, describeUsage, keepAudioChoices } from "./retention";
import { useRetention } from "./useRetention";
import type { VoiceBinding } from "./useVoice";
import Sheet from "./Sheet";

export type SettingsSheetProps = {
  client: EngineClient;
  // The chosen Voice, read once for the whole shell — the same binding the
  // composer's carousel writes. Settings never picks a Voice, and never
  // edits its Controls — those are Advanced's; it only relays the Voice's
  // notice.
  voice: VoiceBinding;
  onClose: () => void;
};

// The Settings sheet: what Readily keeps, what it is keeping it on, and
// where it is kept.
//
// The two retention knobs are ADR 0004 §5's, and neither is offered as a raw
// number — a reader choosing a budget is choosing how much listening to
// keep, and one choosing a keep-audio window is choosing what gets removed.
// The disk usage sits under both on purpose: the Engine evicts before it
// answers, so lowering the budget moves that number while the reader is
// looking at it. That is what makes "deleting visibly frees disk" (ADR 0004
// §6) something they can check rather than something they are told.
export default function SettingsSheet({
  client,
  voice,
  onClose,
}: SettingsSheetProps) {
  const retention = useRetention(client);
  // What the reader just did here outranks a standing complaint carried in
  // from the composer's pill.
  const notice = retention.notice ?? voice.notice;

  const settings = retention.settings;

  return (
    <Sheet title="Settings" onClose={onClose}>
      {/* Mounted even when silent, so its first message is an update
          rather than an arrival. */}
      <p
        className={`sheet__notice${notice ? "" : " sheet__notice--quiet"}`}
        aria-live="polite"
      >
        {notice ?? ""}
      </p>

      {settings === null ? (
        <p className="sheet__empty">
          {retention.failure ?? "Reading your settings…"}
        </p>
      ) : (
        <>
          <div className="setting">
            <label className="setting__label" htmlFor="setting-budget">
              Audio to keep
            </label>
            <select
              className="setting__control"
              id="setting-budget"
              value={settings.segmentBudgetBytes}
              onChange={(event) =>
                retention.save({
                  segmentBudgetBytes: Number(event.target.value),
                })
              }
            >
              {budgetChoices(settings.segmentBudgetBytes).map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </div>

          <div className="setting">
            <label className="setting__label" htmlFor="setting-keep">
              Remove audio after
            </label>
            <select
              className="setting__control"
              id="setting-keep"
              value={settings.keepAudioDays ?? ""}
              onChange={(event) =>
                retention.save({
                  keepAudioDays:
                    event.target.value === "" ? null : Number(event.target.value),
                })
              }
            >
              {keepAudioChoices(settings.keepAudioDays).map((choice) => (
                <option key={choice.label} value={choice.value ?? ""}>
                  {choice.label}
                </option>
              ))}
            </select>
          </div>

          <p className="setting__usage">{describeUsage(settings.diskUsage)}</p>
        </>
      )}

      <div className="setting">
        <button
          className="setting__folder"
          type="button"
          onClick={retention.openAudioFolder}
        >
          Open audio folder
        </button>
      </div>

      <div className="setting">
        <button
          className="setting__folder"
          type="button"
          onClick={retention.openFolder}
        >
          Open data folder
        </button>
      </div>

      <div className="setting">
        <button
          className="setting__folder"
          type="button"
          onClick={retention.revealLogs}
        >
          Show log file
        </button>
      </div>
    </Sheet>
  );
}
