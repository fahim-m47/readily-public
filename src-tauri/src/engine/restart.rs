//! When an Engine run ends, how long to wait before the next one — and when
//! to stop waiting and surface the failure instead.

use std::time::Duration;

/// The supervisor's reaction to an Engine run that ended.
#[derive(Clone, Copy, Debug)]
pub struct RestartPolicy {
    /// Consecutive failed runs tolerated before the failure is surfaced.
    pub max_consecutive: u32,
    /// Wait before the first retry; each further retry doubles it.
    pub base_delay: Duration,
    /// Ceiling on the doubling.
    pub max_delay: Duration,
    /// A run that answered health checks at least this long clears the
    /// budget. An Engine that crashes once an hour should keep being
    /// restarted; one that crash-loops should not.
    pub healthy_reset_after: Duration,
}

/// Counts consecutive failed runs and turns them into a backoff or a stop.
#[derive(Debug)]
pub struct Restarts {
    policy: RestartPolicy,
    consecutive: u32,
}

impl Restarts {
    pub fn new(policy: RestartPolicy) -> Self {
        Self {
            policy,
            consecutive: 0,
        }
    }

    /// Records that a run ended. `healthy_for` is how long it answered
    /// health checks, or `None` if it never came up. Returns how long to
    /// wait before starting the Engine again, or `None` when the budget is
    /// spent and the failure should be surfaced instead of retried.
    pub fn record_run_ended(&mut self, healthy_for: Option<Duration>) -> Option<Duration> {
        if healthy_for.is_some_and(|held| held >= self.policy.healthy_reset_after) {
            self.consecutive = 0;
        }
        self.consecutive += 1;
        if self.consecutive > self.policy.max_consecutive {
            return None;
        }
        let doublings = self.consecutive - 1;
        let delay = self
            .policy
            .base_delay
            .checked_mul(1u32.checked_shl(doublings).unwrap_or(u32::MAX))
            .unwrap_or(self.policy.max_delay);
        Some(delay.min(self.policy.max_delay))
    }

    /// How many consecutive failed runs stand right now — the attempt
    /// number the UI shows while restarting.
    pub fn consecutive(&self) -> u32 {
        self.consecutive
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> RestartPolicy {
        RestartPolicy {
            max_consecutive: 3,
            base_delay: Duration::from_millis(100),
            max_delay: Duration::from_millis(300),
            healthy_reset_after: Duration::from_secs(60),
        }
    }

    #[test]
    fn backoff_doubles_then_holds_at_the_ceiling() {
        let mut restarts = Restarts::new(policy());

        assert_eq!(
            restarts.record_run_ended(None),
            Some(Duration::from_millis(100))
        );
        assert_eq!(
            restarts.record_run_ended(None),
            Some(Duration::from_millis(200))
        );
        assert_eq!(
            restarts.record_run_ended(None),
            Some(Duration::from_millis(300)),
            "the doubling is capped by max_delay"
        );
    }

    #[test]
    fn repeated_failure_stops_retrying() {
        let mut restarts = Restarts::new(policy());
        for _ in 0..3 {
            assert!(restarts.record_run_ended(None).is_some());
        }

        assert_eq!(
            restarts.record_run_ended(None),
            None,
            "past the budget the supervisor must surface the failure, not loop"
        );
    }

    #[test]
    fn a_run_that_stayed_healthy_clears_the_budget() {
        let mut restarts = Restarts::new(policy());
        for _ in 0..3 {
            restarts.record_run_ended(None);
        }

        let after_a_good_run = restarts.record_run_ended(Some(Duration::from_secs(90)));

        assert_eq!(
            after_a_good_run,
            Some(Duration::from_millis(100)),
            "an Engine that ran fine for a while earns a fresh budget"
        );
        assert_eq!(restarts.consecutive(), 1);
    }

    #[test]
    fn a_short_healthy_run_does_not_clear_the_budget() {
        let mut restarts = Restarts::new(policy());
        restarts.record_run_ended(None);

        let next = restarts.record_run_ended(Some(Duration::from_secs(2)));

        assert_eq!(next, Some(Duration::from_millis(200)));
        assert_eq!(restarts.consecutive(), 2);
    }
}
