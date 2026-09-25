//! The log file a reader can send.
//!
//! Until this module, nothing Readily did was written down anywhere: the
//! shell printed to a stderr no bundled app has, and the Engine's own
//! stderr went the same way. A reader who hit a problem had nothing to
//! send. Now every `log` record the shell makes, and every line the Engine
//! writes to stderr, lands in `<data folder>/logs/readily.log`, rotated by
//! size with one older file kept, so the folder never grows past about two
//! megabytes and a friend can read the file before they send it.
//!
//! The file is designed to be sent, so it is designed never to hold a
//! Source. The shell's records carry Engine states, update outcomes and
//! process exits — nothing a reader typed — and the Engine's logging config
//! (`readily_engine/logs.py`) keeps its own records to ids, ordinals and
//! counts. The rule is what the call sites write; [`line`] is a backstop
//! behind them, at the one writer both feed, and withholds any message
//! longer than a status line could be, so the one failure it can catch — a
//! record that quotes a paragraph — is recorded as its length (threat
//! model, "The log file").
//! `readily.db` in the same data folder *does* hold Source text, which is
//! why the file is what a reader is asked for and the folder is not.

use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};

use log::{Level, LevelFilter, Record};
use tauri::plugin::TauriPlugin;
use tauri::Runtime;
use tauri_plugin_log::fern::FormatCallback;
use tauri_plugin_log::{RotationStrategy, Target, TargetKind};

/// The file's stem; the plugin adds `.log`, and names the older one
/// `readily_<date>.log`.
const STEM: &str = "readily";

/// How large the current file may grow before it becomes the older one.
const MAX_FILE_SIZE: u128 = 1024 * 1024;

/// Longer than any status line the shell or the Engine writes, and shorter
/// than any paragraph of a Source. A message past this is withheld whole:
/// a clipped prefix would still be prose.
const MAX_MESSAGE_CHARS: usize = 1000;

/// The target the Engine's lines are recorded under, so a reader of the
/// file can tell the two processes apart at a glance.
const ENGINE_TARGET: &str = "engine";

/// The `logs` folder inside Readily's tree — the same one the Engine's
/// `storage/layout.py` makes and lists among its own.
fn folder(data: &Path) -> PathBuf {
    data.join("logs")
}

/// The current log file: what a reader is asked to send.
pub fn file(data: &Path) -> PathBuf {
    folder(data).join(format!("{STEM}.log"))
}

/// The plugin that installs the writer, for `AppHandle::plugin` in setup.
///
/// Registered from setup rather than the builder because the data folder
/// is resolved through the app's own path API. Everything the shell logs
/// after this call reaches the file; a `tauri dev` run also sees it on the
/// terminal. `Info` and up: tauri and wry narrate their internals at debug
/// and trace, and a file a reader is meant to read has no room for them.
pub fn plugin<R: Runtime>(data: &Path) -> TauriPlugin<R> {
    let mut targets = vec![Target::new(TargetKind::Folder {
        path: folder(data),
        file_name: Some(STEM.to_owned()),
    })];
    if cfg!(debug_assertions) {
        targets.push(Target::new(TargetKind::Stderr));
    }
    tauri_plugin_log::Builder::new()
        .targets(targets)
        .level(LevelFilter::Info)
        .max_file_size(MAX_FILE_SIZE)
        .rotation_strategy(RotationStrategy::KeepSome(1))
        .format(|out: FormatCallback, message, record: &Record| {
            out.finish(format_args!(
                "{}",
                line(
                    &now(),
                    record.level(),
                    record.target(),
                    &message.to_string()
                )
            ))
        })
        .build()
}

/// Copies the Engine's stderr into the log on its own thread until the
/// Engine closes it. Before this it went to a stderr no bundled app has.
///
/// `redact` is applied to every line first: the supervisor's, which names
/// the bundle's paths by what they are. The Engine's own logging config
/// keeps paths out of its records, but a traceback the interpreter prints
/// before that config exists — an import that fails — names its files by
/// absolute path, and the bundle can sit anywhere the reader chose.
pub fn relay(stderr: impl Read + Send + 'static, redact: impl Fn(&str) -> String + Send + 'static) {
    std::thread::Builder::new()
        .name("readily-engine-stderr".to_owned())
        .spawn(move || drain(BufReader::new(stderr), |line| engine_line(&redact(line))))
        .expect("spawn the Engine log relay");
}

/// Hands every line `reader` yields to `record` until the reader ends,
/// bytes rather than `str`: `BufRead::lines` stops at the first byte that
/// is not UTF-8, and a relay that stopped would leave the Engine blocked
/// on a full pipe the next time it wrote. A stray byte from a native
/// library is recorded as U+FFFD instead. `record` is [`engine_line`]
/// outside tests.
pub fn drain(mut reader: impl BufRead, mut record: impl FnMut(&str)) {
    let mut bytes = Vec::new();
    loop {
        bytes.clear();
        match reader.read_until(b'\n', &mut bytes) {
            Ok(0) | Err(_) => return,
            Ok(_) => {
                let line = String::from_utf8_lossy(&bytes);
                record(line.trim_end_matches(['\n', '\r']));
            }
        }
    }
}

/// Records one line the Engine wrote, under [`ENGINE_TARGET`], at the level
/// the Engine's own logging config put in front of it.
pub fn engine_line(line: &str) {
    if line.trim().is_empty() {
        return;
    }
    let (level, rest) = relayed(line);
    log::log!(target: ENGINE_TARGET, level, "{rest}");
}

/// The level a line the Engine wrote belongs at, and the line without the
/// level's own name: `readily_engine/logs.py` writes `LEVEL logger message`,
/// and Python's levels map onto `log`'s. A line without one — a traceback,
/// a `print` — is information, kept whole.
fn relayed(line: &str) -> (Level, &str) {
    let (token, rest) = line.split_once(' ').unwrap_or((line, ""));
    let level = match token {
        "DEBUG" => Level::Debug,
        "INFO" => Level::Info,
        "WARNING" => Level::Warn,
        "ERROR" | "CRITICAL" => Level::Error,
        _ => return (Level::Info, line),
    };
    (level, rest)
}

/// One line of the file: timestamp, level, target, message.
///
/// The message is the one field that could carry text the shell was never
/// meant to see, so it is the one field with rules: past
/// [`MAX_MESSAGE_CHARS`] it is replaced by its length, and the reader's
/// home directory in it is spelled `~`.
fn line(at: &str, level: Level, target: &str, message: &str) -> String {
    let message = if message.chars().count() > MAX_MESSAGE_CHARS {
        format!(
            "[a {}-character message was withheld: the log keeps lengths, never text]",
            message.chars().count()
        )
    } else {
        unhomed(message, std::env::var_os("HOME").as_deref())
    };
    format!("{at} {level:<5} {target} {message}")
}

/// `message` with every spelling of `home` as `~`. Every path the shell
/// names on its own — the data folder, the bundle, the Engine's tree —
/// starts with the home, and the home holds the reader's name.
fn unhomed(message: &str, home: Option<&std::ffi::OsStr>) -> String {
    match home.and_then(std::ffi::OsStr::to_str) {
        Some(home) if home.len() > 1 => message.replace(home, "~"),
        _ => message.to_owned(),
    }
}

/// UTC, to the millisecond, so two readers' files line up without a time
/// zone in each line.
fn now() -> String {
    let format = time::macros::format_description!(
        "[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:3]Z"
    );
    time::OffsetDateTime::now_utc()
        .format(format)
        .unwrap_or_else(|_| "0000-00-00T00:00:00.000Z".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_line_is_timestamp_level_target_message() {
        assert_eq!(
            line(
                "2026-09-11T10:00:00.000Z",
                Level::Warn,
                "readily_lib::update",
                "check failed: no route to host"
            ),
            "2026-09-11T10:00:00.000Z WARN  readily_lib::update check failed: no route to host"
        );
    }

    /// The writer is the last gate: whatever reaches it that reads like a
    /// paragraph is recorded as a length, so a Source can never be sent
    /// by accident inside the file that is meant to be sent.
    #[test]
    fn a_message_the_length_of_prose_is_withheld_and_its_length_kept() {
        let prose = "Call me Ishmael. ".repeat(100);
        let written = line("t", Level::Info, "engine", &prose);
        assert!(!written.contains("Ishmael"), "{written}");
        assert!(
            written.contains("1700-character message was withheld"),
            "{written}"
        );

        let status = "a".repeat(MAX_MESSAGE_CHARS);
        assert!(line("t", Level::Info, "engine", &status).ends_with(&status));
    }

    #[test]
    fn a_path_under_the_home_directory_is_recorded_from_the_tilde() {
        let home = std::ffi::OsStr::new("/Users/reader");
        assert_eq!(
            unhomed(
                "no log folder at /Users/reader/Library/Application Support/Readily/logs",
                Some(home)
            ),
            "no log folder at ~/Library/Application Support/Readily/logs"
        );
        assert_eq!(unhomed("Engine exited: 0", Some(home)), "Engine exited: 0");
        // A home the environment does not name, or names as `/`, changes nothing.
        assert_eq!(unhomed("/tmp/x", None), "/tmp/x");
        assert_eq!(unhomed("/tmp/x", Some(std::ffi::OsStr::new("/"))), "/tmp/x");
    }

    /// The Engine writes `LEVEL logger message`; the file records the
    /// message at that level, once, rather than as an INFO line that
    /// starts with the word WARNING.
    #[test]
    fn an_engine_line_keeps_its_own_level_and_a_bare_line_is_information() {
        assert_eq!(
            relayed("WARNING readily_engine.worker synthesis retried: segment 4"),
            (
                Level::Warn,
                "readily_engine.worker synthesis retried: segment 4"
            )
        );
        assert_eq!(
            relayed("CRITICAL readily_engine.server out of memory"),
            (Level::Error, "readily_engine.server out of memory")
        );
        assert_eq!(
            relayed("Traceback (most recent call last):"),
            (Level::Info, "Traceback (most recent call last):")
        );
        assert_eq!(relayed("INFO"), (Level::Info, ""));
    }

    /// One byte that is not UTF-8 must not end the relay: the Engine
    /// would block on its next write once the pipe filled.
    #[test]
    fn a_byte_that_is_not_utf8_does_not_stop_the_relay() {
        let mut seen = Vec::new();
        let reader = std::io::Cursor::new(b"first\nbad \xff byte\r\nlast".to_vec());
        drain(reader, |line| seen.push(line.to_owned()));
        assert_eq!(seen, ["first", "bad \u{FFFD} byte", "last"]);
    }

    #[test]
    fn the_file_sits_in_the_logs_folder_the_engine_makes() {
        let data = Path::new("/Users/reader/Library/Application Support/Readily");
        assert_eq!(
            file(data),
            Path::new("/Users/reader/Library/Application Support/Readily/logs/readily.log")
        );
    }

    #[test]
    fn the_clock_reads_as_utc_to_the_millisecond() {
        let at = now();
        assert_eq!(at.len(), "2026-09-11T10:00:00.000Z".len(), "{at}");
        assert!(at.ends_with('Z'));
    }
}
