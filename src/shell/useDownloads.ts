import { useCallback, useEffect, useRef, useState } from "react";
import type { DownloadState, EngineClient } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";
import { describeModelDeletion } from "./catalog";
import type { CatalogBinding } from "./useCatalog";

// The Engine's download queue, plus the three things a screen can ask of
// it. The Catalog sheet renders it; the shell watches it.
export type DownloadBinding = {
  // The Engine's snapshot of the job it is running and the jobs waiting
  // behind it.
  download: DownloadState | null;
  // One sentence about the last thing the sheet did, or the last thing the
  // Engine refused.
  notice: string | null;
  start: (entryId: string) => void;
  remove: (entryId: string, name: string) => void;
  // Takes the entry's waiting job back out of the queue.
  withdraw: (entryId: string) => void;
};

// A delete the Engine queued, kept until it leaves the queue so it can be
// announced then rather than when it was asked for.
type PendingDelete = { name: string; freed: number; seen: boolean };

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

  const pendingDeletes = useRef(new Map<string, PendingDelete>());

  const connected = engine.connection.state === "ready";
  const { refresh } = catalog;
  const running =
    download?.phase === "downloading" || download?.phase === "verifying";
  const refreshKey =
    download === null
      ? null
      : running
        ? `running:${download.modelId}`
        : `${download.phase}:${download.modelId}|${download.queue
            .map((job) => `${job.action}:${job.modelId}`)
            .join(",")}`;

  // Re-read whenever disk may have moved — including the first snapshot,
  // which is how a download already running at launch settles — and never
  // mid-download:
  // `downloading` publishes new byte counts several times a second, and a
  // listing per byte count would be a listing per frame. Keyed on which
  // model is running rather than on the phase alone, so a queue that moves
  // straight on to its next download — too quickly for `installed` to be
  // seen — still re-reads the one that just landed; and on the settled phase
  // itself rather than a settled/not-settled boolean, so a reconnect that
  // lands straight on `installed` still counts as a change. Between
  // downloads the queue's membership is part of the key, because a delete
  // leaves the queue in the step that retires the model, without touching
  // the phase: the second of two deletes waiting behind a download leaves
  // while the first is still being cleaned up, under the same settled
  // phase. Spelled out as a string so a snapshot that changes nothing but
  // the queue array's identity does not count as a change.
  useEffect(() => {
    if (!connected) return;
    if (refreshKey === null) return;
    void refresh();
  }, [connected, refreshKey, refresh]);

  // Announces each queued delete once it has left the queue. The Engine
  // deletes a model in the same step that takes it off the queue, and it
  // leaving changes `refreshKey`, so the listing is re-read without any
  // help from here. Only once it has been seen in the queue, so a snapshot
  // from just before the Engine queued it is not taken for one from after
  // it ran; and only without a failure for the model, which is how a delete
  // that could not run leaves the queue — the row's failure line says why.
  // A delete queued and run between two snapshots is never seen and goes
  // unannounced; the Engine only ever queues one behind a download, and
  // that download ending moves `refreshKey`, so the listing still drops it.
  const settleDeletes = useCallback((snapshot: DownloadState) => {
    for (const [entryId, pending] of pendingDeletes.current) {
      const waiting = snapshot.queue.some(
        (job) => job.modelId === entryId && job.action === "delete",
      );
      if (waiting) pending.seen = true;
      else if (pending.seen) {
        pendingDeletes.current.delete(entryId);
        const failed = snapshot.failures.some(
          (failure) => failure.modelId === entryId,
        );
        if (!failed) setNotice(describeModelDeletion(pending.name, pending.freed));
      }
    }
  }, []);

  // A queued delete lives only in the Engine's memory, so an Engine that
  // stops being ready may come back without it — and its empty queue would
  // otherwise read as the delete having run.
  useEffect(() => {
    if (!connected) pendingDeletes.current.clear();
  }, [connected]);

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
            settleDeletes(snapshot);
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
  }, [client, connected, settleDeletes]);

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
          const { queued } = await client.deleteModel(entryId);
          if (queued) {
            pendingDeletes.current.set(entryId, { name, freed, seen: false });
            return;
          }
          setNotice(describeModelDeletion(name, freed));
          await refresh();
        } catch (error) {
          setNotice(sentence(error, "That Voice Model could not be deleted."));
        }
      })();
    },
    [catalog, client, refresh],
  );

  const withdraw = useCallback(
    (entryId: string) => {
      setNotice(null);
      pendingDeletes.current.delete(entryId);
      void client.withdrawModel(entryId).catch((error: unknown) => {
        setNotice(sentence(error, "That could not be taken out of the queue."));
      });
    },
    [client],
  );

  return { download, notice, start, remove, withdraw };
};
