//! The supervised Engine (ADR 0001 §3): the Rust core spawns it, guards it
//! with a per-launch token, health-checks it, and restarts it when it dies.
//!
//! This module is the shell's seam onto all of that — everything Tauri
//! touches is here, and the loop underneath it knows nothing about Tauri.

mod health;
mod launch;
mod restart;
mod status;
mod supervisor;

/// Fixtures shared by the engine modules' tests.
#[cfg(test)]
pub(crate) mod testing {
    use super::status::EngineConfig;

    pub const TOKEN: &str = "0123456789abcdef0123456789abcdef";

    /// One launch's way in, as the tests see it.
    pub fn reach(port: u16) -> EngineConfig {
        EngineConfig {
            port,
            token: TOKEN.to_owned(),
        }
    }
}

use std::sync::Arc;

use tauri::{AppHandle, Manager, Runtime, State};

use supervisor::{Engine, Reporter};

pub use status::{EngineConfig, EngineStatus};

/// The Engine's address and token — `null` whenever the Engine is not
/// ready, because then there is nothing to talk to. Re-read it on every
/// `ready` status: a restarted Engine listens on a new port with a new
/// token.
#[tauri::command]
pub(crate) fn engine_config(engine: State<'_, Arc<Engine>>) -> Option<EngineConfig> {
    engine.config()
}

/// What the Engine is doing right now, for a screen that subscribed late.
#[tauri::command]
pub(crate) fn engine_status(engine: State<'_, Arc<Engine>>) -> EngineStatus {
    engine.status()
}

/// Start the Engine over after the supervisor gave up.
///
/// The only way back from a `Failed` status, and the reason a first run
/// whose network dropped is a retry rather than a dead app. Safe to call in
/// any state: the supervisor only ever parks waiting for this, so a request
/// made while it is working changes nothing.
///
/// It grants the webview no new reach — no path, no command, no argument
/// crosses this boundary (threat model B4). The page can ask the supervisor
/// to do again exactly what it does on launch, and nothing else.
#[tauri::command]
pub(crate) fn engine_retry(engine: State<'_, Arc<Engine>>) {
    engine.retry();
}

/// Starts supervising the Engine on its own thread, so the window opens
/// while provisioning and startup happen behind it.
///
/// Note what is *not* here: no `shell` capability is granted to the webview
/// (threat model B4). The Engine is spawned by this Rust code and by
/// nothing else, so a compromised page cannot ask the shell to run a
/// command — it can only ask for the port and token of an Engine the
/// supervisor already chose to start.
pub fn start<R: Runtime>(app: &AppHandle<R>) {
    let resource_dir = app.path().resource_dir().ok();
    let support_dir = app.path().data_dir().ok();
    let documents_dir = crate::data::documents(app.path());
    let paths = launch::Paths::resolve(
        resource_dir.as_deref(),
        support_dir.as_deref(),
        documents_dir.as_deref(),
    );
    let engine = Arc::new(Engine::default());
    app.manage(Arc::clone(&engine));

    let reporter = Box::new(LogReporter);
    std::thread::Builder::new()
        .name("readily-engine-supervisor".to_owned())
        .spawn(move || supervisor::run(engine, paths, supervisor::DEFAULT_POLICY, reporter))
        .expect("spawn the Engine supervisor thread");
}

/// Ends the Engine on the way out of a normal quit. The tidy path only —
/// crash and force-quit are covered by the Engine's own watchdog
/// (`readily_engine.server.lifetime`).
pub fn stop<R: Runtime>(app: &AppHandle<R>) {
    if let Some(engine) = app.try_state::<Arc<Engine>>() {
        engine.shut_down();
    }
}

/// Writes the supervisor's announcements to the log file (`crate::logs`).
///
/// Deliberately not Tauri events. The webview learns the Engine's state by
/// calling `engine_status`, and one source of truth beats two: an event
/// stream nothing subscribes to is a contract that rots. A feature that wants
/// push instead of poll adds the emit *and* the subscriber together.
struct LogReporter;

impl Reporter for LogReporter {
    fn status(&self, status: EngineStatus) {
        if let Some(line) = log_line(&status) {
            log::info!("{line}");
        }
    }
}

/// What the log records for a status, if anything.
///
/// `Provisioning` is recorded once, as the state, and never with the note:
/// the note is a line of uv's own stderr, republished for the first-run
/// screen, and uv narrates where it works (`Creating virtual environment
/// at: /Users/<name>/…`). A file a reader sends holds no path of theirs
/// (threat model, "The log file"), so those lines stay on screen only.
fn log_line(status: &EngineStatus) -> Option<String> {
    match status {
        EngineStatus::Provisioning { note: None } => Some("Provisioning".to_owned()),
        EngineStatus::Provisioning { note: Some(_) } => None,
        other => Some(format!("{other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::log_line;
    use super::status::EngineStatus;

    /// uv's lines name the folder it works in, which names the reader.
    /// The screen shows them; the file a reader sends does not.
    #[test]
    fn a_provisioning_note_from_uv_never_reaches_the_log() {
        assert_eq!(
            log_line(&EngineStatus::Provisioning { note: None }).as_deref(),
            Some("Provisioning")
        );
        assert_eq!(
            log_line(&EngineStatus::Provisioning {
                note: Some("Creating virtual environment at: /Users/reader/Library/x".to_owned())
            }),
            None
        );
        assert_eq!(
            log_line(&EngineStatus::Ready { port: 4242 }).as_deref(),
            Some("Ready { port: 4242 }")
        );
    }
}
