"""Pin and load-check the shared aligner through the ordinary model store."""

import numpy as np

from readily_engine.catalog import SupportModel
from readily_engine.curation.draft import SupportDraft
from readily_engine.curation.pinning import FetchFiles, pin
from readily_engine.download.fetch import fetch_files
from readily_engine.loading.alignment import ForcedAligner
from readily_engine.store import ModelStore


def curate_support(
    draft: SupportDraft, store: ModelStore, fetch: FetchFiles = fetch_files
) -> SupportModel:
    """Compute pins from downloaded bytes, verify promotion, then exercise ONNX.

    This verifies the runtime interface. Timestamp qualification is a separate
    listening measurement; silence supplies no human onset reference.
    """
    staging = store.staging_dir(draft.name, draft.tag)
    entry = draft.entry(pin(draft, staging, fetch))
    store.promote_staged(entry)
    ForcedAligner(entry, store).align("A", np.zeros(16000, dtype=np.float32), 16000)
    return entry
