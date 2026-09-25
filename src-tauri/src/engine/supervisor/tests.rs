use super::*;
use crate::engine::testing;
use std::path::PathBuf;
use std::process::Command;
use std::sync::mpsc::{channel, Sender};

/// Collects everything the supervisor announced, in order.
struct Recorder {
    statuses: Sender<EngineStatus>,
}

impl Reporter for Recorder {
    fn status(&self, status: EngineStatus) {
        let _ = self.statuses.send(status);
    }
}

/// How many times provisioning began. Each run opens with a note-less
/// `Provisioning` and then republishes `uv`'s output, so counting the
/// variant would count lines rather than builds.
fn provisioning_runs(seen: &[EngineStatus]) -> usize {
    seen.iter()
        .filter(|status| matches!(status, EngineStatus::Provisioning { note: None }))
        .count()
}

/// Every line `uv` wrote, as the shell would receive them.
fn provisioning_notes(seen: &[EngineStatus]) -> Vec<&str> {
    seen.iter()
        .filter_map(|status| match status {
            EngineStatus::Provisioning { note } => note.as_deref(),
            _ => None,
        })
        .collect()
}

fn impatient() -> RestartPolicy {
    RestartPolicy {
        max_consecutive: 2,
        base_delay: Duration::from_millis(10),
        max_delay: Duration::from_millis(20),
        healthy_reset_after: Duration::from_secs(60),
    }
}

/// A stand-in for `uv` running `script`. The scratch dir must outlive
/// the run, so it travels with the paths.
///
/// The script is not run directly: `paths.uv` is the committed `fake-uv`,
/// which finds the script beside the environment. See that file for why.
fn fake_uv_script(script: &str) -> (tempfile::TempDir, Paths) {
    let dir = tempfile::tempdir().expect("scratch dir");
    std::fs::write(dir.path().join("uv"), script).expect("write fake uv");
    let paths = Paths {
        uv: PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/src/engine/fake-uv")),
        engine_dir: PathBuf::from("."),
        environment: Some(dir.path().join("environment")),
        bytecode: Some(dir.path().join("bytecode")),
    };
    (dir, paths)
}

/// A fake `uv` whose `sync` and `run` each succeed or fail to order,
/// so a test can shape one failure without shaping the other.
///
/// It answers `sync --check` the way uv does — off the state of the
/// environment rather than off a constant — by leaving a marker behind
/// when a sync succeeds. Without that the supervisor's gate could not
/// be tested at all: the whole point of it is that the second launch
/// gets a different answer from the first.
///
/// It also refuses an unpinned invocation, the way the real uv would
/// silently not: reading past `--project` is exactly what uv does to
/// find the verb, so a pin that drifted out of the leading slots would
/// stop every supervisor test here rather than passing quietly.
///
/// `--check` is found by scanning the flags rather than by position,
/// because uv does not care where it sits either. Matching a slot made
/// this fake answer a *check* by provisioning, so the moment a flag was
/// added ahead of it every test that turns on the gate failed at once,
/// naming the flag rather than the fake that misread it.
fn fake_uv(sync_exit: u8, run_exit: u8) -> (tempfile::TempDir, Paths) {
    fake_uv_script(&format!(
        r#"#!/bin/sh
marker="$(dirname "$0")/synced"
if [ "$1" != --project ]; then echo "unpinned: $*" >&2; exit 111; fi
shift 2
verb=$1
for arg in "$@"; do
  if [ "$arg" = --check ]; then
    if [ -f "$marker" ]; then exit 0; else exit 1; fi
  fi
done
case "$verb" in
  sync) echo 'Resolved 2 packages' >&2; [ {sync_exit} -eq 0 ] && touch "$marker"; exit {sync_exit} ;;
  *) exit {run_exit} ;;
esac
"#
    ))
}

/// The same fake on a machine that has already been provisioned once.
fn warm_uv(run_exit: u8) -> (tempfile::TempDir, Paths) {
    let (dir, paths) = fake_uv(0, run_exit);
    std::fs::write(dir.path().join("synced"), b"").expect("mark the environment built");
    (dir, paths)
}

/// Runs the supervisor on its own thread until it surfaces a failure,
/// then quits it. On a thread because the loop no longer returns when
/// its budget runs out — it parks there waiting to be asked for
/// another go — so a test that called it inline would never come back.
fn supervise(paths: Paths, policy: RestartPolicy) -> (Arc<Engine>, Vec<EngineStatus>) {
    let (engine, statuses, worker) = supervising(paths, policy);
    let seen = collect_until_failed(&statuses);
    engine.shut_down();
    worker.join().expect("the supervisor thread");
    (engine, seen)
}

type Announcements = (
    Arc<Engine>,
    std::sync::mpsc::Receiver<EngineStatus>,
    std::thread::JoinHandle<()>,
);

fn supervising(paths: Paths, policy: RestartPolicy) -> Announcements {
    let (status_tx, statuses) = channel();
    let engine = Arc::new(Engine::default());
    let supervised = Arc::clone(&engine);
    let worker = std::thread::spawn(move || {
        run(
            supervised,
            paths,
            policy,
            Box::new(Recorder {
                statuses: status_tx,
            }),
        )
    });
    (engine, statuses, worker)
}

/// Everything announced up to and including the first `Failed`.
fn collect_until_failed(statuses: &std::sync::mpsc::Receiver<EngineStatus>) -> Vec<EngineStatus> {
    let mut seen = Vec::new();
    while let Ok(status) = statuses.recv_timeout(Duration::from_secs(30)) {
        let failed = matches!(status, EngineStatus::Failed { .. });
        seen.push(status);
        if failed {
            break;
        }
    }
    seen
}

#[test]
fn an_engine_that_will_not_come_up_stops_retrying_and_surfaces_the_failure() {
    let (_uv, paths) = fake_uv(0, 1);
    let (engine, seen) = supervise(paths, impatient());

    assert_eq!(
        provisioning_notes(&seen),
        ["Resolved 2 packages"],
        "uv's own output reaches the webview, not just the log: {seen:?}"
    );
    let starts = seen
        .iter()
        .filter(|status| matches!(status, EngineStatus::Starting { .. }))
        .count();
    assert_eq!(
        starts, 3,
        "two tolerated failures plus the one that spends the budget: {seen:?}"
    );
    assert_eq!(
        provisioning_runs(&seen),
        1,
        "an environment that is already built is not re-announced as a \
         first run on every retry: {seen:?}"
    );
    assert!(
        matches!(seen.last(), Some(EngineStatus::Failed { .. })),
        "the loop must end in a surfaced failure, not keep looping: {seen:?}"
    );
    assert_eq!(engine.status(), *seen.last().expect("a final status"));
    assert_eq!(
        engine.config(),
        None,
        "there is nothing listening, so there is nothing to hand the webview"
    );
}

#[test]
fn failed_provisioning_is_retried_like_any_other_failed_launch() {
    // A first run downloads an interpreter and wheels; a network blip
    // there must not leave a dead app with no second chance.
    let (_uv, paths) = fake_uv(1, 0);
    let (_engine, seen) = supervise(paths, impatient());

    assert_eq!(
        provisioning_runs(&seen),
        3,
        "provisioning sits inside the budget: {seen:?}"
    );
    assert!(
        seen.iter()
            .any(|status| matches!(status, EngineStatus::Restarting { .. })),
        "and it backs off between tries: {seen:?}"
    );
    match seen.last() {
        Some(EngineStatus::Failed { reason }) => {
            assert!(reason.contains("uv sync --locked"), "{reason}")
        }
        other => panic!("expected a surfaced provisioning failure, got {other:?}"),
    }
}

#[test]
fn a_warm_launch_never_claims_to_be_setting_itself_up() {
    let (_uv, paths) = warm_uv(1);
    let (_engine, seen) = supervise(paths, impatient());

    assert!(
        !seen
            .iter()
            .any(|status| matches!(status, EngineStatus::Provisioning { .. })),
        "nothing was provisioned, so nothing should say it was: {seen:?}"
    );
}

#[test]
fn a_warm_launch_never_reads_as_setting_itself_up_either() {
    // The same guarantee through the channel the product uses.
    // `engine_status` is the only one the webview has — `status.rs` says
    // so — and it answers from the moment the window opens, before the
    // supervisor has published anything. A `Provisioning` sitting there
    // as the default is a first-run takeover on every warm launch, which
    // the announcement-side test above cannot see.
    let (_uv, paths) = warm_uv(1);
    let (engine, statuses, worker) = supervising(paths, impatient());

    let mut polled = Vec::new();
    let watched = Instant::now();
    loop {
        let status = engine.status();
        let done = matches!(status, EngineStatus::Failed { .. });
        if polled.last() != Some(&status) {
            polled.push(status);
        }
        if done {
            break;
        }
        // Bounded so a supervisor that never reaches `Failed` fails this
        // test instead of hanging the binary, which is the one outcome a
        // test runner cannot report.
        assert!(
            watched.elapsed() < Duration::from_secs(30),
            "the supervisor never failed: {polled:?}"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    engine.shut_down();
    worker.join().expect("the supervisor thread");
    drop(statuses);

    assert!(
        !polled
            .iter()
            .any(|status| matches!(status, EngineStatus::Provisioning { .. })),
        "a warm launch was readable as Provisioning: {polled:?}"
    );
}

#[test]
fn a_provisioning_check_that_never_answers_falls_through_to_the_real_sync() {
    // The other half of "a wedged check must not hold the app": quitting
    // can reach it, and if nobody quits it still has to end. Otherwise the
    // reader sits on "Starting the Engine…" with nothing behind it moving.
    let (_uv, paths) = fake_uv_script("#!/bin/sh\nsleep 30\n");
    let engine = Arc::new(Engine::default());

    let asked = Instant::now();
    let verdict = provisioned(&engine, &paths, Duration::from_millis(50));

    assert_eq!(verdict, Ok(false), "an unanswered check is not a yes");
    assert!(
        asked.elapsed() < Duration::from_secs(5),
        "the launch sat out the wedged check: {:?}",
        asked.elapsed()
    );
    // That the branch also kills the child is deliberately not asserted
    // here. `child_alive()` would answer no whether or not anything was
    // ended — the branch takes the handle before it stops the child — and
    // the process itself cannot be probed either: a fifty-millisecond
    // deadline fires before the shell has run far enough to announce its
    // pid, which is what makes the rest of this test quick. The kill is
    // one line under an `else` that has already returned; what it costs to
    // leave uncovered is smaller than what a timing-dependent probe would
    // cost every run.
}

#[test]
fn a_reader_can_ask_a_supervisor_that_gave_up_to_start_over() {
    let (_uv, paths) = fake_uv(1, 0);
    let (engine, statuses, worker) = supervising(paths, impatient());
    let first = collect_until_failed(&statuses);
    assert!(matches!(first.last(), Some(EngineStatus::Failed { .. })));

    engine.retry();

    let second = collect_until_failed(&statuses);
    assert!(
        second
            .iter()
            .any(|status| matches!(status, EngineStatus::Provisioning { .. })),
        "a retry starts the whole thing again: {second:?}"
    );
    match second.first() {
        Some(EngineStatus::Provisioning { .. }) => {}
        other => panic!("the retry begins at the beginning, got {other:?}"),
    }
    assert!(
        matches!(second.last(), Some(EngineStatus::Failed { .. })),
        "and spends a fresh budget of its own: {second:?}"
    );
    engine.shut_down();
    worker.join().expect("the supervisor thread");
}

/// A recorder that presses Retry the first time it hears `Failed`, from
/// inside the supervisor's own announcement. That is the earliest a
/// reader could possibly press it — the button does not exist until the
/// status has been emitted — and it is the one frame where the park may
/// not be armed yet.
struct EagerRetry {
    engine: Arc<Engine>,
    statuses: Sender<EngineStatus>,
    pressed: std::sync::atomic::AtomicBool,
}

impl Reporter for EagerRetry {
    fn status(&self, status: EngineStatus) {
        let failed = matches!(status, EngineStatus::Failed { .. });
        let _ = self.statuses.send(status);
        if failed && !self.pressed.swap(true, std::sync::atomic::Ordering::SeqCst) {
            self.engine.retry();
        }
    }
}

#[test]
fn a_retry_on_a_working_supervisor_is_not_remembered() {
    // The end-to-end test below cannot see this on its own: arming the
    // park overwrites whatever the field held, so a stale ask would be
    // cleaned up by accident. The gate is what makes `retry` a no-op on
    // its own terms rather than by the good luck of an assignment
    // somewhere else.
    let engine = Engine::default();

    engine.retry();

    assert_eq!(engine.state().park, Park::Running);
}

#[test]
fn a_retry_pressed_before_anything_failed_leaves_the_park_alone() {
    let (_uv, paths) = fake_uv(1, 0);
    let (engine, statuses, worker) = supervising(paths, impatient());
    engine.retry();

    let first = collect_until_failed(&statuses);
    assert!(matches!(first.last(), Some(EngineStatus::Failed { .. })));

    match statuses.recv_timeout(Duration::from_millis(500)) {
        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
        other => panic!("the supervisor parks on `Failed`, got {other:?}"),
    }
    engine.shut_down();
    worker.join().expect("the supervisor thread");
}

#[test]
fn a_retry_pressed_on_the_first_frame_of_the_failure_screen_still_lands() {
    let (_uv, paths) = fake_uv(1, 0);
    let (status_tx, statuses) = channel();
    let engine = Arc::new(Engine::default());
    let supervised = Arc::clone(&engine);
    let pressing = Arc::clone(&engine);
    let worker = std::thread::spawn(move || {
        run(
            supervised,
            paths,
            impatient(),
            Box::new(EagerRetry {
                engine: pressing,
                statuses: status_tx,
                pressed: std::sync::atomic::AtomicBool::new(false),
            }),
        )
    });

    collect_until_failed(&statuses);

    // Provisioning beginning again is the press having landed. Dropped,
    // the supervisor would sit parked on a button nobody can press twice
    // and nothing further would ever be announced.
    let restarted = statuses
        .recv_timeout(Duration::from_secs(5))
        .expect("the retry starts the Engine over");
    assert!(matches!(
        restarted,
        EngineStatus::Provisioning { note: None }
    ));

    engine.shut_down();
    worker.join().expect("the supervisor thread");
}

#[test]
fn quitting_releases_a_supervisor_parked_on_a_failure() {
    let (_uv, paths) = fake_uv(1, 0);
    let (engine, statuses, worker) = supervising(paths, impatient());
    collect_until_failed(&statuses);

    engine.shut_down();

    worker.join().expect("the supervisor thread");
    assert!(engine.quitting());
}

#[test]
fn shutting_down_mid_backoff_ends_the_loop_without_a_failure_screen() {
    let engine = Arc::new(Engine::default());
    let quitting = Arc::clone(&engine);
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(15));
        quitting.shut_down();
    });
    let (status_tx, statuses) = channel();

    let (_uv, paths) = fake_uv(0, 1);
    run(
        Arc::clone(&engine),
        paths,
        RestartPolicy {
            base_delay: Duration::from_secs(30),
            max_delay: Duration::from_secs(30),
            ..impatient()
        },
        Box::new(Recorder {
            statuses: status_tx,
        }),
    );

    let seen: Vec<_> = statuses.try_iter().collect();
    assert!(
        !seen
            .iter()
            .any(|status| matches!(status, EngineStatus::Failed { .. })),
        "quitting is not an Engine failure: {seen:?}"
    );
}

#[test]
fn stopping_kills_the_python_engine_behind_uv_not_just_uv() {
    // `uv run` does not exec: the Engine is a grandchild. The fake
    // prints its grandchild's pid and waits — a kill that reached only
    // the leader would leave that grandchild running, which is exactly
    // the orphaned listener threat model B3 forbids.
    let (_uv, paths) = fake_uv_script("#!/bin/sh\nsleep 30 &\necho $!\nwait\n");
    let mut child = launch::engine_command(&paths, testing::TOKEN)
        .spawn()
        .expect("spawn the fake uv");
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().expect("stdout is piped");
    let mut line = String::new();
    BufReader::new(stdout)
        .read_line(&mut line)
        .expect("the grandchild pid");
    let grandchild: i32 = line.trim().parse().expect("a pid");

    Running::new(child, stdin).stop();

    // SIGKILL is asynchronous; give the kernel a beat.
    let mut gone = false;
    for _ in 0..100 {
        if unsafe { libc::kill(grandchild, 0) } == -1 {
            gone = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(gone, "the group kill must reach the grandchild");
}

#[test]
fn quitting_during_the_provisioning_check_does_not_leave_it_running() {
    // The check runs before the reader has been told anything, which
    // makes it the easiest place in the launch to quit out from under.
    // A `uv` that wedges there has to be the supervisor's child, or
    // `shut_down` returns to a Finder with a process still holding on.
    let (dir, paths) =
        fake_uv_script("#!/bin/sh\necho $$ > \"$(dirname \"$0\")/check.pid\"\nsleep 30\n");
    let (engine, _statuses, worker) = supervising(paths, impatient());

    // Waiting for the OS to schedule a shell, not for anything this
    // supervisor decides, so the ceiling is deliberately far above what
    // the wait costs: the rest of the suite spawns processes on the same
    // cores, and a budget sized to this test running alone fails there
    // for a reason that says nothing about shutting down. Both timed
    // assertions start after this, so a longer ceiling costs them
    // nothing. Measured at four to seven seconds here, so ten was not the
    // headroom it looked like. Twenty stays under both of the thirty-second
    // bounds that keep the check alive to be caught: the fixture's own
    // sleep, and [`PROVISION_CHECK_DEADLINE`].
    let recorded = dir.path().join("check.pid");
    let waited = Instant::now();
    let pid = loop {
        if let Ok(written) = std::fs::read_to_string(&recorded) {
            if let Ok(parsed) = written.trim().parse::<i32>() {
                break parsed;
            }
        }
        assert!(
            waited.elapsed() < Duration::from_secs(20),
            "the check never started: {:?}",
            waited.elapsed()
        );
        std::thread::sleep(Duration::from_millis(10));
    };

    let asked = Instant::now();
    engine.shut_down();
    worker.join().expect("the supervisor thread");

    // Both halves matter. A supervisor that merely waits the check out
    // also ends with the process gone, thirty seconds later, with the
    // app's icon still in the Dock the whole time.
    assert!(
        asked.elapsed() < Duration::from_secs(5),
        "quitting sat out the wedged check: {:?}",
        asked.elapsed()
    );
    assert_eq!(
        unsafe { libc::kill(pid, 0) },
        -1,
        "the check outlived the shutdown that was supposed to reach it"
    );
}

#[test]
fn a_liveness_check_that_reaps_the_child_takes_its_pid_out_of_play() {
    // `kill_group` is raw libc with none of the post-reap guard
    // `Child::kill` carries, so the moment a wait reaps the process its
    // pid must stop being something this supervisor signals — the
    // kernel is free to hand that number to anyone.
    let child = Command::new("/bin/sh")
        .args(["-c", "exit 0"])
        .spawn()
        .expect("spawn");
    let mut running = Running::new(child, None);

    for _ in 0..100 {
        if !running.alive() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }

    assert!(running.reaped, "an exited child must be reaped by `alive`");
    assert!(!running.alive(), "a reaped child is never alive again");
    running.stop();
}

#[test]
fn a_process_spawned_into_a_shutdown_is_handed_back_to_be_stopped() {
    // Closes the race between `shut_down` and a spawn already in
    // flight: whoever loses the adoption still stops the child.
    let engine = Engine::default();
    engine.shut_down();
    let child = Command::new("/bin/sh")
        .args(["-c", "sleep 30"])
        .spawn()
        .expect("spawn");

    let rejected = engine.adopt(Running::new(child, None));

    let Err(running) = rejected else {
        panic!("nobody would ever stop a child adopted after shut_down");
    };
    running.stop();
}

#[test]
fn config_arrives_with_ready_and_leaves_with_everything_else() {
    let engine = Engine::default();
    let (status_tx, _statuses) = channel();
    let recorder = Recorder {
        statuses: status_tx,
    };

    engine.publish_ready(testing::reach(51234), &recorder);
    assert_eq!(engine.status(), EngineStatus::Ready { port: 51234 });
    assert_eq!(engine.config(), Some(testing::reach(51234)));

    engine.publish(
        EngineStatus::Restarting {
            attempt: 1,
            retry_in_ms: 10,
        },
        &recorder,
    );
    assert_eq!(
        engine.config(),
        None,
        "a status with nothing to reach must not leave a stale config behind"
    );
}

#[test]
fn a_fresh_engine_is_not_quitting() {
    assert!(!Engine::default().quitting());
}

#[test]
fn sleeping_out_the_full_duration_reports_no_shutdown() {
    let engine = Engine::default();

    let started = Instant::now();
    assert!(!engine.sleep(Duration::from_millis(50)));
    assert!(started.elapsed() >= Duration::from_millis(50));
}

#[test]
fn shutting_down_cuts_a_sleep_short() {
    // App exit must not wait out a restart backoff.
    let engine = Arc::new(Engine::default());
    let quitter = Arc::clone(&engine);
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(20));
        quitter.shut_down();
    });

    let started = Instant::now();
    assert!(engine.sleep(Duration::from_secs(30)));
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn sleeping_after_shutdown_returns_at_once() {
    let engine = Engine::default();
    engine.shut_down();
    engine.shut_down(); // idempotent

    let started = Instant::now();
    assert!(engine.sleep(Duration::from_secs(30)));
    assert!(started.elapsed() < Duration::from_secs(5));
    assert!(engine.quitting());
}
