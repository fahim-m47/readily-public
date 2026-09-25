//! What it takes to start one Engine: a fresh token, the two `uv`
//! invocations from ADR 0001 §6, and the announcement that says where the
//! Engine chose to listen.

use std::ffi::{OsStr, OsString};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;

/// The environment variable the Engine reads its per-launch token from.
pub const TOKEN_VAR: &str = "READILY_ENGINE_TOKEN";
/// Tells the Engine it was started by the supervisor, so it installs the
/// watchdog that ends it when the supervisor's pipe closes. A standalone
/// `uv run readily-engine` must not do that — its stdin is a terminal.
pub const SUPERVISED_VAR: &str = "READILY_ENGINE_SUPERVISED";
/// Set only by a debug build, where the webview is served by Vite rather
/// than from the bundle, so the Engine may admit Vite's origin too.
pub const DEV_ORIGIN_VAR: &str = "READILY_ENGINE_ALLOW_DEV_ORIGIN";
/// The one line the Engine writes to stdout, right after its bind
/// (`server/serve.py`), telling the supervisor which port it owns.
pub const PORT_ANNOUNCEMENT: &str = "READILY_ENGINE_PORT=";
/// Where `uv` builds the environment, overriding the `.venv` beside the
/// project it would otherwise choose. See [`Paths::environment`].
pub const PROJECT_ENVIRONMENT_VAR: &str = "UV_PROJECT_ENVIRONMENT";
/// Where CPython writes compiled bytecode, overriding the `__pycache__` it
/// would otherwise put beside each source file. See [`Paths::bytecode`].
pub const BYTECODE_PREFIX_VAR: &str = "PYTHONPYCACHEPREFIX";

/// Where the supervisor finds `uv`, the Engine project it runs, and the
/// environment it provisions into.
#[derive(Clone, Debug)]
pub struct Paths {
    pub uv: PathBuf,
    pub engine_dir: PathBuf,
    /// The virtualenv `uv sync` builds and `uv run` runs from, or `None` to
    /// leave `uv` its own default — the `.venv` beside the project.
    ///
    /// Named explicitly in a bundle because the two directories pull apart
    /// there: the project is inside `Readily.app`, so the default would put
    /// a ~1GB virtualenv in `/Applications`, which needs a writable app
    /// bundle to work at all and which a reinstall throws away. ADR 0004 §7
    /// already has the right home for it, in the reader's own tree and
    /// excluded from Time Machine.
    ///
    /// `None` only in a dev checkout, where the default *is* the answer:
    /// the project is this repo's `engine/`, so `uv` lands on the same
    /// `engine/.venv` the test suite already built.
    pub environment: Option<PathBuf>,
    /// Where CPython caches the bytecode it compiles, or `None` to leave it
    /// the `__pycache__` beside each source file.
    ///
    /// Named for the same reason [`Paths::environment`] is, and it is the
    /// same directory problem one step further in: the sources a bundled
    /// build runs are inside `Readily.app`, so the default writes `.pyc`
    /// files into the app bundle on first import. `prepare-bundle.sh` prunes
    /// that bytecode out of the bundle at build time; without this the
    /// running app puts it straight back, and every launch after the first
    /// executes bytecode that shipped with nothing.
    ///
    /// `None` in a dev checkout, where a `__pycache__` beside the source is
    /// what every other Python tool in the repo already expects.
    pub bytecode: Option<PathBuf>,
}

impl Paths {
    /// Resolves all three from the app's resource and application-support
    /// directories, either of which is `None` when the platform has no such
    /// directory.
    pub fn resolve(resource_dir: Option<&Path>, support_dir: Option<&Path>) -> Self {
        Self {
            uv: uv_bin(resource_dir),
            engine_dir: engine_dir(resource_dir),
            environment: environment_dir(support_dir),
            bytecode: bytecode_dir(support_dir),
        }
    }
}

/// A fresh 128-bit bearer token for one Engine launch, hex-encoded
/// (threat model B3).
pub fn new_token() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("the OS CSPRNG must be available");
    bytes
        .iter()
        .fold(String::with_capacity(32), |mut hex, byte| {
            use std::fmt::Write;
            let _ = write!(hex, "{byte:02x}");
            hex
        })
}

/// The `uv` binary. ADR 0001 §6 ships one inside the bundle, pinned by hash
/// at build time (`scripts/prepare-bundle.sh`); a `tauri dev` run, which has
/// no bundle, falls back to `uv` on `PATH`.
///
/// A bundled build does not, and must not: the launch token is handed to
/// whatever this path names, so a `PATH` lookup lets anything earlier on the
/// user's `PATH` be handed the keys to the Engine. Same rule, and the same
/// reason, as [`engine_dir`].
///
/// Gated on `dev`, which is Tauri's own answer rather than
/// `debug_assertions`, which is the Cargo profile. `tauri build --debug`
/// is a bundled app compiled with assertions on, and the two questions
/// have opposite answers there. `dev` comes from the `custom-protocol`
/// feature: the Tauri CLI adds it for `build` and withholds it for `dev`,
/// and `tauri-build` turns that into this cfg. So the gate follows the
/// feature, not the profile — a `cargo build --release` that bypasses the
/// CLI takes the dev branches, which is the one build shape where this is
/// looser than the old gate. Nothing ships that way; `tauri build` is the
/// only path that produces a bundle.
pub fn uv_bin(resource_dir: Option<&Path>) -> PathBuf {
    let bundled = resource_dir.map(|dir| dir.join("uv"));
    #[cfg(dev)]
    {
        match bundled {
            Some(path) if path.is_file() => path,
            _ => PathBuf::from("uv"),
        }
    }
    #[cfg(not(dev))]
    {
        bundled.unwrap_or_else(|| PathBuf::from("/readily-uv-not-bundled"))
    }
}

/// The Engine project directory `uv` runs in. A bundled `.app` carries it
/// in Resources; only a `tauri dev` run — which has no bundle — may fall
/// back to this repo's own `engine/`. Gated on `dev` for the reason spelled
/// out on [`uv_bin`]: `tauri build --debug` bundles.
pub fn engine_dir(resource_dir: Option<&Path>) -> PathBuf {
    let bundled = resource_dir.map(|dir| dir.join("engine"));
    #[cfg(dev)]
    {
        match bundled {
            Some(path) if path.is_dir() => path,
            _ => repo_engine_dir(),
        }
    }
    #[cfg(not(dev))]
    {
        bundled.unwrap_or_else(|| PathBuf::from("/readily-engine-not-bundled"))
    }
}

/// Where a bundled build provisions its Python environment: ADR 0004 §7's
/// `engine/`, inside the reader's own Readily tree.
///
/// Gated on `dev` for the reason spelled out on [`uv_bin`]. A dev checkout
/// gets `None` — `uv`'s default is already this repo's `engine/.venv`, and
/// pointing a `tauri dev` run at the shipped location would build a second
/// gigabyte-scale environment beside the one the terminal uses.
fn environment_dir(support_dir: Option<&Path>) -> Option<PathBuf> {
    shipped_dir(support_dir, crate::data::engine_environment)
}

/// Where a bundled build caches compiled bytecode: ADR 0004 §7's tree again,
/// beside the environment rather than inside the app.
///
/// Gated on `dev` for the reason spelled out on [`uv_bin`]. A dev checkout
/// gets `None`, keeping `__pycache__` beside the sources where `pytest` and
/// every other tool in `engine/` already look for it.
fn bytecode_dir(support_dir: Option<&Path>) -> Option<PathBuf> {
    shipped_dir(support_dir, crate::data::engine_bytecode)
}

/// The shape both of the above share: `None` in a dev checkout, and in a
/// bundled build the directory `under` names below the reader's
/// application-support directory.
///
/// A bundled build with no application-support directory gets a path that
/// cannot be created rather than `None`, so the failure lands where it can
/// be read instead of falling back to `uv`'s default inside the app bundle.
fn shipped_dir(support_dir: Option<&Path>, under: fn(&Path) -> PathBuf) -> Option<PathBuf> {
    #[cfg(dev)]
    {
        let _ = (support_dir, under);
        None
    }
    #[cfg(not(dev))]
    {
        Some(
            support_dir
                .map(under)
                .unwrap_or_else(|| PathBuf::from("/readily-support-dir-unavailable")),
        )
    }
}

/// The only variables a `uv` child inherits from whatever launched the app.
///
/// An allowlist rather than a list of scrubs, because the names worth
/// scrubbing have no end. `uv` takes `UV_PROJECT` (which retargets the whole
/// invocation), `UV_CONFIG_FILE`, `UV_DEFAULT_INDEX` and a dozen more; the
/// Python it then executes takes `PYTHONPATH` and `PYTHONSTARTUP`, either of
/// which puts an attacker's module inside the process that holds the launch
/// token and the reader's library. Naming what may pass is the only version
/// of this rule that stays true as `uv` and CPython grow new variables.
///
/// `PATH` so `uv` can find the interpreter and the tools a build shells out
/// to. `HOME` because the wheel cache lives under it, and dropping it would
/// re-download the world on every launch. `TMPDIR` because macOS gives each
/// user a private one and the fallback, `/tmp`, is writable by everyone.
///
/// Nothing beginning `READILY_` is on it, which is what closes three holes
/// at once. `READILY_ENGINE_PORT` would pin the Engine to a predictable
/// port instead of the `:0` bind it announces, `READILY_DATA_DIR` would
/// point its model loads at a tree this install never verified (threat
/// model B1), and `READILY_ENGINE_ALLOW_DEV_ORIGIN` would reopen Vite's
/// origin in a shipped build (B3). The Engine's variables are the ones
/// [`engine_command`] sets and no others — two, or three on a dev run.
const INHERITED: [&str; 3] = ["PATH", "HOME", "TMPDIR"];

/// [`INHERITED`] applied to an environment, which is the whole of the rule
/// [`uv_command`] enforces.
///
/// A free function over an iterator rather than a loop reading the process
/// environment in place, so the rule can be shown a hostile environment and
/// asked what survives. Reading the real one is the only thing the caller
/// adds, and a test that has to mutate the process environment to say
/// anything is a test that races every other test in the binary.
fn inherited<E>(environment: E) -> Vec<(OsString, OsString)>
where
    E: IntoIterator<Item = (OsString, OsString)>,
{
    environment
        .into_iter()
        .filter(|(name, _)| INHERITED.iter().any(|allowed| name == OsStr::new(allowed)))
        .collect()
}

/// A `uv` invocation carrying nothing it was not given, already pinned to
/// the Engine directory this install shipped.
///
/// `--project` rather than `current_dir` alone: the working directory is
/// where `uv` *starts* looking, and `UV_PROJECT` overrides where it settles.
/// Naming the project on argv is what makes the shipped directory the one
/// that gets synced and run — argv beats the environment, and after
/// [`INHERITED`] there is no environment left to beat.
///
/// It goes on before the subcommand, and it goes on *here* rather than at
/// the three call sites, because position is load-bearing and silently so.
/// `uv run` takes everything after the command name as the command's own
/// arguments, so `uv run readily-engine --project X` hands the pin to the
/// Engine and leaves uv reading `UV_PROJECT` — an invocation byte-for-byte
/// equivalent to one carrying no pin at all. Emitting it before any
/// call site can add a subcommand is what makes that mistake unavailable.
///
/// Deliberately not `--no-config`: the Engine's own `pyproject.toml` carries
/// the `[tool.uv.dependency-metadata]` that `uv.lock` was resolved against,
/// so refusing to read it makes `--locked` fail against the very lockfile it
/// is meant to enforce. A `uv.toml` under an inherited `HOME` can still
/// redirect the index, which is why every wheel in `uv.lock` is hash-pinned:
/// another index can refuse to serve, but it cannot substitute.
///
/// `UV_PROJECT_ENVIRONMENT` and `PYTHONPYCACHEPREFIX` are the two variables
/// this sets rather than refuses, and they are safe for the same reason
/// `--project` is: they are set here, after [`INHERITED`] has emptied the
/// environment, so their values are this process's own and never the
/// launching shell's. Neither names code to load — one is a directory `uv`
/// builds, the other a directory CPython writes to — which is what keeps
/// them on this side of the line `PYTHONPATH` is on the wrong side of.
fn uv_command(paths: &Paths) -> Command {
    let mut command = Command::new(&paths.uv);
    command.env_clear();
    for (name, value) in inherited(std::env::vars_os()) {
        command.env(name, value);
    }
    if let Some(environment) = &paths.environment {
        command.env(PROJECT_ENVIRONMENT_VAR, environment);
    }
    if let Some(bytecode) = &paths.bytecode {
        command.env(BYTECODE_PREFIX_VAR, bytecode);
    }
    command.arg("--project").arg(&paths.engine_dir);
    command.current_dir(&paths.engine_dir);
    own_process_group(&mut command);
    command
}

/// The subcommand and dependency set both provisioning invocations share.
///
/// Shared rather than written twice because the check and the sync have to
/// name the same set or the check can never pass: asked about an environment
/// nothing builds, it answers "outdated" on every launch, and every launch
/// then pays for a full sync.
///
/// `--no-dev` in a shipped build, because the environment this provisions is
/// the reader's and must hold exactly what the license policy cleared.
/// `licenses-engine-macos` gates a `--no-dev` sync, and ADR 0006 keeps SciPy
/// out of the shipped Engine over the GCC runtime its macOS wheels may
/// bundle. Without the flag, first run puts ten more packages on the
/// reader's disk — SciPy and Hypothesis among them, and a tree no license
/// job has ever looked at.
///
/// Not in a dev checkout, where [`Paths::environment`] is `None` and the sync
/// lands on this repo's own `engine/.venv` — the one `pytest` and `ruff` run
/// from. Pruning the dev group there would break the test suite every time
/// someone opened the app. Gated on `dev` for the reason spelled out on
/// [`uv_bin`].
fn sync_args() -> &'static [&'static str] {
    #[cfg(dev)]
    {
        &["sync", "--locked"]
    }
    #[cfg(not(dev))]
    {
        &["sync", "--locked", "--no-dev"]
    }
}

/// [`sync_args`] plus `--check --offline` — is the environment already the
/// one the lockfile describes?
///
/// Provisioning has to run on every launch, because it is the only thing
/// that repairs a half-built environment. But a *warm* launch is not a
/// first run, and publishing `Provisioning` before finding that out would
/// put the first-run screen in front of every reader for as long as the
/// no-op sync takes (~2s here, and it is a disk-speed number, not a
/// constant). This answers the same question in ~0.1s.
///
/// `--offline` is not an optimisation: it is what makes the check safe to
/// run ahead of the reader's consent to wait. It cannot reach the network,
/// so the answer is about the disk and nothing else. Exit 0 means synced;
/// anything else — outdated, partial, or an environment that isn't there —
/// falls through to the real sync, which is the only thing allowed to
/// download.
pub fn provision_check_command(paths: &Paths) -> Command {
    let mut command = uv_command(paths);
    command
        .args(sync_args())
        .args(["--check", "--offline"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

/// [`sync_args`] on its own — first-run provisioning (ADR 0001 §6). This is
/// the one supervisor invocation allowed to reach the network, and it
/// resolves nothing: the committed, hash-pinned `uv.lock` decides every wheel
/// (threat model B5). Its stderr is piped so the supervisor can republish
/// uv's progress.
pub fn provision_command(paths: &Paths) -> Command {
    let mut command = uv_command(paths);
    command
        .args(sync_args())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    command
}

/// `uv run --no-sync readily-engine` — the launch itself. `--no-sync` keeps
/// this invocation offline: provisioning already built the environment.
///
/// The token is passed by environment; argv carries nothing, because `ps`
/// shows argv to every local user on the machine (threat model B3). No
/// port is passed at all: the Engine binds `:0` itself and announces the
/// result on the piped stdout — see [`announced_port`]. Stdin is the pipe
/// `readily_engine.server.lifetime` watches. Stderr is piped too, for the
/// supervisor to relay into the log file (`crate::logs`): a bundled app has
/// no stderr of its own, and what the Engine says as it fails is the one
/// thing a reader's report needs.
pub fn engine_command(paths: &Paths, token: &str) -> Command {
    let mut command = uv_command(paths);
    command
        .args(["run", "--no-sync", "readily-engine"])
        .env(TOKEN_VAR, token)
        .env(SUPERVISED_VAR, "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Only a `tauri dev` run sits behind Vite, and only then may the
    // Engine admit Vite's origin (threat model B3). `dev` is Tauri's own
    // flag, the same one that picks `devCsp` for the webview, so the two
    // halves of that policy cannot disagree — `debug_assertions` would say
    // yes to `tauri build --debug`, which is bundled and served from its
    // own origin. A bundled build cannot be talked into it either:
    // [`INHERITED`] never carries the variable, so the flag exists only
    // where this line puts it.
    if cfg!(dev) {
        command.env(DEV_ORIGIN_VAR, "1");
    }
    command
}

/// Puts the child in its own process group, so stopping it can SIGKILL the
/// whole group: `uv run` does not exec, which makes the Python Engine a
/// grandchild a plain child-kill would orphan — and an Engine wedged in a
/// native call (the MLX crash class) may never run the stdin watchdog that
/// covers the polite paths.
fn own_process_group(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(not(unix))]
    let _ = command;
}

/// Watches the Engine's stdout for the port announcement on its own
/// thread, delivering exactly one `Ok(port)` or `Err(reason)` on the
/// returned channel, then keeps draining stdout (into the log file,
/// beside the Engine's stderr, each line through `redact` first) so the
/// pipe never fills.
///
/// The Engine owns the bind and reports where it landed: the port never
/// exists un-owned, so there is no window in which another local process
/// can take it and be handed the launch token by the first health probe
/// (threat model B3).
pub fn announced_port(
    stdout: impl Read + Send + 'static,
    redact: impl Fn(&str) -> String + Send + 'static,
) -> mpsc::Receiver<Result<u16, String>> {
    let (tx, rx) = mpsc::channel();
    std::thread::Builder::new()
        .name("readily-engine-port".to_owned())
        .spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            let announced = match reader.read_line(&mut line) {
                Ok(0) => Err("the Engine ended before announcing its port".to_owned()),
                Ok(_) => parse_announcement(line.trim_end()),
                Err(error) => Err(format!("the Engine's stdout could not be read: {error}")),
            };
            let _ = tx.send(announced);
            crate::logs::drain(reader, |line| crate::logs::engine_line(&redact(line)));
        })
        .expect("spawn the port announcement reader");
    rx
}

fn parse_announcement(line: &str) -> Result<u16, String> {
    line.strip_prefix(PORT_ANNOUNCEMENT)
        .and_then(|port| port.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .ok_or_else(|| format!("the Engine announced something other than a port: {line:?}"))
}

/// This repo's `engine/`, resolved from the crate's own location at
/// compile time. Dev-only by construction: a shipped binary carrying a
/// build-machine path would be both wrong and a fingerprint.
#[cfg(dev)]
fn repo_engine_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or(Path::new("."))
        .join("engine")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::testing::TOKEN;
    use std::time::Duration;

    /// A stand-in `uv` that prints the environment it was handed and
    /// ignores its arguments, so a test can read the child's real
    /// environment rather than the builder's intentions about it.
    ///
    /// Run through the committed `fake-uv` rather than executed directly,
    /// for the reason given in that file.
    fn env_printing_uv() -> (tempfile::TempDir, Paths) {
        let scratch = tempfile::tempdir().expect("scratch dir");
        std::fs::write(scratch.path().join("uv"), b"#!/bin/sh\nexec /usr/bin/env\n")
            .expect("write the stub");
        let paths = Paths {
            uv: PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/src/engine/fake-uv")),
            engine_dir: scratch.path().to_owned(),
            environment: Some(scratch.path().join("environment")),
            bytecode: Some(scratch.path().join("bytecode")),
        };
        (scratch, paths)
    }

    /// The names in the environment `command` actually starts its child with.
    fn spawned_env_names(mut command: Command) -> Vec<String> {
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let printed = command.output().expect("run the stub uv");
        assert!(printed.status.success(), "the stub uv failed: {printed:?}");
        String::from_utf8(printed.stdout)
            .expect("env prints utf-8")
            .lines()
            .filter_map(|line| line.split_once('=').map(|(name, _)| name.to_owned()))
            .collect()
    }

    fn paths() -> Paths {
        Paths {
            uv: PathBuf::from("/opt/readily/uv"),
            engine_dir: PathBuf::from("/opt/readily/engine"),
            environment: Some(PathBuf::from("/Users/reader/Readily/engine")),
            bytecode: Some(PathBuf::from("/Users/reader/Readily/bytecode")),
        }
    }

    fn args(command: &Command) -> Vec<String> {
        command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect()
    }

    /// `None`: the child is never handed this variable. `Some(Some(_))`:
    /// set explicitly on top of the allowlist.
    fn env_entry(command: &Command, key: &str) -> Option<Option<String>> {
        command
            .get_envs()
            .find(|(name, _)| *name == OsStr::new(key))
            .map(|(_, value)| value.map(|value| value.to_string_lossy().into_owned()))
    }

    fn env_value(command: &Command, key: &str) -> Option<String> {
        env_entry(command, key).flatten()
    }

    #[test]
    fn a_launch_token_is_128_bits_of_hex() {
        let token = new_token();

        assert_eq!(token.len(), 32);
        assert!(
            u128::from_str_radix(&token, 16).is_ok(),
            "{token} is not hex"
        );
    }

    #[test]
    fn every_launch_gets_its_own_token() {
        assert_ne!(new_token(), new_token());
    }

    #[test]
    fn the_token_reaches_the_engine_by_environment_never_argv() {
        let command = engine_command(&paths(), TOKEN);

        assert_eq!(env_value(&command, TOKEN_VAR).as_deref(), Some(TOKEN));
        assert!(
            !args(&command).iter().any(|arg| arg.contains(TOKEN)),
            "argv is world-readable via ps: {:?}",
            args(&command)
        );
    }

    #[test]
    fn the_engine_launches_from_the_synced_environment_without_touching_the_network() {
        let command = engine_command(&paths(), TOKEN);

        assert_eq!(
            args(&command),
            [
                "--project",
                "/opt/readily/engine",
                "run",
                "--no-sync",
                "readily-engine"
            ]
        );
        assert_eq!(command.get_program(), OsStr::new("/opt/readily/uv"));
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/opt/readily/engine"))
        );
    }

    #[test]
    fn the_engine_is_told_it_is_supervised_and_never_which_port_to_take() {
        // A supervisor-reserved port would be released before the Engine
        // rebinds it — a window for another local process to take it and
        // be handed the token by the first probe (threat model B3).
        let command = engine_command(&paths(), TOKEN);

        assert_eq!(env_value(&command, SUPERVISED_VAR).as_deref(), Some("1"));
        assert!(!args(&command).iter().any(|arg| arg.contains("port")));
    }

    #[test]
    fn only_a_dev_run_lets_the_engine_admit_vites_origin() {
        // A bundled app is served from its own origin; if a shipped build
        // ever told the Engine to trust http://127.0.0.1:1420, any page the
        // user's machine serves there would be a permitted origin (B3).
        // The variable in a developer's shell profile must not count.
        let command = engine_command(&paths(), TOKEN);

        assert!(!INHERITED.contains(&DEV_ORIGIN_VAR));
        if cfg!(dev) {
            assert_eq!(env_value(&command, DEV_ORIGIN_VAR).as_deref(), Some("1"));
        } else {
            assert_eq!(env_entry(&command, DEV_ORIGIN_VAR), None);
        }
    }

    #[test]
    fn the_port_arrives_from_the_engines_first_stdout_line() {
        let announced = announced_port(
            std::io::Cursor::new("READILY_ENGINE_PORT=51234\nstray print\n"),
            str::to_owned,
        );

        assert_eq!(
            announced.recv_timeout(Duration::from_secs(2)),
            Ok(Ok(51234))
        );
    }

    #[test]
    fn an_engine_that_ends_without_announcing_is_a_failed_launch_not_a_hang() {
        let announced = announced_port(std::io::Cursor::new(""), str::to_owned);

        let result = announced
            .recv_timeout(Duration::from_secs(2))
            .expect("a verdict");
        assert!(result.is_err(), "EOF with no announcement: {result:?}");
    }

    #[test]
    fn garbage_on_stdout_is_a_failed_launch() {
        let announced = announced_port(
            std::io::Cursor::new("Traceback (most recent call last):\n"),
            str::to_owned,
        );

        let result = announced
            .recv_timeout(Duration::from_secs(2))
            .expect("a verdict");
        assert!(result.is_err(), "not an announcement: {result:?}");
    }

    #[cfg(not(dev))]
    #[test]
    fn provisioning_installs_only_the_groups_the_license_gate_cleared() {
        // Ten packages ride on this flag, SciPy among them (ADR 0006).
        // Without it they land on the reader's disk having passed no
        // license job at all.
        let command = provision_command(&paths());

        assert_eq!(
            args(&command),
            [
                "--project",
                "/opt/readily/engine",
                "sync",
                "--locked",
                "--no-dev"
            ]
        );
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/opt/readily/engine"))
        );
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_run_provisions_the_dev_group_the_repo_tests_from() {
        // Here the sync lands on this repo's own engine/.venv, so --no-dev
        // would uninstall pytest and ruff every time someone opened the app.
        let command = provision_command(&paths());

        assert_eq!(
            args(&command),
            ["--project", "/opt/readily/engine", "sync", "--locked"]
        );
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/opt/readily/engine"))
        );
    }

    #[cfg(not(dev))]
    #[test]
    fn the_provisioning_check_cannot_reach_the_network() {
        // The whole reason the check is allowed to run before the reader
        // has been told anything: it reads the disk and stops there. The
        // groups have to match the sync's, or it answers "outdated" forever
        // and every warm launch pays for a full sync.
        let command = provision_check_command(&paths());

        assert_eq!(
            args(&command),
            [
                "--project",
                "/opt/readily/engine",
                "sync",
                "--locked",
                "--no-dev",
                "--check",
                "--offline"
            ]
        );
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/opt/readily/engine"))
        );
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_provisioning_check_cannot_reach_the_network_either() {
        let command = provision_check_command(&paths());

        assert_eq!(
            args(&command),
            [
                "--project",
                "/opt/readily/engine",
                "sync",
                "--locked",
                "--check",
                "--offline"
            ]
        );
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/opt/readily/engine"))
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_uv_child_is_handed_the_allowlist_and_nothing_else() {
        let (_scratch, paths) = env_printing_uv();

        // The stub is a shell script, and a shell sets these for itself
        // before `env` ever runs. They come from the stand-in, not from
        // the builder under test.
        const SHELLS_OWN: [&str; 3] = ["PWD", "SHLVL", "_"];
        const SET_DELIBERATELY: [&str; 2] = [PROJECT_ENVIRONMENT_VAR, BYTECODE_PREFIX_VAR];

        let seen = spawned_env_names(provision_command(&paths));

        for name in &seen {
            assert!(
                INHERITED.contains(&name.as_str())
                    || SHELLS_OWN.contains(&name.as_str())
                    || SET_DELIBERATELY.contains(&name.as_str()),
                "{name} reached uv but is not on the allowlist: {seen:?}"
            );
        }
        assert!(seen.contains(&"PATH".to_owned()), "uv needs PATH: {seen:?}");
    }

    #[test]
    fn nothing_in_the_parents_environment_can_steer_the_engine() {
        // Named rather than assumed absent. A test that only checks what
        // the runner happens to be carrying proves nothing on a clean
        // machine, and every one of these is a documented hole: the three
        // `UV_*`/`PYTHON*` names retarget the project, the config or the
        // interpreter's import path, and the three `READILY_*` ones are a
        // predictable port, an unverified data tree (threat model B1) and
        // Vite's origin reopened in a shipped build (B3).
        //
        // The last three are near misses rather than holes. They are here
        // because the rule being tested is the comparison, not the list: a
        // prefix or case-insensitive match would let each of them through
        // while every name above still failed, and the assertion would not
        // notice.
        let hostile = [
            ("UV_PROJECT", "/tmp/attacker/engine"),
            ("UV_CONFIG_FILE", "/tmp/attacker/uv.toml"),
            ("PYTHONPATH", "/tmp/attacker/site-packages"),
            ("READILY_ENGINE_PORT", "9999"),
            ("READILY_DATA_DIR", "/tmp/attacker/data"),
            ("READILY_ENGINE_ALLOW_DEV_ORIGIN", "1"),
            ("HOME", "/Users/reader"),
            ("PATH", "/usr/bin"),
            ("PATH_INJECT", "/tmp/attacker/bin"),
            ("HOMEBREW_PREFIX", "/tmp/attacker/brew"),
            ("path", "/tmp/attacker/bin"),
        ]
        .map(|(name, value)| (OsString::from(name), OsString::from(value)));

        let mut kept: Vec<_> = inherited(hostile)
            .into_iter()
            .map(|(name, _)| name.into_string().expect("ascii name"))
            .collect();
        kept.sort();

        assert_eq!(kept, ["HOME", "PATH"]);
    }

    #[cfg(unix)]
    #[test]
    fn the_engines_own_variables_are_the_only_readily_ones_it_gets() {
        let (_scratch, paths) = env_printing_uv();

        let mut seen = spawned_env_names(engine_command(&paths, TOKEN));
        seen.retain(|name| name.starts_with("READILY_"));
        seen.sort();

        let mut expected = vec![SUPERVISED_VAR.to_owned(), TOKEN_VAR.to_owned()];
        if cfg!(dev) {
            expected.push(DEV_ORIGIN_VAR.to_owned());
        }
        expected.sort();
        assert_eq!(seen, expected);
    }

    #[test]
    fn every_uv_invocation_is_pinned_before_it_names_a_subcommand() {
        for command in [
            provision_command(&paths()),
            provision_check_command(&paths()),
            engine_command(&paths(), TOKEN),
        ] {
            let argv = args(&command);
            assert_eq!(
                argv.get(..2),
                Some(["--project".to_owned(), "/opt/readily/engine".to_owned()].as_slice()),
                "not pinned where uv reads it: {argv:?}"
            );
        }
    }

    #[test]
    fn the_engine_is_handed_no_arguments_of_its_own() {
        // Everything after the command name belongs to the Engine, and
        // `ps` shows it to every local user (threat model B3). The Engine
        // is told what it needs by environment and by the stdin pipe.
        let argv = args(&engine_command(&paths(), TOKEN));
        let at = argv
            .iter()
            .position(|arg| arg == "readily-engine")
            .unwrap_or_else(|| panic!("no command name in {argv:?}"));

        assert_eq!(argv.get(at + 1..), Some([].as_slice()), "{argv:?}");
    }

    #[test]
    fn provisioning_never_sees_the_launch_token() {
        assert_eq!(env_value(&provision_command(&paths()), TOKEN_VAR), None);
        assert_eq!(
            env_value(&provision_check_command(&paths()), TOKEN_VAR),
            None
        );
    }

    #[test]
    fn every_uv_invocation_provisions_and_runs_from_the_same_environment() {
        // A sync that builds one virtualenv and a run that launches from
        // another is a first run that downloads a gigabyte and then reports
        // a missing Engine.
        for command in [
            provision_command(&paths()),
            provision_check_command(&paths()),
            engine_command(&paths(), TOKEN),
        ] {
            assert_eq!(
                env_value(&command, PROJECT_ENVIRONMENT_VAR),
                Some("/Users/reader/Readily/engine".to_owned()),
                "{:?}",
                args(&command)
            );
        }
    }

    #[test]
    fn the_environment_the_launching_shell_asked_for_is_not_the_one_uv_gets() {
        // The variable this sets is one `uv` also reads from the
        // environment, so the rule that matters is which of the two wins:
        // `INHERITED` drops the inherited value before this one is set.
        assert!(!INHERITED.contains(&PROJECT_ENVIRONMENT_VAR));

        let hostile = [(
            OsString::from(PROJECT_ENVIRONMENT_VAR),
            OsString::from("/tmp/attacker/venv"),
        )];

        assert!(inherited(hostile).is_empty());
    }

    #[test]
    fn the_engine_never_compiles_bytecode_beside_the_sources_it_runs() {
        assert_eq!(
            env_value(&engine_command(&paths(), TOKEN), BYTECODE_PREFIX_VAR),
            Some("/Users/reader/Readily/bytecode".to_owned())
        );
    }

    #[test]
    fn the_cache_the_launching_shell_asked_for_is_not_the_one_python_gets() {
        assert!(!INHERITED.contains(&BYTECODE_PREFIX_VAR));

        let hostile = [(
            OsString::from(BYTECODE_PREFIX_VAR),
            OsString::from("/tmp/attacker/pycache"),
        )];

        assert!(inherited(hostile).is_empty());
    }

    #[cfg(not(dev))]
    #[test]
    fn a_bundled_build_never_writes_bytecode_into_its_own_app_bundle() {
        let support = Path::new("/Users/reader/Library/Application Support");

        assert_eq!(
            bytecode_dir(Some(support)),
            Some(support.join("Readily").join("bytecode"))
        );
        assert!(bytecode_dir(None).is_some());
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_run_keeps_its_pycache_where_the_rest_of_the_repo_looks() {
        let support = Path::new("/Users/reader/Library/Application Support");

        assert_eq!(bytecode_dir(Some(support)), None);
    }

    #[cfg(not(dev))]
    #[test]
    fn a_bundled_build_never_provisions_into_its_own_app_bundle() {
        let support = Path::new("/Users/reader/Library/Application Support");

        assert_eq!(
            environment_dir(Some(support)),
            Some(support.join("Readily").join("engine"))
        );
        assert!(environment_dir(None).is_some());
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_run_shares_the_environment_the_repo_already_built() {
        let support = Path::new("/Users/reader/Library/Application Support");

        assert_eq!(environment_dir(Some(support)), None);
    }

    #[test]
    fn bundled_resources_win_over_the_development_fallbacks() {
        let resources = tempfile::tempdir().expect("scratch dir");
        std::fs::create_dir(resources.path().join("engine")).expect("bundled engine dir");
        std::fs::write(resources.path().join("uv"), b"").expect("bundled uv");

        assert_eq!(
            engine_dir(Some(resources.path())),
            resources.path().join("engine")
        );
        assert_eq!(uv_bin(Some(resources.path())), resources.path().join("uv"));
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_run_without_a_bundle_takes_uv_from_path() {
        let empty = tempfile::tempdir().expect("scratch dir");

        assert_eq!(uv_bin(Some(empty.path())), PathBuf::from("uv"));
    }

    #[cfg(not(dev))]
    #[test]
    fn a_bundled_build_never_takes_uv_from_path() {
        let empty = tempfile::tempdir().expect("scratch dir");

        assert_ne!(uv_bin(Some(empty.path())), PathBuf::from("uv"));
        assert_ne!(uv_bin(None), PathBuf::from("uv"));
    }

    #[cfg(dev)]
    #[test]
    fn a_dev_run_without_a_bundle_runs_the_engine_from_this_repo() {
        let empty = tempfile::tempdir().expect("scratch dir");

        assert!(
            engine_dir(Some(empty.path()))
                .join("pyproject.toml")
                .is_file(),
            "the development fallback must point at the repo's own engine/"
        );
    }

    #[cfg(not(dev))]
    #[test]
    fn a_bundled_build_without_a_bundle_fails_the_spawn_rather_than_guessing() {
        let empty = tempfile::tempdir().expect("scratch dir");

        assert_eq!(engine_dir(Some(empty.path())), empty.path().join("engine"));
        assert!(!engine_dir(None).exists());
    }
}
