import { useCallback, useEffect, useState } from "react";
import type { DownloadState, EngineClient } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { describeModelDeletion } from "./catalog";
import type { CatalogBinding } from "./useCatalog";

// The one download the Engine runs, plus the two things a screen can ask
// for. The Catalog sheet renders it; the shell watches it.
export type DownloadBinding = {
  // The Engine's snapshot of the one download it runs at a time.
  download: DownloadState | null;
  // One sentence about the last thing the sheet did, or the last thing the
  // Engine refused.
  notice: string | null;
  start: (entryId: string) => void;
  remove: (entryId: string, name: string) => void;
};

const sentence = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const PROGRESS_LOST =
  "Download progress is unavailable right now. Downloads still run.";

// Binds the shell to the Engine's download stream.
//
// A second stream rather than a second handler on `useEngine`'s: this one
// carries a different snapshot on a different route, and the Narration
// stream the whole app depends on stays exactly as it was.
//
// Mounted for the life of the app rather than with the sheet that shows it,
// because a download is not a screen: a reader can start one, close the
// sheet, and keep reading while it finishes. Watching only while the sheet
// is up would mean the `installed` beat arrives for nobody, and the model
// store is never re-read — so the Voice pill would go on offering a Voice
// Model the reader already has.
export const useDownloads = (
  client: EngineClient,
  engine: EngineBinding,
  catalog: CatalogBinding,
): DownloadBinding => {
  const [download, setDownload] = useState<DownloadState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const connected = engine.connection.state === "ready";
  const phase = download?.phase ?? null;
  const { refresh } = catalog;

  // Re-read whenever disk may have moved — including the first snapshot,
  // which is how a download already running at launch settles — and never
  // mid-download:
  // `downloading` publishes new byte counts several times a second, and a
  // listing per byte count would be a listing per frame. Keyed on the phase
  // itself rather than a settled/not-settled boolean, so a reconnect that
  // lands straight on `installed` still counts as a change.
  useEffect(() => {
    if (!connected) return;
    if (phase === null) return;
    if (phase === "downloading" || phase === "verifying") return;
    void refresh();
  }, [connected, phase, refresh]);

  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    void client
      .watchDownloads(
        {
          onDownload: (snapshot) => {
            // A snapshot is progress arriving, so the sentence saying it
            // would not stops being true and is withdrawn. Only that one:
            // anything else here is the reader's own last action, and it
            // is still the newest thing they did.
            setNotice((said) => (said === PROGRESS_LOST ? null : said));
            setDownload(snapshot);
          },
          // Said once, not once per reconnect: the loop retries twice a
          // second, and a reader who clicks Download deserves to know the
          // progress they are waiting for is not coming, rather than
          // watching a row that never changes.
          onLost: () => setNotice(PROGRESS_LOST),
        },
        controller.signal,
      )
      .catch(() => {
        // The loop reconnects on its own; a rejection here means it gave up
        // because the signal aborted, which is this effect tearing down.
      });
    return () => controller.abort();
  }, [client, connected]);

  const start = useCallback(
    (entryId: string) => {
      setNotice(null);
      void client.downloadModel(entryId).catch((error: unknown) => {
        setNotice(sentence(error, "That download could not be started."));
      });
    },
    [client],
  );

  const remove = useCallback(
    (entryId: string, name: string) => {
      // Read before the delete, because after it the store reports zero —
      // and the number is the whole point of saying anything at all.
      const freed = catalog.statusOf(entryId)?.diskBytes ?? 0;
      void (async () => {
        try {
          await client.deleteModel(entryId);
          setNotice(describeModelDeletion(name, freed));
          await refresh();
        } catch (error) {
          setNotice(sentence(error, "That Voice Model could not be deleted."));
        }
      })();
    },
    [catalog, client, refresh],
  );

  return { download, notice, start, remove };
};
