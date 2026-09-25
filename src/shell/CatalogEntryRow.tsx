import { Play, Square } from "lucide-react";
import { useState } from "react";
import type { CatalogEntry, DownloadState, ModelStatus } from "../engine/client";
import LicenceView from "./LicenceView";
import type { Audition } from "./useAudition";
import {
  describeDetail,
  describeRowDownload,
  describeVoices,
  previewUrl,
} from "./catalog";
import { formatBytes } from "./estimate";

export type CatalogEntryRowProps = {
  entry: CatalogEntry;
  // The model store's answer for this entry, or `undefined` before the
  // first listing has arrived.
  status: ModelStatus | undefined;
  // The Engine's one download snapshot, whichever entry it is about; the
  // row works out what that means for it.
  download: DownloadState | null;
  audition: Audition;
  onDownload: () => void;
  onDelete: () => void;
};

// One Catalog entry: what it is, who it can be, what it costs, and the one
// thing that can be done with it right now.
//
// Every Voice is auditionable whether or not the model is on disk — that is
// the sheet's whole argument, and it is why the preview clips are app
// resources rather than something the Engine synthesizes.
export default function CatalogEntryRow({
  entry,
  status,
  download,
  audition,
  onDownload,
  onDelete,
}: CatalogEntryRowProps) {
  // Deleting is recoverable — the model can be downloaded again — but the
  // download is hundreds of megabytes, so the button arms first, the same
  // way a History row's does.
  const [armed, setArmed] = useState(false);
  const [readingLicence, setReadingLicence] = useState<
    Pick<CatalogEntry, "name" | "licenseTerms"> | null
  >(null);
  const detail = describeDetail(entry, status);
  const { line, inFlight, busyElsewhere } = describeRowDownload(entry.id, download);
  const known = status !== undefined;
  const installed = status?.installed ?? false;

  return (
    <li className="model">
      <div className="model__head">
        <span className="model__name">{entry.name}</span>
        <span className="model__tier">{entry.tier}</span>
        {entry.licenseTerms.credit && (
          <span className="model__credit">{entry.licenseTerms.credit}</span>
        )}
        {installed && !inFlight && (
          <span className="model__installed">Downloaded</span>
        )}

        <span className="model__action">
          {!known ? (
            <span className="model__checking">Checking…</span>
          ) : installed ? (
            <button
              className={`model__delete${armed ? " model__delete--armed" : ""}`}
              type="button"
              disabled={inFlight}
              onClick={() => {
                setArmed(!armed);
                if (armed) onDelete();
              }}
              onBlur={() => setArmed(false)}
              onKeyDown={(event) => {
                if (event.key === "Escape") setArmed(false);
              }}
            >
              {armed
                ? "Delete?"
                : `Delete · frees ${formatBytes(status?.diskBytes ?? 0)}`}
            </button>
          ) : (
            <button
              className="model__download"
              type="button"
              disabled={inFlight || busyElsewhere}
              onClick={onDownload}
            >
              {`Download · ${formatBytes(entry.downloadBytes)}`}
            </button>
          )}
        </span>
      </div>

      <ul className="model__voices">
        {entry.voices.map((voice) => {
          const url = previewUrl(voice);
          const key = `${entry.id}/${voice.id}`;
          const playing = audition.playing === key;

          return (
            <li className="model__voice" key={voice.id}>
              <button
                className="model__audition"
                type="button"
                disabled={url === null}
                aria-label={
                  playing ? `Stop ${voice.name}` : `Hear ${voice.name}`
                }
                onClick={() => url !== null && audition.toggle(key, url)}
              >
                {playing ? <Square size={14} fill="currentColor" strokeWidth={0} /> : <Play size={14} fill="currentColor" strokeWidth={0} />}
              </button>
              <span className="model__voice-name">{voice.name}</span>
              <span className="model__voice-language">{voice.language}</span>
              {url === null && (
                // Said out loud rather than left to a `title` on a button
                // nobody can focus: a greyed play button under a lede that
                // promises an audition reads as broken, and this is a clip
                // curation has not made yet.
                <span className="model__voice-note">No preview yet</span>
              )}
              {audition.failed === key && (
                <span className="model__voice-note">Preview unavailable</span>
              )}
            </li>
          );
        })}
      </ul>

      <p className="model__detail">
        {describeVoices(entry.voices)} · {detail.cost} ·{" "}
        <button
          className="model__licence"
          type="button"
          onClick={() => setReadingLicence(entry)}
        >
          {detail.licence}
        </button>
        {entry.supportModels.map((support) => (
          <span key={support.name}>
            {" · "}
            <button
              className="model__licence"
              type="button"
              onClick={() => setReadingLicence(support)}
            >
              {support.name} licence
            </button>
          </span>
        ))}
      </p>

      {readingLicence && (
        <LicenceView
          entryName={readingLicence.name}
          terms={readingLicence.licenseTerms}
          onClose={() => setReadingLicence(null)}
        />
      )}

      {line && (
        <p className={`model__progress model__progress--${line.tone}`}>
          {line.progress && (
            <progress
              className="model__bar"
              max={line.progress.total}
              value={line.progress.done}
            />
          )}
          <span>{line.message}</span>
        </p>
      )}

      {known && !installed && !inFlight && busyElsewhere && (
        <p className="model__progress model__progress--working">
          Waiting for the download already running.
        </p>
      )}
    </li>
  );
}
