//! Where Readily's application data and the reader's Exports live.
//!
//! ADR 0004 §7 puts everything in one `Readily` tree in the platform's
//! application-data folder — `~/Library/Application Support` on macOS,
//! `$XDG_DATA_HOME` or `~/.local/share` on Linux: the database, the Segments,
//! the models, the provisioned runtime, the logs. Exports sit outside it, in
//! `Readily` in the reader's Documents.
//!
//! The shell resolves both folders through Tauri's path API and hands them
//! to the Engine (`engine::launch`), which writes where it is told. One
//! resolver, not two: the Engine starts with a cleared environment, so its
//! own answer would miss `XDG_DATA_HOME` and the desktop's Documents folder,
//! and the file-manager buttons (`reveal`) would open directories nothing
//! writes to, or `uv` would provision a runtime outside the tree the Engine
//! excludes from backups.

use std::path::{Path, PathBuf};

use tauri::{path::PathResolver, Runtime};

/// The folder name the Engine uses, spelled the same way its own
/// `default_data_dir()` spells it.
const FOLDER: &str = "Readily";

/// Readily's tree below the platform's application-support directory.
pub fn folder(support: &Path) -> PathBuf {
    support.join(FOLDER)
}

/// The Python environment `uv` provisions, which ADR 0004 §7 lists as
/// `engine/` inside the tree.
///
/// Deliberately not inside the app bundle, where `uv`'s own default would
/// put it: the project `uv` syncs is `Readily.app/Contents/Resources/engine`,
/// so an unset `UV_PROJECT_ENVIRONMENT` would try to write a virtualenv into
/// `/Applications`. Here it is the reader's own directory, it survives
/// replacing the app, and `initialize_layout` already keeps it out of Time
/// Machine as a reproducible download.
pub fn engine_environment(support: &Path) -> PathBuf {
    folder(support).join("engine")
}

/// Where CPython writes the bytecode it compiles from the Engine's sources.
///
/// Named for the same reason the environment is: the default puts a
/// `__pycache__` beside every `.py`, and in a bundled build those sources
/// live in `Readily.app/Contents/Resources/engine/src`. So a plain first run
/// writes files into the app bundle — files no reviewer read, that survive
/// every later launch, and that CPython prefers over the `.py` beside them
/// whenever their header matches. `prepare-bundle.sh` prunes that bytecode at
/// build time; this is what stops the running app from putting it back.
///
/// `storage/layout.py` names the same directory and lists it among the
/// reproducible ones Time Machine is told to skip.
pub fn engine_bytecode(support: &Path) -> PathBuf {
    folder(support).join("bytecode")
}

/// Where every Export lands: `Readily` in the reader's Documents.
///
/// Not inside the tree above — Exports are the reader's files, not Readily's
/// cache. The Engine is handed this folder (`engine::launch`) rather than
/// naming its own, so the "Open audio folder" button (`reveal`) opens the
/// folder the Engine writes to.
pub fn audio_folder(documents: &Path) -> PathBuf {
    documents.join(FOLDER)
}

/// The reader's Documents folder, as the platform names it.
///
/// A Linux desktop names it in `user-dirs.dirs`, and one that never wrote
/// that file names none; `~/Documents` is the folder such a reader would
/// look in.
pub fn documents<R: Runtime>(paths: &PathResolver<R>) -> Option<PathBuf> {
    paths
        .document_dir()
        .or_else(|_| paths.home_dir().map(|home| home.join("Documents")))
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Wherever the platform keeps application data; the tree's shape
    /// below it is the same on every platform.
    const SUPPORT: &str = "/data";

    #[test]
    fn the_tree_sits_where_the_engine_writes() {
        assert_eq!(folder(Path::new(SUPPORT)), Path::new("/data/Readily"));
    }

    #[test]
    fn the_provisioned_environment_sits_inside_that_tree() {
        // `storage/layout.py` names this directory `engine` and hands it to
        // `tmutil addexclusion`; a venv anywhere else would be backed up.
        assert_eq!(
            engine_environment(Path::new(SUPPORT)),
            Path::new("/data/Readily/engine")
        );
    }

    #[test]
    fn compiled_bytecode_lands_in_the_tree_and_not_beside_the_sources() {
        assert_eq!(
            engine_bytecode(Path::new(SUPPORT)),
            Path::new("/data/Readily/bytecode")
        );
    }

    #[test]
    fn exports_land_in_the_readers_documents() {
        assert_eq!(
            audio_folder(Path::new("/documents")),
            Path::new("/documents/Readily")
        );
    }
}
