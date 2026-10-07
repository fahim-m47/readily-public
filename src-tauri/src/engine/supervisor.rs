//! The loop ADR 0001 §3 makes the Rust core responsible for: provision,
//! spawn, health-check, restart — and stop trying when the Engine will not
//! come up.

use std::io::{BufRead, BufReader};
use std::process::{Child, ChildStdin, ExitStatus};
use std::sync::mpsc::RecvTimeoutError;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use super::health;
use super::launch::{self, Paths};
use super::restart::{RestartPolicy, Restarts};
use super::status::{EngineConfig, EngineStatus};

/// How long a fresh Engine gets to announce its port and answer its first
/// health check. Generous on purpose — an interpreter start is slow on a
/// cold machine — and never the thing that catches a dead Engine, because
/// a child that exits is noticed the moment it does.
const STARTUP_DEADLINE: Duration = Duration::from_secs(30);
/// How often a starting Engine is re-probed.
const STARTUP_POLL: Duration = Duration::from_millis(200);
/// How often a ready Engine is checked.
const HEALTH_INTERVAL: Duration = Duration::from_secs(2);
/// The deadline on one probe.
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);
/// Consecutive missed health checks before a ready Engine counts as gone.
/// One miss can be a stalled machine; two in a row is a dead Engine.
///
/// WATCH ITEM (unconfirmed): with [`HEALTH_INTERVAL`] and [`PROBE_TIMEOUT`]
/// this gives a healthy Engine roughly four to eight seconds of unanswered
/// probes before a restart. A model load that holds the GIL inside a native
/// call for that long would be restarted while it is working — and MLX's
/// ~2.7GB load is the candidate. `scripts/probe-latency.sh` measures it:
/// across five `qwen3-tts:0.6b` loads on an M-series Mac the longest probe
/// was 1.8s, on a machine under load, and no probe missed. The macOS smoke
/// lane's Engine step (`.github/workflows/ci.yml`) is where a failing case
/// would show up. If one does, the fix is a health endpoint that answers
/// off the loading thread, not a bigger number here.
const MISSES_BEFORE_RESTART: u32 = 2;
/// How many trailing `uv sync` lines to keep for the failure message.
const PROVISION_TAIL: usize = 5;

/// How often the offline provisioning check is looked in on. Tighter than
/// [`STARTUP_POLL`] because the check exists to be fast — roughly a tenth
/// of a second — and a poll on the same scale would spend the saving it
/// was added to make.
const PROVISION_CHECK_POLL: Duration = Duration::from_millis(20);

/// How long the offline check gets before the launch stops waiting on it.
///
/// Generous by two orders of magnitude — the check reads the disk and
/// answers in about a tenth of a second — because the only thing this
/// number decides on a healthy machine is nothing at all. It exists for
/// the machine where `uv` wedges on a stalled volume or a lock another
/// process is holding. Without it the reader sits on "Starting the
/// Engine…" with nothing behind it moving. Falling through does not make
/// the wedge go away — the real sync can stall on the same lock — but it
/// gets there behind a screen that republishes what `uv` is saying and
/// that the reader can quit out of, instead of a silent one that ends
/// only when the volume does.
const PROVISION_CHECK_DEADLINE: Duration = Duration::from_secs(30);

/// What every provisioning path says when the app quit out from under it.
const CANCELLED: &str = "provisioning was cancelled";

/// The shipped restart policy. Five consecutive failures is enough to ride
/// out the observed MLX teardown crash, and few enough that a
/// crash-loop surfaces in seconds rather than spinning forever.
pub const DEFAULT_POLICY: RestartPolicy = RestartPolicy {
    max_consecutive: 5,
    base_delay: Duration::from_millis(500),
    max_delay: Duration::from_secs(8),
    healthy_reset_after: Duration::from_secs(60),
};

/// How the supervisor tells the rest of the app what is happening. Keeps
/// the loop free of Tauri, and the Tauri wiring free of the loop.
pub trait Reporter: Send + Sync + 'static {
    fn status(&self, status: EngineStatus);
}

/// The `uv` process the supervisor currently owns, and the pipe whose
/// closing tells the Engine the supervisor is gone (see `lifetime.py`).
struct Running {
    child: Child,
    stdin: Option<ChildStdin>,
    /// Set the moment a wait reaped the child. Past that point the pid is
    /// the kernel's to reuse, and [`kill_group`] — raw libc, with none of
    /// the post-reap guard `Child::kill` carries — would be signalling
    /// whatever process group inherited the number.
    reaped: bool,
}

impl Running {
    fn new(child: Child, stdin: Option<ChildStdin>) -> Self {
        Self {
            child,
            stdin,
            reaped: false,
        }
    }

    /// Whether the process is still there and has not exited, reaping it
    /// if it has.
    fn alive(&mut self) -> bool {
        if self.reaped {
            return false;
        }
        match self.child.try_wait() {
            Ok(Some(_)) => {
                self.reaped = true;
                false
            }
            // An `Err` reaped nothing — the pid is still ours — so it must
            // not disarm `stop`'s kill, and it proves nothing about the
            // child either way: report it alive and let the next poll (or
            // a health-probe miss) decide.
            Ok(None) | Err(_) => true,
        }
    }

    /// Ends the process — the whole group, because `uv run` does not exec,
    /// so a SIGKILL to `uv` alone would orphan the Python Engine behind
    /// it. The pipe drop is the polite signal; the group kill is for an
    /// Engine too wedged in a native call (the MLX crash class) to ever
    /// run its stdin watchdog. An already-reaped child is left alone: it
    /// is gone, and its pid is no longer ours to signal.
    fn stop(mut self) {
        drop(self.stdin.take());
        if self.reaped {
            return;
        }
        kill_group(&self.child);
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.reaped = true;
    }

    fn wait(mut self) -> std::io::Result<ExitStatus> {
        drop(self.stdin.take());
        let status = self.child.wait();
        self.reaped = true;
        status
    }
}

/// SIGKILL to the child's whole process group — `launch` starts every
/// child as its own group leader. Safe on a child not yet waited on: its
/// pid, the group's id, cannot be reused while the zombie holds it.
#[cfg(unix)]
fn kill_group(child: &Child) {
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
}

#[cfg(not(unix))]
fn kill_group(_child: &Child) {}

/// The supervised Engine: its current state, how to reach it, the process
/// it is running, and the flag that ends all of it — behind one lock, so
/// no reader ever catches two of them mid-transition. The config is `Some`
/// exactly while the status says `Ready`.
#[derive(Default)]
pub struct Engine {
    inner: Mutex<Inner>,
    /// Wakes every wait in the loop the moment `quitting` or `park`
    /// flips — the backoff sleeps, and the stop the supervisor parks on
    /// once its budget is spent.
    wake: Condvar,
}

#[derive(Default)]
struct Inner {
    config: Option<EngineConfig>,
    status: EngineStatus,
    running: Option<Running>,
    quitting: bool,
    /// Where the supervisor stands with respect to giving up. A retry only
    /// means anything once it has, so "parked" and "asked" are one field
    /// rather than two flags that would have to agree.
    park: Park,
}

/// The supervisor's side of the retry button.
#[derive(Default, PartialEq, Eq, Clone, Copy, Debug)]
enum Park {
    /// Working. A retry asked for now is the no-op it is documented to be.
    #[default]
    Running,
    /// Gave up, waiting to be asked for another go.
    Parked,
    /// Asked, and about to start over on a clean budget.
    Asked,
}

impl Engine {
    fn state(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().expect("engine state poisoned")
    }

    /// How to reach the Engine, or `None` whenever there is nothing
    /// listening to reach.
    pub fn config(&self) -> Option<EngineConfig> {
        self.state().config.clone()
    }

    pub fn status(&self) -> EngineStatus {
        self.state().status.clone()
    }

    /// Ends the Engine for good: no restart follows, and the process is
    /// gone before this returns.
    pub fn shut_down(&self) {
        let running = {
            let mut inner = self.state();
            inner.quitting = true;
            inner.config = None;
            inner.running.take()
        };
        self.wake.notify_all();
        if let Some(running) = running {
            running.stop();
        }
    }

    /// Whether the app is on its way out.
    fn quitting(&self) -> bool {
        self.state().quitting
    }

    /// Waits up to `duration`, returning early — and `true` — if the app
    /// starts quitting. Every wait in the loop goes through here so exit
    /// never has to sit out a backoff.
    fn sleep(&self, duration: Duration) -> bool {
        let inner = self.state();
        let (inner, _) = self
            .wake
            .wait_timeout_while(inner, duration, |inner| !inner.quitting)
            .expect("engine state poisoned");
        inner.quitting
    }

    /// Asks the supervisor to start over after it has given up, and does
    /// nothing at all in any other state. The shell offers the button on
    /// any dropped `/v1/events` stream, including one that dropped while
    /// the Engine was healthy, so a press has to be able to mean nothing.
    ///
    /// This is the whole reason the app is not a dead end on a first run.
    /// Provisioning is the one step that needs the network, so the most
    /// likely reason the budget runs out is a connection that dropped —
    /// and quitting to a Finder icon is a poor way to ask someone to try
    /// their wifi again.
    pub fn retry(&self) {
        let mut inner = self.state();
        if inner.park != Park::Parked {
            return;
        }
        inner.park = Park::Asked;
        self.wake.notify_all();
    }

    /// Parks, announces the failure that parked it, and waits until a
    /// retry is asked for or the app quits; `true` means quitting.
    ///
    /// One call rather than a publish and a wait, because the order
    /// matters and only in one direction. [`Engine::publish`] emits
    /// outside the lock, so a failure announced before the park is armed
    /// can have its button pressed while the supervisor is still
    /// `Running` — and [`Engine::retry`] does nothing at all in that
    /// state, so the press would vanish and the park would never be
    /// asked out of.
    fn park_until_retried(&self, reason: String, reporter: &dyn Reporter) -> bool {
        self.state().park = Park::Parked;
        self.publish(EngineStatus::Failed { reason }, reporter);
        // Re-taken after the emit, so a retry pressed in between is
        // already `Asked` and the wait falls straight through.
        let mut inner = self
            .wake
            .wait_while(self.state(), |inner| {
                !inner.quitting && inner.park != Park::Asked
            })
            .expect("engine state poisoned");
        inner.park = Park::Running;
        inner.quitting
    }

    /// Publishes a status that has nothing to reach, clearing the config
    /// in the same motion. `Ready` goes through [`Engine::publish_ready`].
    fn publish(&self, status: EngineStatus, reporter: &dyn Reporter) {
        self.transition(status, None, reporter);
    }

    /// Publishes `Ready` together with the config that reaches it, so no
    /// reader ever sees a ready Engine it cannot talk to.
    fn publish_ready(&self, reach: EngineConfig, reporter: &dyn Reporter) {
        self.transition(
            EngineStatus::Ready { port: reach.port },
            Some(reach),
            reporter,
        );
    }

    fn transition(
        &self,
        status: EngineStatus,
        config: Option<EngineConfig>,
        reporter: &dyn Reporter,
    ) {
        {
            let mut inner = self.state();
            inner.status = status.clone();
            inner.config = config;
        }
        // Outside the lock: the reporter re-enters app code.
        reporter.status(status);
    }

    /// Takes ownership of a newly spawned process, ending any predecessor.
    /// Hands the process straight back — for the caller to stop — when the
    /// app is already quitting: nobody would ever stop a child adopted
    /// after `shut_down` took its pass.
    fn adopt(&self, running: Running) -> Result<(), Running> {
        let previous = {
            let mut inner = self.state();
            if inner.quitting {
                return Err(running);
            }
            inner.running.replace(running)
        };
        if let Some(previous) = previous {
            previous.stop();
        }
        Ok(())
    }

    fn take_child(&self) -> Option<Running> {
        self.state().running.take()
    }

    fn stop_child(&self) {
        if let Some(running) = self.take_child() {
            running.stop();
        }
    }

    /// Whether the process is still there and has not exited. A child the
    /// user killed from outside is noticed here, not by a timeout.
    fn child_alive(&self) -> bool {
        match self.state().running.as_mut() {
            Some(running) => running.alive(),
            None => false,
        }
    }
}

/// Why one supervised run stopped mattering.
enum RunEnd {
    /// The app is quitting: nothing to record, nothing to retry.
    Quit,
    /// The run ended on its own; the restart policy decides what is next.
    Ended {
        healthy_for: Option<Duration>,
        reason: String,
    },
}

impl RunEnd {
    fn ended(healthy_for: Option<Duration>, reason: impl Into<String>) -> Self {
        Self::Ended {
            healthy_for,
            reason: reason.into(),
        }
    }
}

/// Supervises the Engine until the app shuts down or the restart budget
/// runs out. Blocking: call it on its own thread. The policy is a parameter
/// so tests can exercise the budget without waiting out the shipped
/// backoff.
pub fn run(engine: Arc<Engine>, paths: Paths, policy: RestartPolicy, reporter: Box<dyn Reporter>) {
    let reporter = reporter.as_ref();
    let mut restarts = Restarts::new(policy);

    loop {
        if engine.quitting() {
            return;
        }
        let attempt = restarts.consecutive() + 1;

        let end = match provision(&engine, &paths, reporter) {
            Ok(()) => {
                engine.publish(EngineStatus::Starting { attempt }, reporter);
                run_once(&engine, &paths, reporter)
            }
            Err(reason) => RunEnd::ended(None, reason),
        };
        engine.stop_child();
        let RunEnd::Ended {
            healthy_for,
            reason,
        } = end
        else {
            return;
        };
        // Quitting can also land just after a run ended on its own.
        if engine.quitting() {
            return;
        }

        match restarts.record_run_ended(healthy_for) {
            Some(delay) => {
                engine.publish(
                    EngineStatus::Restarting {
                        attempt: restarts.consecutive() + 1,
                        retry_in_ms: delay.as_millis() as u64,
                    },
                    reporter,
                );
                if engine.sleep(delay) {
                    return;
                }
            }
            None => {
                let gave_up = format!(
                    "{reason} — gave up after {} attempts",
                    restarts.consecutive()
                );
                if engine.park_until_retried(gave_up, reporter) {
                    return;
                }
                restarts = Restarts::new(policy);
            }
        }
    }
}

/// Builds the Engine environment from the committed lockfile, republishing
/// uv's own progress as it goes (ADR 0001 §6). A no-op once the environment
/// exists, which is why it runs per attempt instead of being remembered.
///
/// The offline check ahead of it is what keeps `Provisioning` truthful.
/// Publishing it unconditionally would mean claiming a first-run build on
/// every launch for as long as the no-op sync takes — and the shell reads
/// that status as its cue to take the screen over. Asking first costs a
/// tenth of a second and makes the answer worth acting on.
fn provision(engine: &Engine, paths: &Paths, reporter: &dyn Reporter) -> Result<(), String> {
    if provisioned(engine, paths, PROVISION_CHECK_DEADLINE)? {
        return Ok(());
    }
    engine.publish(EngineStatus::Provisioning { note: None }, reporter);

    let mut child = launch::provision_command(paths)
        .spawn()
        // The path is not named: the reason is logged, and the bundle sits
        // wherever the reader put Readily.
        .map_err(|error| format!("could not run the bundled uv to set the Engine up: {error}"))?;
    let stderr = child.stderr.take();
    // Held like any other child so app exit can cut a long first-run
    // download short instead of waiting it out.
    if let Err(rejected) = engine.adopt(Running::new(child, None)) {
        rejected.stop();
        return Err(CANCELLED.to_owned());
    }

    let mut tail = Vec::new();
    if let Some(stderr) = stderr {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            engine.publish(
                EngineStatus::Provisioning {
                    note: Some(line.clone()),
                },
                reporter,
            );
            tail.push(redacted(&line, paths));
            if tail.len() > PROVISION_TAIL {
                tail.remove(0);
            }
        }
    }

    let Some(running) = engine.take_child() else {
        return Err(CANCELLED.to_owned());
    };
    match running.wait() {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(format!(
            "`uv sync --locked` failed ({status}): {}",
            tail.join(" · ")
        )),
        Err(error) => Err(format!(
            "`uv sync --locked` could not be waited on: {error}"
        )),
    }
}

/// `line` with every path uv or the interpreter could name replaced by
/// what it is: the environment uv builds, the Engine project it reads, the
/// uv binary, and the reader's home under any of them. The line goes into
/// a failure reason or a relayed record the log file keeps, and the file
/// holds no path a reader chose (threat model, "The log file"); the
/// bundle's location and the home directory are both theirs.
fn redacted(line: &str, paths: &Paths) -> String {
    let mut line = line.to_owned();
    let named = [
        (paths.environment.as_deref(), "<engine environment>"),
        (Some(paths.engine_dir.as_path()), "<bundled engine>"),
        (Some(paths.uv.as_path()), "<bundled uv>"),
    ];
    for (path, name) in named {
        if let Some(path) = path.and_then(std::path::Path::to_str) {
            line = line.replace(path, name);
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        if let Some(home) = home.to_str().filter(|home| home.len() > 1) {
            line = line.replace(home, "~");
        }
    }
    line
}

/// Whether the environment already matches the lockfile, or [`CANCELLED`]
/// if the app quit while the question was being asked. `deadline` is how
/// long the question gets — [`PROVISION_CHECK_DEADLINE`] in the one place
/// this runs for real, and short enough to watch in a test.
///
/// A check that cannot be run is not an answer, so anything other than a
/// clean exit 0 — a missing `uv`, a signal, an environment that isn't
/// there, or one that never finishes answering — means provisioning goes
/// ahead and the real sync decides.
///
/// Held and polled like every other child rather than waited on inline.
/// This is the first process a launch starts, so it is the easiest one to
/// quit out from under, and a `uv` wedged on a stalled disk would otherwise
/// be a process `shut_down` has no handle on and an app that will not close.
fn provisioned(engine: &Engine, paths: &Paths, deadline: Duration) -> Result<bool, String> {
    let Ok(child) = launch::provision_check_command(paths).spawn() else {
        return Ok(false);
    };
    if let Err(rejected) = engine.adopt(Running::new(child, None)) {
        rejected.stop();
        return Err(CANCELLED.to_owned());
    }
    let asked = Instant::now();
    while engine.child_alive() {
        if engine.sleep(PROVISION_CHECK_POLL) {
            return Err(CANCELLED.to_owned());
        }
        if asked.elapsed() > deadline {
            let Some(check) = engine.take_child() else {
                // Nothing else takes it on this thread, so it went to
                // `shut_down` — the same reading, and the same answer, as
                // the take below.
                return Err(CANCELLED.to_owned());
            };
            check.stop();
            return Ok(false);
        }
    }
    // Already reaped by the poll above, so this is the recorded status
    // rather than a second wait.
    let Some(check) = engine.take_child() else {
        return Err(CANCELLED.to_owned());
    };
    Ok(check.wait().is_ok_and(|status| status.success()))
}

/// Runs one Engine from spawn until it stops answering: spawn, learn which
/// port the Engine bound, probe until ready, then watch.
fn run_once(engine: &Engine, paths: &Paths, reporter: &dyn Reporter) -> RunEnd {
    // One launch, one way in: this token, and the port the Engine binds
    // for it. A restarted Engine gets a fresh pair, so a leaked token dies
    // with the process that used it.
    let token = launch::new_token();
    let mut child = match launch::engine_command(paths, &token).spawn() {
        Ok(child) => child,
        Err(error) => {
            // The path is not named, for the same reason as in `provision`.
            return RunEnd::ended(None, format!("could not start the bundled uv: {error}"));
        }
    };
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().expect("engine stdout is piped");
    // Both pipes reach the log file, and the interpreter can name the
    // bundle's paths on either before the Engine's own config is in
    // place, so both go through `redacted` first.
    if let Some(stderr) = child.stderr.take() {
        let paths = paths.clone();
        crate::logs::relay(stderr, move |line| redacted(line, &paths));
    }
    if let Err(rejected) = engine.adopt(Running::new(child, stdin)) {
        rejected.stop();
        return RunEnd::Quit;
    }

    // The Engine owns the bind and announces where it landed (threat model
    // B3) — see `launch::announced_port`.
    let announced = {
        let paths = paths.clone();
        launch::announced_port(stdout, move |line| redacted(line, &paths))
    };
    let started = Instant::now();
    let port = loop {
        if engine.quitting() {
            return RunEnd::Quit;
        }
        if !engine.child_alive() {
            return RunEnd::ended(None, "the Engine exited before it was ready");
        }
        match announced.recv_timeout(STARTUP_POLL) {
            Ok(Ok(port)) => break port,
            Ok(Err(reason)) => return RunEnd::ended(None, reason),
            Err(RecvTimeoutError::Disconnected) => {
                return RunEnd::ended(None, "the Engine ended before announcing its port")
            }
            Err(RecvTimeoutError::Timeout) => {
                if started.elapsed() > STARTUP_DEADLINE {
                    return RunEnd::ended(
                        None,
                        format!(
                            "the Engine did not announce a port within {}s",
                            STARTUP_DEADLINE.as_secs()
                        ),
                    );
                }
            }
        }
    };

    let reach = EngineConfig { port, token };
    loop {
        if engine.quitting() {
            return RunEnd::Quit;
        }
        if !engine.child_alive() {
            return RunEnd::ended(None, "the Engine exited before it was ready");
        }
        if health::probe(&reach, PROBE_TIMEOUT) {
            break;
        }
        if started.elapsed() > STARTUP_DEADLINE {
            return RunEnd::ended(
                None,
                format!(
                    "the Engine did not answer /health within {}s",
                    STARTUP_DEADLINE.as_secs()
                ),
            );
        }
        if engine.sleep(STARTUP_POLL) {
            return RunEnd::Quit;
        }
    }

    let ready_at = Instant::now();
    engine.publish_ready(reach.clone(), reporter);

    let mut misses = 0;
    loop {
        if engine.sleep(HEALTH_INTERVAL) {
            return RunEnd::Quit;
        }
        if !engine.child_alive() {
            return RunEnd::ended(Some(ready_at.elapsed()), "the Engine exited");
        }
        if health::probe(&reach, PROBE_TIMEOUT) {
            misses = 0;
        } else {
            misses += 1;
            if misses >= MISSES_BEFORE_RESTART {
                return RunEnd::ended(
                    Some(ready_at.elapsed()),
                    "the Engine stopped answering /health",
                );
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests;

#[cfg(test)]
mod redaction_tests {
    use super::redacted;
    use crate::engine::launch::Paths;
    use std::path::PathBuf;

    /// uv narrates where it works, and where it works is under the bundle
    /// and the reader's home. The failure reason names neither.
    #[test]
    fn a_uv_line_names_no_path_of_the_readers() {
        let paths = Paths {
            uv: PathBuf::from("/Volumes/Apps/Readily.app/Contents/Resources/uv"),
            engine_dir: PathBuf::from("/Volumes/Apps/Readily.app/Contents/Resources/engine"),
            environment: Some(PathBuf::from(
                "/Users/reader/Library/Application Support/Readily/engine-env",
            )),
            bytecode: None,
            data: None,
            audio: None,
        };
        let home = std::env::var("HOME").unwrap_or_default();
        assert_eq!(
            redacted(
                "Creating virtual environment at: /Users/reader/Library/Application Support/Readily/engine-env",
                &paths
            ),
            "Creating virtual environment at: <engine environment>"
        );
        assert_eq!(
            redacted(
                "error: Failed to read `/Volumes/Apps/Readily.app/Contents/Resources/engine/pyproject.toml`",
                &paths
            ),
            "error: Failed to read `<bundled engine>/pyproject.toml`"
        );
        assert_eq!(
            redacted(
                &format!("Using CPython 3.12 at: {home}/.local/bin/python"),
                &paths
            ),
            "Using CPython 3.12 at: ~/.local/bin/python"
        );
        assert_eq!(
            redacted("Resolved 84 packages in 12ms", &paths),
            "Resolved 84 packages in 12ms"
        );
    }
}
