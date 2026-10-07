mod data;
mod engine;
mod logs;
mod reveal;
mod update;

use tauri::Manager;

fn navigation_is_local(url: &tauri::Url, allow_dev_origin: bool) -> bool {
    match (url.scheme(), url.host_str(), url.port()) {
        ("tauri", Some("localhost"), None) => true,
        ("http" | "https", Some("tauri.localhost"), None) => true,
        ("http", Some("127.0.0.1"), Some(1420)) => allow_dev_origin,
        _ => false,
    }
}

fn navigation_guard<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri::plugin::Builder::new("navigation-guard")
        .on_navigation(|_, url| navigation_is_local(url, cfg!(dev)))
        .build()
}

/// Builds and runs the Tauri application: the Readily window, the opener
/// plugin that hands a licence's source page to the reader's browser, the
/// updater that keeps an installed build current, the log file a reader can
/// send, and the supervised Engine behind them all.
pub fn run() {
    tauri::Builder::default()
        .plugin(navigation_guard())
        .plugin(
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
        // Registered for its Rust half. The plugin also installs JS
        // commands, and `capabilities/default.json` grants the webview
        // none of them: the page must not be able to name the host, the
        // headers or the proxy of an outbound request. See `update`.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // One call, and only one: Tauri keeps the last `invoke_handler` and
        // discards any before it, so a second call here would silently take
        // the Engine commands away from the webview. This list is also the
        // enumeration `capabilities/default.json` makes its claim about —
        // everything the page can reach, and every one of them argument-free.
        .invoke_handler(tauri::generate_handler![
            engine::engine_config,
            engine::engine_status,
            engine::engine_retry,
            reveal::open_audio_folder,
            reveal::open_data_folder,
            reveal::reveal_logs,
            update::update_status,
            update::update_install,
            update::open_download_page,
        ])
        .setup(|app| {
            // The log file before anything that writes to it. Registered
            // here rather than on the builder because the data folder is
            // the app's path API's to name. Not fatal: a home the file
            // cannot be made in leaves Readily without a log, which is
            // better than without Readily.
            match app.path().data_dir() {
                Ok(support) => {
                    if let Err(error) = app.handle().plugin(logs::plugin(&data::folder(&support))) {
                        eprintln!("no log file: {error}");
                    }
                }
                Err(error) => eprintln!("no log file: {error}"),
            }
            log::info!("Readily {} starting", app.package_info().version);
            engine::start(app.handle());
            update::start(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Readily shell")
        .run(|app, event| {
            // The last event a normal quit delivers. A crash or a
            // force-quit never gets here — the Engine's own stdin watchdog
            // covers those.
            if matches!(event, tauri::RunEvent::Exit) {
                engine::stop(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::navigation_is_local;
    use serde_json::json;
    use serde_json::Value;

    #[test]
    fn the_opener_scope_is_only_hugging_face_https_pages() {
        let capability: Value = serde_json::from_str(include_str!("../capabilities/default.json"))
            .expect("the capability file is JSON");

        let opener_permissions: Vec<Value> = capability["permissions"]
            .as_array()
            .expect("permissions is a list")
            .iter()
            .filter(|permission| {
                permission
                    .as_str()
                    .is_some_and(|identifier| identifier.starts_with("opener:"))
                    || permission["identifier"]
                        .as_str()
                        .is_some_and(|identifier| identifier.starts_with("opener:"))
            })
            .cloned()
            .collect();

        assert_eq!(
            opener_permissions,
            vec![json!({
                "identifier": "opener:allow-open-url",
                "allow": [{ "url": "https://huggingface.co/**" }]
            })]
        );
    }

    /// The updater plugin is registered, so its JS commands exist in the
    /// host. What keeps them out of the page's reach is this file granting
    /// no `updater:` permission — and no `process:` one, since a restart
    /// the page can ask for is a restart it can ask for in a loop. Both
    /// happen in Rust, behind `update_install`. The log plugin's JS `log`
    /// command is kept out the same way: a page that can write lines into
    /// the file a reader sends could write a Source into it.
    #[test]
    fn the_webview_is_granted_no_updater_process_or_log_permission() {
        let capability: Value = serde_json::from_str(include_str!("../capabilities/default.json"))
            .expect("the capability file is JSON");

        let granted = capability["permissions"]
            .as_array()
            .expect("permissions is a list")
            .iter()
            .map(|permission| {
                permission
                    .as_str()
                    .or_else(|| permission["identifier"].as_str())
                    .expect("every permission names an identifier")
                    .to_owned()
            })
            .collect::<Vec<_>>();

        assert!(
            !granted
                .iter()
                .any(|identifier| identifier.starts_with("updater:")
                    || identifier.starts_with("process:")
                    || identifier.starts_with("log:")),
            "the webview must reach the updater only through update_status and \
             update_install, and the log file only through reveal_logs, but the \
             capability grants: {granted:?}"
        );
    }

    /// Plain HTTP would let the network rewrite the release prompt or
    /// strand updates, and an empty public key would turn the minisign check
    /// into a formality. Both are one edit of the config away.
    #[test]
    fn the_update_endpoint_is_https_and_the_release_key_is_set() {
        let config: Value = serde_json::from_str(include_str!("../tauri.conf.json"))
            .expect("the Tauri config is JSON");
        let updater = &config["plugins"]["updater"];

        let endpoints = updater["endpoints"]
            .as_array()
            .expect("the updater names its endpoints");
        assert!(!endpoints.is_empty(), "there is nowhere to check");
        for endpoint in endpoints {
            let url = endpoint.as_str().expect("an endpoint is a URL string");
            assert!(
                url.starts_with("https://"),
                "the update endpoint must be https, got {url}"
            );
        }

        assert!(
            updater["pubkey"]
                .as_str()
                .is_some_and(|key| !key.trim().is_empty()),
            "the minisign public key must be baked into the build"
        );

        assert_eq!(
            config["bundle"]["createUpdaterArtifacts"],
            json!(true),
            "a build that emits no updater artifacts can never be updated"
        );
    }

    /// Tauri replaces a platform file's `windows` array wholesale rather than
    /// merging it, so the macOS window is a second copy of the base one. It
    /// may add the overlay title bar and nothing else: a copy that drifted
    /// would, say, turn Tauri's drop handler back on for Mac readers only.
    #[test]
    fn the_macos_window_is_the_base_window_with_an_overlay_title_bar() {
        let base: Value = serde_json::from_str(include_str!("../tauri.conf.json"))
            .expect("the Tauri config is JSON");
        let macos: Value = serde_json::from_str(include_str!("../tauri.macos.conf.json"))
            .expect("the macOS Tauri config is JSON");

        let mut window = macos["app"]["windows"][0].clone();
        let overlay = window
            .as_object_mut()
            .expect("the macOS window is an object");
        for key in ["hiddenTitle", "titleBarStyle", "trafficLightPosition"] {
            assert!(overlay.remove(key).is_some(), "the macOS window sets {key}");
        }
        assert_eq!(macos["app"]["windows"].as_array().map(Vec::len), Some(1));
        assert_eq!(window, base["app"]["windows"][0]);
    }

    /// The Linux file replaces the build command wholesale too, to build the
    /// frontend with Opus previews. Any other step the base command gains —
    /// or loses, like staging the pinned `uv` — must reach Linux as well.
    #[test]
    fn the_linux_build_is_the_base_build_with_opus_previews() {
        let base: Value = serde_json::from_str(include_str!("../tauri.conf.json"))
            .expect("the Tauri config is JSON");
        let linux: Value = serde_json::from_str(include_str!("../tauri.linux.conf.json"))
            .expect("the Linux Tauri config is JSON");

        let command = |config: &Value| {
            config["build"]["beforeBuildCommand"]
                .as_str()
                .expect("the build command is a string")
                .to_owned()
        };
        assert_eq!(
            command(&linux),
            command(&base).replace("bun run build", "bash scripts/opus-previews.sh")
        );
        assert_ne!(command(&linux), command(&base));
    }

    #[test]
    fn webview_navigation_stays_on_readily_origins() {
        for url in [
            "tauri://localhost/",
            "http://tauri.localhost/",
            "https://tauri.localhost/",
        ] {
            assert!(navigation_is_local(&url.parse().unwrap(), false));
        }
        assert!(navigation_is_local(
            &"http://127.0.0.1:1420/".parse().unwrap(),
            true
        ));
        assert!(!navigation_is_local(
            &"http://127.0.0.1:1420/".parse().unwrap(),
            false
        ));

        for url in [
            "https://huggingface.co/acme/model",
            "https://tauri.localhost.example.com/",
            "file:///tmp/licence",
            "mailto:someone@example.com",
        ] {
            assert!(!navigation_is_local(&url.parse().unwrap(), true));
        }
    }
}
