#!/usr/bin/env bash
# Builds one signed, notarized, stapled release candidate and writes down what
# it is. Preparing an artifact, not publishing one: nothing here touches a
# tag, a release or a website (publishing is scripts/publish-release.sh's job).
#
#   scripts/release-candidate.sh
#
# Needs, on this Mac only — CI holds no signing secrets:
#   - the Developer ID Application identity in the login keychain;
#   - the App Store Connect API key at ~/.private_keys/AuthKey_<id>.p8;
#   - APPLE_API_KEY and APPLE_API_ISSUER in the environment, or in
#     .env.release beside this script's parent (gitignored).
# docs/release.md walks through provisioning them.
#
# Also needs, for over-the-air updates:
#   - the updater's minisign private key, and its password, in
#     TAURI_SIGNING_PRIVATE_KEY and TAURI_SIGNING_PRIVATE_KEY_PASSWORD.
#     Keep them in a password manager; copy them into .env.release for a
#     build and let them stay out of the repo.
#   - a real update endpoint in src-tauri/tauri.conf.json. The placeholder
#     `.invalid` host is refused below rather than shipped.
#
# Output lands in dist-release/<version>/: the .dmg, the update archive and
# its signature, latest.json for the update endpoint (naming the archive at
# its place on the mirror's GitHub release), SHA256SUMS, and candidate.json
# naming the commit, version, checksums and notary submissions.
#
# Optional: RELEASE_NOTES_FILE names a file whose text becomes the update's
# notes — what the reader is shown when Readily offers them this version.
set -euo pipefail

cd "$(dirname "$0")/.."

# ---- inputs -----------------------------------------------------------------

if [[ -f .env.release ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.release
  set +a
fi

: "${APPLE_API_KEY:?set APPLE_API_KEY to the App Store Connect key id}"
: "${APPLE_API_ISSUER:?set APPLE_API_ISSUER to the App Store Connect issuer id}"
export APPLE_API_KEY_PATH=${APPLE_API_KEY_PATH:-$HOME/.private_keys/AuthKey_$APPLE_API_KEY.p8}
test -r "$APPLE_API_KEY_PATH" || { echo "no API key at $APPLE_API_KEY_PATH" >&2; exit 1; }
# The same key the bundler notarizes with, handed to notarytool directly for
# this script's own submissions, so there is one credential and no profile.
notary=(--key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER")

# The one Developer ID Application identity in the keychain. Resolved rather
# than hard-coded so the certificate's subject — a legal name — is not
# written into the repository.
if [[ -z ${APPLE_SIGNING_IDENTITY:-} ]]; then
  identities=$(security find-identity -v -p codesigning)
  APPLE_SIGNING_IDENTITY=$(sed -n 's/.*"\(Developer ID Application: .*\)"$/\1/p' <<<"$identities")
fi
case $(grep -c . <<<"$APPLE_SIGNING_IDENTITY") in
  1) ;;
  0) echo "no Developer ID Application identity in the keychain" >&2; exit 1 ;;
  *) echo "more than one Developer ID Application identity; set APPLE_SIGNING_IDENTITY to one:" >&2
     echo "$APPLE_SIGNING_IDENTITY" >&2; exit 1 ;;
esac
export APPLE_SIGNING_IDENTITY
team=$(sed -n 's/.*(\(.*\))$/\1/p' <<<"$APPLE_SIGNING_IDENTITY")

# The updater signs the release archive with a key the repo never holds. A
# build without it produces no signature, and an update with no signature is
# one no installed Readily will accept, so this is an input, not an option.
: "${TAURI_SIGNING_PRIVATE_KEY:?set TAURI_SIGNING_PRIVATE_KEY to the updater minisign key, as a path or as its contents}"
: "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:?set TAURI_SIGNING_PRIVATE_KEY_PASSWORD to the password for that key}"
export TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD

# Where installed copies of this build will look for their next update, read
# out of the config the build is about to compile in. The placeholder host is
# refused here rather than in review: a candidate carrying it would check an
# address that resolves nowhere, for as long as that copy exists, and no
# later release could reach it.
endpoint=$(python3 - <<'EOF1'
import json, sys, urllib.parse
config = json.load(open("src-tauri/tauri.conf.json"))
updater = config.get("plugins", {}).get("updater", {})
endpoints = updater.get("endpoints", [])
if len(endpoints) != 1:
    sys.exit("tauri.conf.json must name exactly one update endpoint")
url = urllib.parse.urlparse(endpoints[0])
if url.scheme != "https":
    sys.exit(f"update endpoint is not https: {endpoints[0]}")
if url.hostname is None or url.hostname.endswith(".invalid"):
    sys.exit(
        f"update endpoint is still the placeholder ({endpoints[0]}); "
        "point it at the release host before building a candidate"
    )
if endpoints[0] != "https://readily-updates.vercel.app/latest.json":
    sys.exit(
        f"update endpoint is {endpoints[0]}, not the updates project's "
        "latest.json that scripts/publish-release.sh serves"
    )
if not updater.get("pubkey"):
    sys.exit("tauri.conf.json names no updater pubkey")
if not config.get("bundle", {}).get("createUpdaterArtifacts"):
    sys.exit("bundle.createUpdaterArtifacts is off, so no update archive is built")
print(endpoints[0])
EOF1
)

# A candidate is a build of a commit, so the tree has to be one. Untracked
# files count: the bundle takes `engine/src`, `catalog/licenses` and
# `catalog/references` as whole directories, so a file that was never added
# would ship under a commit that lacks it.
# (This script's own output lives in the ignored `dist-release/`.)
if [[ -n $(git status --porcelain) ]]; then
  echo "working tree has uncommitted or untracked files; a candidate names a commit" >&2
  git status --short >&2
  exit 1
fi
commit=$(git rev-parse HEAD)
# `verify` runs this too, but a candidate can be built from a commit that
# never went through verify, and a DMG whose Engine says a different
# version from its shell is not a candidate.
scripts/check-version.sh
version=$(python3 -c 'import json;print(json.load(open("src-tauri/tauri.conf.json"))["version"])')
arch=aarch64

# ---- build ------------------------------------------------------------------

# `tauri build` runs prepare-bundle.sh (which signs uv because
# APPLE_SIGNING_IDENTITY is set), signs the .app with the hardened runtime and
# Entitlements.plist, notarizes and staples it, then builds and signs the .dmg.
# The .dmg is asked for here rather than in tauri.conf.json, so a plain
# `bun tauri build` (the smoke lane, CI) stays an .app and does not spend
# minutes styling a disk image nobody will open.
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
bun install --frozen-lockfile
bun tauri build --bundles app,dmg

app=src-tauri/target/release/bundle/macos/Readily.app
dmg=src-tauri/target/release/bundle/dmg/Readily_${version}_${arch}.dmg
# The update archive is the same .app the .dmg carries, gzipped, with a
# detached minisign signature. It is what an installed Readily downloads.
archive=src-tauri/target/release/bundle/macos/Readily.app.tar.gz
test -d "$app" || { echo "no app bundle at $app" >&2; exit 1; }
test -f "$dmg" || { echo "no disk image at $dmg" >&2; exit 1; }
test -f "$archive" || { echo "no update archive at $archive" >&2; exit 1; }
test -f "$archive.sig" || { echo "no signature at $archive.sig" >&2; exit 1; }

scripts/check-bundle.sh "$app/Contents/Resources"

# The bundler signs the .dmg but does not notarize it. Stapling the image as
# well as the app means Gatekeeper can pass the download itself offline,
# before anything is mounted.
notary_dmg=$(xcrun notarytool submit "$dmg" "${notary[@]}" --wait --output-format json)
grep -q '"status": *"Accepted"' <<<"$notary_dmg" || {
  echo "$notary_dmg" >&2; echo "dmg notarization not accepted" >&2; exit 1; }
xcrun stapler staple "$dmg"

# ---- verify -----------------------------------------------------------------

# Each check is one thing a reader's Mac will decide on its own: the signature
# holds through every nested file, the executables carry the hardened
# runtime, the tickets are stapled, and Gatekeeper accepts both the app and
# the image with no allowance for a machine that built them.
# The signing details are captured before they are searched: `grep -q`
# closes the pipe on its first match, codesign dies of SIGPIPE, and under
# pipefail a passing check would fail the script.
signed_by_us() { # signed_by_us <path> — hardened runtime, and our team's Developer ID
  local info
  info=$(codesign --display --verbose=2 "$1" 2>&1)
  grep -q 'flags=.*runtime' <<<"$info" || { echo "$1 lacks the hardened runtime" >&2; return 1; }
  grep -q "^TeamIdentifier=$team\$" <<<"$info" || { echo "$1 is not signed by team $team" >&2; return 1; }
}
codesign --verify --deep --strict --verbose=2 "$app"
signed_by_us "$app"
signed_by_us "$app/Contents/Resources/uv"
xcrun stapler validate "$app"
xcrun stapler validate "$dmg"
spctl --assess --type execute --verbose=2 "$app"
spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg"

# The archive is checked as the bytes a reader's Readily will unpack, not as
# the folder it was made from: the bundler tars the app after signing it,
# and a tar that dropped an attribute or a symlink would carry a bundle that
# looks signed here and fails Gatekeeper on the reader's Mac after the swap.
unpacked=$(mktemp -d)
trap 'rm -rf "$unpacked"' EXIT
tar -xzf "$archive" -C "$unpacked"
inner=$unpacked/Readily.app
test -d "$inner" || { echo "the update archive does not hold Readily.app at its root" >&2; exit 1; }
codesign --verify --deep --strict --verbose=2 "$inner"
signed_by_us "$inner"
signed_by_us "$inner/Contents/Resources/uv"
xcrun stapler validate "$inner"
spctl --assess --type execute --verbose=2 "$inner"

# The one thing Gatekeeper cannot answer: whether this archive was signed by
# the key whose public half is compiled into this very build. Signing with
# the wrong key produces a perfectly valid signature that every installed
# Readily rejects, and the only way to recover is a fresh download — so it is
# checked before the candidate exists, not after it ships. Minisign's key id
# is bytes 2..10 of both the public key and the signature.
python3 - "$archive.sig" <<'EOF3'
import base64, json, sys

def key_id(line: str) -> bytes:
    return base64.b64decode(line)[2:10]

pubkey = json.load(open("src-tauri/tauri.conf.json"))["plugins"]["updater"]["pubkey"]
published = base64.b64decode(pubkey).decode().splitlines()[1]
# Tauri's .sig is the base64 of the whole minisign signature file.
signed = base64.b64decode(open(sys.argv[1]).read()).decode().splitlines()[1]
if key_id(signed) != key_id(published):
    sys.exit(
        "the update archive was signed by a different key than the one this "
        "build trusts; no installed Readily would accept it"
    )
EOF3

# ---- record -----------------------------------------------------------------

out=dist-release/$version
mkdir -p "$out"
cp "$dmg" "$out/"
# Stamped with the version on the way out. The bundler names every build's
# archive the same thing; this name is the asset's on the release for this
# tag, which publish-release.sh checks latest.json against.
update=Readily_${version}_${arch}.app.tar.gz
cp "$archive" "$out/$update"
cp "$archive.sig" "$out/$update.sig"
(cd "$out" && shasum -a 256 "$(basename "$dmg")" "$update" > SHA256SUMS)
dmg_sha=$(awk -v name="$(basename "$dmg")" '$2 == name {print $1}' "$out/SHA256SUMS")
update_sha=$(awk -v name="$update" '$2 == name {print $1}' "$out/SHA256SUMS")

# What the endpoint serves. Uploading this file is what makes the release
# reachable by installed copies, and scripts/publish-release.sh is where that
# happens — nothing here publishes it.
#
# The archive is an asset of the mirror's GitHub release v<version>, not a
# file beside latest.json: at 0.2.0 it passed the 100 MB a Vercel Hobby
# deployment allows, so it lives where the Linux packages already do, and
# scripts/publish-release.sh uploads it there and checks this is the url
# named here. The updater follows GitHub's redirect to the asset's bytes.
python3 - "$out/latest.json" "$out/$update.sig" "${RELEASE_NOTES_FILE:-}" <<EOF4
import datetime, json, sys

out, signature, notes_file = sys.argv[1:4]
url = "https://github.com/fahim-m47/readily-public/releases/download/v$version/$update"
json.dump(
    {
        "version": "$version",
        "notes": open(notes_file).read().strip() if notes_file else "",
        "pub_date": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "platforms": {
            "darwin-aarch64": {"signature": open(signature).read().strip(), "url": url},
            # Linux copies update through their package manager, not through
            # this file (ADR 0016). The entry is here so an installed copy
            # learns a new version exists; it reads the version and never
            # fetches the url, which names the repository's page.
            "linux-x86_64": {"signature": "", "url": "https://readily-download.vercel.app/linux/"},
        },
    },
    open(out, "w"),
    indent=2,
)
EOF4
# The bundler notarized the .app itself, as Readily.zip, and printed nothing
# this script can parse; the newest such submission since the build started
# is the record of it. `stapler validate` above is the proof; this is the id
# to look the ticket up by.
history=$(xcrun notarytool history "${notary[@]}" --output-format json)
app_notary=$(python3 -c '
import json,sys
since=sys.argv[1]
h=json.load(sys.stdin)["history"]
ids=[e["id"] for e in h if e["name"]=="Readily.zip" and e["createdDate"]>=since]
if not ids: sys.exit("no Readily.zip notarization submitted since " + since)
print(ids[0])' "$started" <<<"$history")
dmg_notary=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])' <<<"$notary_dmg")

python3 - "$out/candidate.json" <<EOF2
import json,sys
json.dump({
  "version": "$version",
  "commit": "$commit",
  "arch": "$arch",
  "dmg": "$(basename "$dmg")",
  "dmg_sha256": "$dmg_sha",
  "update_archive": "$update",
  "update_archive_sha256": "$update_sha",
  "update_endpoint": "$endpoint",
  "signing_identity_team": "$team",
  "notarization": {"app": "$app_notary", "dmg": "$dmg_notary"},
  "gatekeeper": "accepted",
}, open(sys.argv[1], "w"), indent=2)
EOF2

echo
echo "candidate $version at $commit"
cat "$out/candidate.json"
