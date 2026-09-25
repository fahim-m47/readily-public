//! The version carried by a signed macOS update archive.

use std::io::{self, Read};
use std::path::Path;

use flate2::read::GzDecoder;
use semver::Version;
use serde::Deserialize;

#[derive(Deserialize)]
struct BundleInfo {
    #[serde(rename = "CFBundleShortVersionString")]
    version: String,
}

/// Refuses a signed archive unless its bundle is newer than the running
/// copy. The endpoint's manifest version cannot answer this: unlike these
/// bytes, `latest.json` is not covered by the updater signature. The rule
/// is the plugin's own default comparator — strictly newer — applied to
/// the signed bytes instead of the manifest, so the two never disagree on
/// what counts as an update.
///
/// `product` is the running bundle's name without `.app`. The bundler names
/// the archive's top-level folder after it, so a renamed product would
/// refuse every update rather than install one: closed, not open.
pub(super) fn require_newer(bytes: &[u8], product: &str, running: &Version) -> Result<(), String> {
    let info_plist = format!("{product}.app/Contents/Info.plist");
    let contents = read_entry(bytes, Path::new(&info_plist))
        .map_err(|_| "The downloaded update is not a valid app archive.".to_owned())?
        .ok_or_else(|| format!("The downloaded update has no {info_plist}."))?;
    let version = plist::from_bytes::<BundleInfo>(&contents)
        .ok()
        .and_then(|info| Version::parse(&info.version).ok())
        .ok_or_else(|| "The downloaded update's Info.plist has no valid version.".to_owned())?;
    if version <= *running {
        return Err(format!(
            "The downloaded update is version {version}, which is not newer than this copy ({running})."
        ));
    }
    Ok(())
}

/// The first entry at `wanted` in a gzipped tar, or `None` when there is
/// no such entry.
fn read_entry(bytes: &[u8], wanted: &Path) -> io::Result<Option<Vec<u8>>> {
    let mut archive = tar::Archive::new(GzDecoder::new(bytes));
    for entry in archive.entries()? {
        let mut entry = entry?;
        if entry.path()? != wanted {
            continue;
        }
        let mut contents = Vec::new();
        entry.read_to_end(&mut contents)?;
        return Ok(Some(contents));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use flate2::{write::GzEncoder, Compression};

    use super::*;

    const PRODUCT: &str = "Readily";

    fn running() -> Version {
        Version::parse("1.0.0").expect("running version")
    }

    fn archive_with(path: &str, contents: &[u8]) -> Vec<u8> {
        let encoder = GzEncoder::new(Vec::new(), Compression::default());
        let mut archive = tar::Builder::new(encoder);
        let mut header = tar::Header::new_gnu();
        header.set_size(contents.len() as u64);
        header.set_mode(0o644);
        archive
            .append_data(&mut header, path, contents)
            .expect("append entry");
        let encoder = archive.into_inner().expect("finish tar");
        encoder.finish().expect("finish gzip")
    }

    fn update_archive(version: &str) -> Vec<u8> {
        let plist = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleShortVersionString</key>
  <string>{version}</string>
</dict>
</plist>"#
        );
        archive_with(
            &format!("{PRODUCT}.app/Contents/Info.plist"),
            plist.as_bytes(),
        )
    }

    /// Whatever version the manifest claimed to make the plugin offer the
    /// archive, only the signed bundle's own version decides the install.
    #[test]
    fn an_older_bundle_is_refused() {
        assert_eq!(
            require_newer(&update_archive("0.9.0"), PRODUCT, &running()),
            Err("The downloaded update is version 0.9.0, which is not newer than this copy (1.0.0).".into())
        );
    }

    #[test]
    fn the_same_bundle_is_refused() {
        assert!(require_newer(&update_archive("1.0.0"), PRODUCT, &running()).is_err());
    }

    #[test]
    fn a_newer_bundle_is_accepted() {
        assert_eq!(
            require_newer(&update_archive("1.0.1"), PRODUCT, &running()),
            Ok(())
        );
    }

    #[test]
    fn an_archive_without_the_bundle_is_refused() {
        let elsewhere = archive_with("Other.app/Contents/Info.plist", b"<plist/>");
        assert_eq!(
            require_newer(&elsewhere, PRODUCT, &running()),
            Err("The downloaded update has no Readily.app/Contents/Info.plist.".into())
        );
    }

    #[test]
    fn bytes_that_are_not_an_archive_are_refused() {
        assert!(require_newer(b"not a tarball", PRODUCT, &running()).is_err());
    }

    #[test]
    fn a_bundle_without_a_semver_version_is_refused() {
        let bad = archive_with(
            &format!("{PRODUCT}.app/Contents/Info.plist"),
            br#"<plist version="1.0"><dict><key>CFBundleShortVersionString</key><string>next</string></dict></plist>"#,
        );
        assert!(require_newer(&bad, PRODUCT, &running()).is_err());
    }
}
