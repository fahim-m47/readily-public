//! "Open audio folder", "Open data folder" and "Show log file": the three
//! places on disk Settings sends a reader to in their file manager — the
//! Finder on macOS, whatever the desktop names on Linux.
//!
//! The audio folder is where every Export lands — `Readily` in Documents —
//! and is the one a reader goes looking for. ADR 0004 §7 puts everything
//! else in one `Readily` tree in the platform's application-data folder —
//! the database, the Segments, the models, the runtime, the logs — and
//! Settings puts a button on that too, because a Settings screen that
//! reports disk usage owes the reader a way to go and look at the disk.
//!
//! The commands take no arguments. The folders come from [`crate::data`],
//! on the platform's own Documents and data directories, the log file from
//! [`crate::logs`], and each is handed to the opener plugin's Rust API by
//! absolute path: `/usr/bin/open` on macOS, the desktop's file manager on
//! Linux. Nothing the webview holds reaches this boundary, so there is
//! nothing in it to steer (threat model B4) — a compromised page can ask for
//! these folders or this file in a file manager, and for no other and no
//! other program. The plugin's JS scope does not govern these calls, and
//! needs no widening for them.
//!
//! The log file gets a button of its own because the folder is not what a
//! reader should send: `readily.db` beside the log holds their Sources.

use std::path::Path;

use tauri::{AppHandle, Manager, Runtime};

use crate::{data, logs};

/// Shows `folder` in the file manager.
///
/// Refuses a folder that is not there rather than letting the opener fail
/// with its own wording: the sheet renders whatever comes back, and "Readily
/// has not made its data folder yet" is something a reader can understand.
fn reveal(folder: &Path) -> Result<(), String> {
    if !folder.is_dir() {
        return Err("Readily has not made its data folder yet.".to_owned());
    }
    show_folder(folder, "Readily's data folder")
}

/// Shows `file` selected in its folder, so what the reader drags into their
/// report is the file and not the folder around it — never opened with
/// whatever app claims `.log`.
fn reveal_file(file: &Path) -> Result<(), String> {
    if !file.is_file() {
        return Err("Readily has not written a log yet.".to_owned());
    }
    tauri_plugin_opener::reveal_item_in_dir(file)
        .map_err(|_| "Readily could not show its log file.".to_owned())
}

/// Opens `folder` in the Finder or the Linux desktop's file manager, whichever
/// the system hands folders to. `what` names it in the error a reader sees.
fn show_folder(folder: &Path, what: &str) -> Result<(), String> {
    tauri_plugin_opener::open_path(folder, None::<&str>)
        .map_err(|_| format!("Readily could not open {what}."))
}

/// Show the folder every Export lands in.
///
/// Made first if it is not there: a reader who presses this before their
/// first Export should find the empty folder their audio will arrive in,
/// not an error. Argument-free like the others (threat model B4).
#[tauri::command]
pub(crate) fn open_audio_folder<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    let documents = data::documents(app.path())
        .ok_or_else(|| "Readily could not find your Documents folder.".to_owned())?;
    let folder = data::audio_folder(&documents);
    std::fs::create_dir_all(&folder)
        .map_err(|_| "Readily could not make its audio folder.".to_owned())?;
    show_folder(&folder, "Readily's audio folder")
}

/// Show Readily's data folder.
///
/// Takes no arguments, and grants the webview no reach it did not have: the
/// path is this function's own, and the only program it can start is the
/// platform's file manager (threat model B4).
#[tauri::command]
pub(crate) fn open_data_folder<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    let support = app
        .path()
        .data_dir()
        .map_err(|_| "Readily could not find its data folder.".to_owned())?;
    reveal(&data::folder(&support))
}

/// Show Readily's log file: the file a reader is asked to send with a bug
/// report.
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
