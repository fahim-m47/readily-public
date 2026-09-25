//! "Open data folder" and "Show log file": the two things the shell does
//! with the directory the Engine owns.
//!
//! ADR 0004 §7 puts everything under `~/Library/Application Support/Readily/`
//! — the database, the Segments, the models, the runtime, the logs — and
//! Settings puts a button on it, because a Settings screen that reports disk
//! usage owes the reader a way to go and look at the disk.
//!
//! The commands take no arguments. The folder comes from [`crate::data`],
//! on the platform's own data directory, the log file from [`crate::logs`]
//! inside it, and each is handed to `/usr/bin/open` by absolute path.
//! Nothing the webview holds reaches this boundary, so there is nothing in
//! it to steer (threat model B4) — a compromised page can ask for this
//! folder or this file in the Finder, and for no other and no other program.
//!
//! The log file gets a button of its own because the folder is not what a
//! reader should send: `readily.db` beside the log holds their Sources.

use std::ffi::OsStr;
use std::path::Path;
use std::process::Command;

use tauri::{AppHandle, Manager, Runtime};

use crate::{data, logs};

/// Shows `folder` in the Finder.
///
/// Refuses a folder that is not there rather than letting `open` fail with
/// its own wording: the sheet renders whatever comes back, and "Readily has
/// not made its data folder yet" is something a reader can understand.
fn reveal(folder: &Path) -> Result<(), String> {
    if !folder.is_dir() {
        return Err("Readily has not made its data folder yet.".to_owned());
    }
    finder([folder.as_os_str()], "Readily's data folder")
}

/// Shows `file` selected in its folder in the Finder, so what the reader
/// drags into their report is the file and not the folder around it.
fn reveal_file(file: &Path) -> Result<(), String> {
    if !file.is_file() {
        return Err("Readily has not written a log yet.".to_owned());
    }
    // `-R` selects the file in its folder instead of opening it with
    // whatever app claims `.log`.
    finder(["-R".as_ref(), file.as_os_str()], "Readily's log file")
}

/// `/usr/bin/open` with exactly these arguments. The binary by absolute
/// path, no shell, so nothing in a path can be read as syntax.
fn finder<'a>(args: impl IntoIterator<Item = &'a OsStr>, what: &str) -> Result<(), String> {
    let status = Command::new("/usr/bin/open")
        .args(args)
        .status()
        .map_err(|_| "The Finder could not be opened.".to_owned())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("The Finder could not open {what}."))
    }
}

/// Show Readily's data folder in the Finder.
///
/// Takes no arguments, and grants the webview no reach it did not have: the
/// path is this function's own, and the only program it can start is the
/// one named here (threat model B4).
#[tauri::command]
pub(crate) fn open_data_folder<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    let support = app
        .path()
        .data_dir()
        .map_err(|_| "Readily could not find its data folder.".to_owned())?;
    reveal(&data::folder(&support))
}

/// Show Readily's log file in the Finder: the file a reader is asked to
/// send with a bug report.
///
/// Argument-free like `open_data_folder`, and for the same reason (threat
/// model B4): the path is `crate::logs`' own.
#[tauri::command]
pub(crate) fn reveal_logs<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    let support = app
        .path()
        .data_dir()
        .map_err(|_| "Readily could not find its data folder.".to_owned())?;
    reveal_file(&logs::file(&data::folder(&support)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_folder_that_is_not_there_is_refused_in_words_a_reader_can_read() {
        let missing = tempfile::tempdir().expect("scratch dir").keep();
        std::fs::remove_dir_all(&missing).expect("remove scratch dir");
        assert_eq!(
            reveal(&missing),
            Err("Readily has not made its data folder yet.".to_owned())
        );
    }

    #[test]
    fn a_log_that_was_never_written_is_refused_in_words_a_reader_can_read() {
        let scratch = tempfile::tempdir().expect("scratch dir");
        assert_eq!(
            reveal_file(&scratch.path().join("readily.log")),
            Err("Readily has not written a log yet.".to_owned())
        );
        // The folder is not the file, and is not what a reader should send.
        assert!(reveal_file(scratch.path()).is_err());
    }

    #[test]
    fn a_file_is_not_mistaken_for_the_data_folder() {
        let scratch = tempfile::tempdir().expect("scratch dir");
        let file = scratch.path().join("Readily");
        std::fs::write(&file, b"not a folder").expect("write file");
        assert!(reveal(&file).is_err());
    }
}
