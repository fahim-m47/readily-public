import { useCallback, useEffect, useRef, useState } from "react";
import type { CatalogEntry, EngineClient, ModelStatus } from "../engine/client";
import type { EngineBinding } from "../engine/useEngine";

// What Readily can run and what of it is on disk, as one live view.
export type CatalogBinding = {
  // Every Catalog entry, or `null` before the first listing arrives. The
  // Manifest is baked in, so this fills with the network off.
  entries: CatalogEntry[] | null;
  // The entry the Engine narrates with when nobody has chosen — the
  // Manifest's own default, and so the Voice Model a first run lands.
  // `null` until the Catalog has been read.
  defaultModelId: string | null;
  // The entry the Manifest names as the escape from a wait on synthesis.
  // `null` until the Catalog has been read, and when it names none.
  defaultFastModelId: string | null;
  // Why there are no entries to show, when the read failed rather than
  // being slow. Kept apart from `notice` so a screen never says it is
  // still reading the Catalog and that the read failed at the same time.
  failure: string | null;
  // The model store's answer for one entry, or `undefined` before the first
  // listing. Absent is not "not installed" — it is "not known yet".
  statusOf: (entryId: string) => ModelStatus | undefined;
  // Whether the Voice Model a Narration would be made with is on disk, or
  // `null` before the store has answered. `null` is not "none": a shell that
  // treated the moment before the first listing as an empty disk would
  // refuse to narrate for a beat on every launch.
  //
  // Asked of a named model, because the reader narrates with the Voice they
  // chose and not with whichever Voice Model happens to be downloaded. A
  // `modelId` of `null` is the selection not having been read yet, and asks
  // the weaker question the shell can still act on: is there any Voice Model
  // at all.
  voiceModelInstalled: (modelId: string | null) => boolean | null;
  // Why what is on disk is not known. The Catalog still renders without it;
  // what cannot be said is which entries are installed.
  notice: string | null;
  // Re-read what is on disk, and the Catalog too if it never arrived. Disk
  // moves when a download settles and when a model is deleted, and both
  // happen in the sheet — this is how the rest of the shell finds out. It
  // is also the way back from a read that refused: without it a first run
  // whose store read failed has nothing left to press.
  refresh: () => Promise<void>;
};

const sentence = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

// Binds the shell to the Catalog and to the model store.
//
// Two reads with different lifetimes. The Catalog is baked into the release
// and cannot change while the app runs, so it is read once. The model store
// changes underneath the screens — a download finishes, a delete lands — so
// it is re-read on request.
//
// Mounted once, at the top: the composer's Voice pill and the Catalog sheet
// are looking at the same two facts, and a model deleted in the sheet has to
// leave the pill's popover in the same breath. Two readings of the same
// listing would be two chances to disagree about what is installed.
export const useCatalog = (
  client: EngineClient,
  engine: EngineBinding,
): CatalogBinding => {
  const [entries, setEntries] = useState<CatalogEntry[] | null>(null);
  const [defaultModelId, setDefaultModelId] = useState<string | null>(null);
  const [defaultFastModelId, setDefaultFastModelId] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Map<string, ModelStatus> | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const connected = engine.connection.state === "ready";

  // Read once and then left alone: the Manifest is baked into the release
  // and cannot change while the app runs. Held in a ref rather than read
  // off `entries`, so re-reading the store — which happens after every
  // download and delete — does not drag a Catalog read along with it, and
  // so `refresh` keeps one identity for the life of the app.
  const read = useRef(false);
  const readCatalog = useCallback(
    () =>
      read.current
        ? Promise.resolve()
        : client.listCatalog().then(
            (catalog) => {
              read.current = true;
              setFailure(null);
              setEntries(catalog.models);
              setDefaultModelId(catalog.defaultModelId ?? null);
              setDefaultFastModelId(catalog.defaultFastModelId ?? null);
            },
            (error: unknown) => {
              setFailure(sentence(error, "The Catalog could not be read."));
            },
          ),
    [client],
  );

  useEffect(() => {
    if (!connected) return;
    void readCatalog();
  }, [connected, readCatalog]);

  const readModels = useCallback(
    () =>
      client.listModels().then(
        (models) => {
          setNotice(null);
          setStatuses(new Map(models.map((model) => [model.id, model])));
        },
        (error: unknown) => {
          setNotice(sentence(error, "The downloaded models could not be read."));
        },
      ),
    [client],
  );

  const refresh = useCallback(async () => {
    await Promise.all([readCatalog(), readModels()]);
  }, [readCatalog, readModels]);

  useEffect(() => {
    if (!connected) return;
    void readModels();
  }, [connected, readModels]);

  const statusOf = useCallback(
    (entryId: string) => statuses?.get(entryId),
    [statuses],
  );

  // Read off the store's own listing rather than off the Catalog, so an
  // entry the Manifest has and the store has not heard of cannot be counted
  // as installed.
  const voiceModelInstalled = useCallback(
    (modelId: string | null) => {
      if (statuses === null) return null;
      if (modelId === null) {
        return [...statuses.values()].some((model) => model.installed);
      }
      return statuses.get(modelId)?.installed ?? false;
    },
    [statuses],
  );

  return {
    entries,
    defaultModelId,
    defaultFastModelId,
    failure,
    statusOf,
    voiceModelInstalled,
    notice,
    refresh,
  };
};
