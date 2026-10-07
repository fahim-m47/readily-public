"""Process-environment setup that must happen before anything imports
huggingface_hub: telemetry off, transient metadata confined to the Readily
tree (ADR 0003 §4, threat model egress inventory). Lives in `download/`
because it is download-domain policy: the egress package owns every HF
knob, and edits here take the trust-boundary review lane."""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, local

# The pinned Hub client asks `constants.is_offline_mode()` at request time.
# Extend that policy once with a thread-local override: model construction can
# refuse egress without changing the process-wide setting seen by an explicit
# Catalog download on its own thread.
_HF_POLICY_INSTALL = Lock()
_HF_THREAD = local()


def default_data_dir(platform: str = sys.platform) -> Path:
    """The Readily tree (ADR 0004 §7).

    The shell resolves the platform's application-data folder and names the
    tree in READILY_DATA_DIR (`src-tauri/src/data.rs`), as tests and dev
    scripts do. A standalone run falls back to the platform's usual place.
    """
    override = os.environ.get("READILY_DATA_DIR")
    if override:
        return Path(override)
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Readily"
    return Path.home() / ".local" / "share" / "Readily"


def configure_hf_environment(data_dir: Path) -> None:
    """Call before anything imports huggingface_hub: kills its telemetry and
    keeps its cache/metadata under the Readily tree."""
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HOME"] = str(data_dir / "hf-home")
    # Progress reaches the UI over SSE; tqdm bars would only spam the
    # supervisor's stderr log.
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"


@contextmanager
def hugging_face_offline() -> Iterator[None]:
    """Disable Hugging Face egress for one model-loading operation.

    The override is thread-local, so it covers only Hub calls the loader
    makes on the calling thread. mlx-audio 0.5.0's loaders fetch inline;
    a loader that fanned out to a thread pool would fetch outside it and
    needs a different guard (`HF_HUB_OFFLINE` is process-wide, and a
    Catalog download on its own thread must stay online).
    """
    from huggingface_hub import constants

    with _HF_POLICY_INSTALL:
        if not getattr(constants, "_readily_thread_local", False):
            upstream_policy = constants.is_offline_mode

            def is_offline_mode() -> bool:
                return bool(getattr(_HF_THREAD, "offline", False)) or upstream_policy()

            constants.is_offline_mode = is_offline_mode
            constants._readily_thread_local = True

    was_offline = getattr(_HF_THREAD, "offline", False)
    _HF_THREAD.offline = True
    try:
        yield
    finally:
        _HF_THREAD.offline = was_offline
