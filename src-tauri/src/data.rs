//! Where Readily's application data lives, as the shell spells it.
//!
//! ADR 0004 §7 puts everything under `~/Library/Application Support/Readily/`
//! — the database, the Segments, the models, the provisioned runtime, the
//! logs. The Engine names that tree for itself in `storage/layout.py`; this
//! module is the shell's one copy of the same fact, needed because two things
//! the shell does reach the tree before the Engine can answer for it: the
//! Finder button (`reveal`), and the environment `uv` provisions into
//! (`engine::launch`), which has to exist before there is an Engine at all.
//!
//! One copy, not two. A second spelling anywhere in the shell would be a
//! button that opens a directory nothing writes to, or a runtime provisioned
//! outside the tree the Engine excludes from Time Machine.

use std::path::{Path, PathBuf};

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

#[cfg(test)]
mod tests {
    use super::*;

    const SUPPORT: &str = "/Users/reader/Library/Application Support";

    #[test]
    fn the_tree_sits_where_the_engine_writes() {
        assert_eq!(
            folder(Path::new(SUPPORT)),
            Path::new("/Users/reader/Library/Application Support/Readily")
        );
    }

    #[test]
    fn the_provisioned_environment_sits_inside_that_tree() {
        // `storage/layout.py` names this directory `engine` and hands it to
        // `tmutil addexclusion`; a venv anywhere else would be backed up.
        assert_eq!(
            engine_environment(Path::new(SUPPORT)),
            Path::new("/Users/reader/Library/Application Support/Readily/engine")
        );
    }

    #[test]
    fn compiled_bytecode_lands_in_the_tree_and_not_beside_the_sources() {
        assert_eq!(
            engine_bytecode(Path::new(SUPPORT)),
            Path::new("/Users/reader/Library/Application Support/Readily/bytecode")
        );
    }
}
