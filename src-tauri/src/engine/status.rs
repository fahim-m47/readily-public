//! The two shapes the webview reads: what the Engine is doing, and how
//! to reach it.

use serde::Serialize;

/// The Engine's state as the UI sees it.
///
/// Readable at any time through the `engine_status` command, which the
/// webview polls — there is no status event, so there is nothing for a
/// screen to subscribe to late. `Failed` is the surfaced
/// end of the restart policy: the Engine will not come up and nothing is
/// still trying.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "camelCase")]
pub enum EngineStatus {
    /// `uv sync --locked` is building the Engine environment (ADR 0001 §6).
    ///
    /// `note` is the last line `uv` wrote, republished as it arrives. The
    /// build is minutes long and has no total to count against — the
    /// wheels are resolved as it goes — so the honest thing to show is
    /// not a bar but evidence that something is still moving. It is `uv`'s
    /// own words, so a screen showing it must keep it subordinate to a
    /// sentence a reader can actually read.
    Provisioning { note: Option<String> },
    /// A launch is under way; `attempt` is 1 for the first.
    Starting { attempt: u32 },
    /// Answering health checks on this loopback port.
    Ready { port: u16 },
    /// The last run ended and another launch is queued.
    #[serde(rename_all = "camelCase")]
    Restarting { attempt: u32, retry_in_ms: u64 },
    /// The supervisor has stopped trying. Nothing restarts from here
    /// unless a reader asks, through `engine_retry`.
    Failed { reason: String },
}

/// A supervisor that has published nothing yet has a window open and an
/// Engine on the way, which is `Starting` and nothing stronger.
///
/// Not `Provisioning`, though deciding whether to provision is literally
/// the first thing it does. `engine_status` answers from the moment the
/// window opens, and the shell reads `Provisioning` as proof this machine
/// has never had a working Engine — its cue to take the whole screen over
/// for the first-run explanation. A default that says it would put that
/// screen in front of every reader on every launch, for as long as the
/// offline check takes, before anything had looked at the disk.
impl Default for EngineStatus {
    fn default() -> Self {
        Self::Starting { attempt: 1 }
    }
}

/// How the webview reaches the Engine.
///
/// Both halves belong to one launch: a restarted Engine gets a new port
/// *and* a new token, so a leaked token dies with the process that used
/// it. Handed over in-process through Tauri's IPC, which is the whole
/// point — the token never touches argv, a URL, or disk (threat model B3).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineConfig {
    pub port: u16,
    pub token: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The webview switches on `state` and reads the rest, so the wire
    /// shape is a contract, not an implementation detail.
    #[test]
    fn the_wire_shape_is_a_tagged_object() {
        let json = |status: EngineStatus| serde_json::to_string(&status).expect("serialize");

        assert_eq!(
            json(EngineStatus::Provisioning { note: None }),
            r#"{"state":"provisioning","note":null}"#
        );
        assert_eq!(
            json(EngineStatus::Provisioning {
                note: Some("Resolved 142 packages".into())
            }),
            r#"{"state":"provisioning","note":"Resolved 142 packages"}"#
        );
        assert_eq!(
            json(EngineStatus::Ready { port: 51234 }),
            r#"{"state":"ready","port":51234}"#
        );
        assert_eq!(
            json(EngineStatus::Restarting {
                attempt: 2,
                retry_in_ms: 1000
            }),
            r#"{"state":"restarting","attempt":2,"retryInMs":1000}"#
        );
        assert_eq!(
            json(EngineStatus::Failed {
                reason: "nope".into()
            }),
            r#"{"state":"failed","reason":"nope"}"#
        );
    }
}
