//! The one shape the webview reads about updates: whether there is a newer
//! Readily, and how the install it asked for is going.

use serde::Serialize;

/// Where the update sits, as the UI sees it.
///
/// Readable at any time through the `update_status` command, which the
/// webview polls — there is no update event, so there is nothing for a
/// screen to subscribe to late. Four states and no more, because the page
/// is only ever asked one question: is there a newer Readily, and does the
/// reader want it now.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "camelCase")]
pub enum UpdateStatus {
    /// Nothing to offer. The state before the launch check answers, the
    /// state when this build is the newest one published, and the state
    /// when the check could not be made at all.
    ///
    /// A check that fails is deliberately indistinguishable from a check
    /// that found nothing. The reader did not ask for it — it happens on
    /// its own, seconds after launch — so a network that was down has
    /// nothing to say to them. It is logged, and the next launch asks
    /// again.
    #[default]
    Idle,
    /// A newer Readily is published. `notes` is the release's own prose,
    /// straight from `latest.json`, and may be absent.
    Available {
        version: String,
        notes: Option<String>,
    },
    /// The reader said yes. The download, the signature check and the
    /// swap are all under way; the app restarts itself when they finish,
    /// so there is no success state to report — the process is gone.
    Installing,
    /// The install the reader asked for did not happen. `untouched` is
    /// whether this build is still the one on disk: true when the download,
    /// its signature check or the bundle's own version check failed, all of
    /// which come before anything is moved, and false when the swap itself
    /// failed — the plugin moves the running bundle aside before the new
    /// one lands, so an error there can leave no Readily to relaunch.
    Failed { reason: String, untouched: bool },
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The webview switches on `state` and reads the rest, so the wire
    /// shape is a contract, not an implementation detail.
    #[test]
    fn the_wire_shape_is_a_tagged_object() {
        let json = |status: UpdateStatus| serde_json::to_string(&status).expect("serialize");

        assert_eq!(json(UpdateStatus::Idle), r#"{"state":"idle"}"#);
        assert_eq!(
            json(UpdateStatus::Available {
                version: "0.2.0".into(),
                notes: None
            }),
            r#"{"state":"available","version":"0.2.0","notes":null}"#
        );
        assert_eq!(
            json(UpdateStatus::Available {
                version: "0.2.0".into(),
                notes: Some("Fixes the export panel.".into())
            }),
            r#"{"state":"available","version":"0.2.0","notes":"Fixes the export panel."}"#
        );
        assert_eq!(json(UpdateStatus::Installing), r#"{"state":"installing"}"#);
        assert_eq!(
            json(UpdateStatus::Failed {
                reason: "nope".into(),
                untouched: true
            }),
            r#"{"state":"failed","reason":"nope","untouched":true}"#
        );
    }

    /// Nothing has been checked when the window opens, and nothing to
    /// offer is what the shell must show until something has.
    #[test]
    fn the_default_offers_nothing() {
        assert_eq!(UpdateStatus::default(), UpdateStatus::Idle);
    }
}
