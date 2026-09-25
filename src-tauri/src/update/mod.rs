//! Over-the-air updates: the shell checks the release endpoint on
//! launch, offers what it finds, and installs only when the reader says so.
//!
//! Every part of that is Rust. The updater plugin ships JS commands of its
//! own, and `check` among them takes `proxy`, `headers`, `timeout` and
//! `target` from whoever calls it — so granting the webview
//! `updater:default` would hand a compromised page an outbound HTTPS
//! request to a host of its choosing with headers of its choosing. That is
//! an upload channel in an app whose whole claim is that nothing a reader
//! does leaves the machine. No updater permission is granted; the plugin is
//! registered for its Rust half only, and the two commands below are
//! argument-free like every other one the page can reach. All the page can
//! do is read what the check found and say yes.

mod archive;
mod status;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Manager, Runtime, State};
use tauri_plugin_updater::{extract_path_from_executable, Updater, UpdaterExt};

pub use status::UpdateStatus;

/// How long the check, and separately the download, may take before they
/// are given up on. The download is an ~80 MB app bundle on a reader's
/// home broadband, so this is generous on purpose; the check answers in
/// milliseconds or not at all. The plugin applies its own timeout to the
/// check alone, so the download is wrapped in this one by hand: without
/// that, a connection that stalls mid-archive would leave the reader in a
/// prompt that says "Installing…" forever.
const TIMEOUT: Duration = Duration::from_secs(600);

/// The shell's single view of the update, shared between the launch check,
/// the install the reader asks for, and the webview polling `update_status`.
#[derive(Default)]
pub struct Updates {
    status: Mutex<UpdateStatus>,
}

impl Updates {
    fn status(&self) -> UpdateStatus {
        self.status.lock().expect("update status lock").clone()
    }

    fn set(&self, status: UpdateStatus) {
        *self.status.lock().expect("update status lock") = status;
    }

    /// Claims the install, so a reader who clicks twice — or a page that
    /// calls the command in a loop — starts exactly one download.
    ///
    /// Returns `false` when there is nothing to install or one is already
    /// running. A previous failure may be tried again: the app it would
    /// have replaced is still the one running.
    fn begin_install(&self) -> bool {
        let mut status = self.status.lock().expect("update status lock");
        if !matches!(
            *status,
            UpdateStatus::Available { .. } | UpdateStatus::Failed { .. }
        ) {
            return false;
        }
        *status = UpdateStatus::Installing;
        true
    }
}

/// Whether a newer Readily is published, and how an install is going.
#[tauri::command]
pub(crate) fn update_status(updates: State<'_, Arc<Updates>>) -> UpdateStatus {
    updates.status()
}

/// Take the update that was offered.
///
/// The one thing the reader can ask for here, and it carries no argument:
/// no URL, no version, no host crosses this boundary (threat model B4).
/// The page cannot say *what* to install — the Rust side re-reads the
/// endpoint it was built with, verifies the archive against the minisign
/// public key baked into this binary, checks that the signed bundle itself
/// is newer than this copy, and installs that or nothing.
/// Readily restarts itself when it is done.
///
/// Safe to call in any state: a call with nothing on offer, or with an
/// install already running, changes nothing.
#[tauri::command]
pub(crate) fn update_install<R: Runtime>(app: AppHandle<R>, updates: State<'_, Arc<Updates>>) {
    if !updates.begin_install() {
        return;
    }
    let updates = Arc::clone(&updates);
    tauri::async_runtime::spawn(async move { install(app, updates).await });
}

/// Manages the update state and checks the endpoint once, in the
/// background, so the window opens without waiting on the network.
///
/// Called from `setup`, beside the Engine's own start.
pub fn start<R: Runtime>(app: &AppHandle<R>) {
    let updates = Arc::new(Updates::default());
    app.manage(Arc::clone(&updates));

    // Offered only where it could be installed. Gatekeeper runs an app
    // opened straight from the mounted image from a read-only translocated
    // copy, and a bundle whose folder the reader cannot write is one the
    // plugin would try to replace with an administrator prompt — a root
    // `rm -rf` on a path derived from the running executable. Neither is
    // a thing to offer a reader; they stay on this version until they
    // drag Readily into Applications.
    if let Err(reason) = install_site() {
        log::info!("not offering an update: {reason}");
        return;
    }

    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        match offered(&app).await {
            Ok(Some(offer)) => updates.set(offer),
            // Nothing published that this build is older than.
            Ok(None) => {}
            // Not the reader's problem: they did not ask for this check.
            // The next launch asks again.
            Err(reason) => log::warn!("update check failed: {reason}"),
        }
    });
}

/// The updater, configured the same way for the check and for the install.
///
/// `no_proxy` is the point of doing this in one place. reqwest reads
/// `HTTPS_PROXY` and friends out of the environment by default, so a proxy
/// variable in the shell that launched Readily would otherwise route both
/// the check and the download through a host neither the reader nor this
/// app chose. The `system-proxy` cargo feature is off for the same reason;
/// this is the belt to its braces.
fn updater<R: Runtime>(app: &AppHandle<R>) -> Result<Updater, String> {
    app.updater_builder()
        .no_proxy()
        .timeout(TIMEOUT)
        .build()
        .map_err(|error| error.to_string())
}

/// What the endpoint has for this build, or `None` when it has nothing
/// newer.
async fn offered<R: Runtime>(app: &AppHandle<R>) -> Result<Option<UpdateStatus>, String> {
    let update = updater(app)?
        .check()
        .await
        .map_err(|error| error.to_string())?;
    Ok(update.map(|update| UpdateStatus::Available {
        version: update.version,
        notes: update.body,
    }))
}

/// Where the running bundle is, when an update could be put there: the
/// `.app` (or, outside a bundle, the executable's folder), in a folder the
/// reader can write, not a translocated copy. The `Err` says why not,
/// without the path: it is written to the log a reader sends, and a
/// bundle's path is a folder name the reader chose.
fn install_site() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    site_for(&exe)
}

/// `install_site` for a given executable.
fn site_for(exe: &Path) -> Result<PathBuf, String> {
    let site = extract_path_from_executable(exe).map_err(|error| error.to_string())?;
    if translocated(&site) {
        return Err(
            "this is Gatekeeper's translocated copy; move Readily to Applications first".to_owned(),
        );
    }
    let parent = site
        .parent()
        .ok_or_else(|| "the bundle has no parent folder".to_owned())?;
    // The plugin replaces the bundle by renaming it, which needs write
    // permission on the folder around it, not on the bundle. Asking the
    // filesystem is the only reliable test of that.
    let probe = parent.join(".readily-update-probe");
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .map_err(|error| format!("the folder around the bundle is not writable: {error}"))?;
    let _ = std::fs::remove_file(&probe);
    Ok(site)
}

/// Gatekeeper's app translocation runs a quarantined app from a read-only
/// mount under a random folder of this name.
fn translocated(site: &Path) -> bool {
    site.components()
        .any(|component| component.as_os_str() == "AppTranslocation")
}

/// Downloads the offered update, verifies it, swaps it in and restarts.
///
/// The check runs a second time rather than holding on to the one the
/// launch check made: whatever is published now is what gets installed,
/// and the archive is verified against the built-in public key either way.
///
/// The signature is verified as part of the download. Before the swap, the
/// supervisor reads the version from the signed bundle's Info.plist and
/// requires it to be newer than the running build; the unsigned manifest's
/// version is only what made the plugin offer it. Anything that fails up
/// to there leaves this build untouched, and the reader may try again. A
/// swap failure may not: the plugin moves the running bundle aside before
/// it puts the new one in place, and an error after that leaves no Readily
/// where one was.
async fn install<R: Runtime>(app: AppHandle<R>, updates: Arc<Updates>) {
    let downloaded = async {
        install_site()?;
        let update = updater(&app)?
            .check()
            .await
            .map_err(|error| error.to_string())?
            .ok_or_else(|| "The update is no longer published.".to_owned())?;
        let bytes = tokio::time::timeout(TIMEOUT, update.download(|_, _| {}, || {}))
            .await
            .map_err(|_| "The download did not finish in ten minutes.".to_owned())?
            .map_err(|error| error.to_string())?;
        let package = app.package_info();
        archive::require_newer(&bytes, &package.name, &package.version)?;
        Ok::<_, String>((update, bytes))
    }
    .await;

    let (update, bytes) = match downloaded {
        Ok(downloaded) => downloaded,
        Err(reason) => {
            log::error!("update not installed: {reason}");
            updates.set(UpdateStatus::Failed {
                reason,
                untouched: true,
            });
            return;
        }
    };

    match update.install(bytes) {
        // `restart` asks the event loop to exit, which runs the shell's
        // `RunEvent::Exit` handler — the same one a normal quit runs, so
        // the Engine is stopped before the new Readily launches. It never
        // returns.
        Ok(()) => app.restart(),
        Err(error) => {
            let reason = error.to_string();
            log::error!("update install failed: {reason}");
            updates.set(UpdateStatus::Failed {
                reason,
                untouched: false,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_install_can_only_be_started_from_an_offer() {
        let updates = Updates::default();

        // Nothing on offer: the command is a no-op.
        assert!(!updates.begin_install());
        assert_eq!(updates.status(), UpdateStatus::Idle);

        updates.set(UpdateStatus::Available {
            version: "0.2.0".into(),
            notes: None,
        });
        assert!(updates.begin_install());
        assert_eq!(updates.status(), UpdateStatus::Installing);

        // A second click, or a page calling in a loop, starts nothing.
        assert!(!updates.begin_install());
    }

    /// The app the failed install would have replaced is still running, so
    /// the reader may ask again rather than being told to go and download
    /// a DMG.
    #[test]
    fn a_failed_install_can_be_tried_again() {
        let updates = Updates::default();
        updates.set(UpdateStatus::Failed {
            reason: "the download stopped".into(),
            untouched: true,
        });

        assert!(updates.begin_install());
        assert_eq!(updates.status(), UpdateStatus::Installing);
    }

    /// Gatekeeper's translocated copy is read-only and gone when the app
    /// quits: an update installed there would install nowhere.
    #[test]
    fn a_translocated_copy_is_recognised_by_its_path() {
        assert!(translocated(Path::new(
            "/private/var/folders/ab/T/AppTranslocation/1F2E-3D4C/d/Readily.app"
        )));
        assert!(!translocated(Path::new("/Applications/Readily.app")));
        assert!(!translocated(Path::new(
            "/Users/reader/Downloads/Readily.app"
        )));
    }

    /// The site is the bundle, and what must be writable is the folder
    /// around it, not the bundle itself: the plugin replaces the bundle by
    /// renaming it, and a folder the reader cannot write is one the plugin
    /// would ask for an administrator password over. macOS only: the
    /// plugin resolves `Contents/MacOS/<exe>` up to the bundle there and
    /// nowhere else, and there is nowhere else an update is offered.
    #[cfg(target_os = "macos")]
    #[test]
    fn a_bundle_in_a_folder_the_reader_cannot_write_is_not_offered_an_update() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().expect("tempdir");
        let bundle = dir.path().join("Readily.app");
        let exe = bundle.join("Contents/MacOS/readily");
        std::fs::create_dir_all(exe.parent().expect("parent")).expect("mkdir");
        std::fs::write(&exe, b"").expect("touch");

        assert_eq!(site_for(&exe).expect("writable folder"), bundle);
        assert!(
            !dir.path().join(".readily-update-probe").exists(),
            "the probe is cleaned up"
        );

        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o500))
            .expect("chmod");
        let refused = site_for(&exe);
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700))
            .expect("chmod back");
        let reason = refused.expect_err("a read-only folder is refused");
        assert!(reason.contains("not writable"), "{reason}");
        // The reason is logged, and the log is sent: no path in it.
        assert!(
            !reason.contains(&dir.path().display().to_string()),
            "{reason}"
        );
    }
}
