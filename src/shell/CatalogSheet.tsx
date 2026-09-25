import CatalogEntryRow from "./CatalogEntryRow";
import { useAudition } from "./useAudition";
import type { CatalogBinding } from "./useCatalog";
import type { DownloadBinding } from "./useDownloads";
import Sheet from "./Sheet";

export type CatalogSheetProps = {
  // The Catalog and what is on disk, read once for the whole shell: the
  // composer's Voice pill is looking at the same two facts, and a model
  // deleted here has to leave its popover in the same breath.
  catalog: CatalogBinding;
  // The one download the Engine runs. Watched above this screen, because a
  // download that finishes after the sheet is closed still changes what the
  // pill can offer.
  downloads: DownloadBinding;
  onClose: () => void;
};

// The Catalog sheet: every Voice Model Readily can run, downloaded or not.
//
// One step out from the composer, and modal on purpose — acquiring a Voice
// Model is a deliberate detour, not something to stumble into while reading.
// Everything it shows comes from the baked-in Manifest and
// the bundled preview clips, so the sheet is fully usable with the network
// off and nothing on disk: a reader can hear every Voice in the Catalog
// before deciding to spend a download on one.
export default function CatalogSheet({
  catalog,
  downloads,
  onClose,
}: CatalogSheetProps) {
  const audition = useAudition();
  // What the reader just did outranks a standing complaint about the model
  // store, the same way a refused request outranks the Narration's own line.
  const notice = downloads.notice ?? catalog.notice;

  return (
    <Sheet title="Voice models" onClose={onClose}>
      <p className="sheet__lede">
        Hear any voice before you download it. Downloads happen here and stay
        on this Mac.
      </p>

      {/* Mounted even when silent, so its first message is an update
          rather than an arrival — the same reason History's notice is. */}
      <p
        className={`sheet__notice${notice ? "" : " sheet__notice--quiet"}`}
        aria-live="polite"
      >
        {notice ?? ""}
      </p>

      {catalog.entries === null ? (
        <p className="sheet__empty">
          {catalog.failure ?? "Reading the Catalog…"}
        </p>
      ) : (
        <ul className="sheet__models">
          {catalog.entries.map((entry) => (
            <CatalogEntryRow
              key={entry.id}
              entry={entry}
              status={catalog.statusOf(entry.id)}
              download={downloads.download}
              audition={audition}
              onDownload={() => downloads.start(entry.id)}
              onDelete={() => downloads.remove(entry.id, entry.name)}
            />
          ))}
        </ul>
      )}
    </Sheet>
  );
}
