#!/usr/bin/env bash
# Puts one release candidate where readers and installed copies look for it,
# and nowhere else: the binaries (the DMG, the update archive, the Linux
# packages) as assets of the mirror's GitHub release, the pages that link
# the DMG on the download site with the apt and dnf metadata under its
# /linux/, and latest.json at the endpoint. Publishing a candidate, not
# announcing one: nothing here makes a tag, a GitHub release or a message
# (those steps are by hand, and the GitHub release comes first).
#
#   scripts/publish-release.sh <version> [--preview]
#
# Reads dist-release/<version>/ as scripts/release-candidate.sh and
# scripts/linux-candidate.sh wrote it and refuses to go on when
#   - the candidate's commit is not on main;
#   - either project's folder would be over Vercel's 100 MB limit, since a
#     deployment that size is refused at upload;
#   - latest.json's signature does not verify against the public key in
#     src-tauri/tauri.conf.json, or names a version or archive other than
#     this candidate's, or an address other than the archive's on the
#     release, since installed copies would fetch it and refuse it, or
#     fetch nothing;
#   - a checksum in candidate.json or candidate-linux.json disagrees with
#     the file beside it;
#   - the Linux candidate is of another commit than the Mac one, or its
#     metadata does not verify against the committed package signing key or
#     does not describe the packages beside it;
#   - the GitHub release v<version> on the public mirror does not exist yet.
#
# Then it uploads the binaries to that GitHub release (the ones already
# there must be these bytes), stages site/download (the landing page with
# this version's name and size written in, the download page linking this
# DMG on the release, SHA256SUMS, the notices, the Linux metadata under
# linux/) and site/updates (latest.json and the archive's signature),
# deploys each folder with the Vercel CLI, fetches what went live, the
# assets included, and compares its checksums with the candidate's. Running
# it again for the same version stages and deploys the same bytes.
#
# `--preview` deploys to preview URLs no reader or installed copy reads, for
# rehearsing with a fake candidate; it uploads nothing to GitHub and fetches
# no binary, and a preview without a Linux candidate rehearses the Mac half
# alone. docs/release.md walks through both.
#
# Needs: minisign (brew install minisign), gpgv (brew install gnupg), gh
# logged in as a user who can write the mirror's releases, the Vercel CLI
# logged in as a member of the team below, and network. No secret: the
# deploy and the upload are authenticated by the CLIs' own logins.
set -euo pipefail

cd "$(dirname "$0")/.."

# ---- inputs -----------------------------------------------------------------

version=${1:-}
mode=${2:-}
case "$version" in
  '' | --*) echo "usage: scripts/publish-release.sh <version> [--preview]" >&2; exit 2 ;;
esac
case "$mode" in
  '') prod=1 ;;
  --preview) prod=0 ;;
  *) echo "unknown option: $mode" >&2; exit 2 ;;
esac

scope=${READILY_VERCEL_SCOPE:-}
test -n "$scope" || { echo "set READILY_VERCEL_SCOPE to the Vercel team slug that owns the two projects" >&2; exit 2; }
download_project=readily-download
updates_project=readily-updates
download_url=https://$download_project.vercel.app
# 100 MB as Vercel counts it for a Hobby deployment.
limit=$((100 * 1000 * 1000))

candidate=dist-release/$version
test -f "$candidate/candidate.json" || { echo "no candidate at $candidate/candidate.json" >&2; exit 1; }
# The Linux half is a release's second artifact, not an optional one: a
# version published for the Mac alone would leave every Linux copy told of a
# version it cannot install. Only a rehearsal may go without it.
linux=$candidate/linux
if test -f "$linux/candidate-linux.json"; then
  with_linux=1
elif (( prod )); then
  echo "no Linux candidate at $linux/candidate-linux.json; run scripts/linux-candidate.sh at the same commit" >&2
  exit 1
else
  with_linux=0
  echo "no Linux candidate at $linux/candidate-linux.json; this preview rehearses the Mac half alone"
fi
# The GitHub release the binaries go on: on the public mirror, named by its
# tag, made by hand before this runs (docs/release.md). Its assets are
# served under this address, and GitHub redirects each to its bytes.
mirror=fahim-m47/readily-public
tag=v$version
release_url=https://github.com/$mirror/releases/download/$tag
for tool in minisign python3 curl shasum bunx; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
if (( prod )); then
  command -v gh >/dev/null || { echo "gh is not installed" >&2; exit 1; }
  gh auth status >/dev/null 2>&1 || { echo "gh is not logged in: run gh auth login" >&2; exit 1; }
fi
if (( with_linux )); then
  for tool in gpgv gpg; do
    command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
  done
fi
# Pinned like every other tool verify runs: a CLI release could change what
# `deploy` prints or `link --yes` does.
vercel() { bunx vercel@59.16.0 "$@"; }
vercel whoami --scope "$scope" >/dev/null 2>&1 || {
  echo "the Vercel CLI is not logged in to $scope: run bunx vercel@59.16.0 login" >&2
  exit 1
}

# Read once, into these shell variables, so the checks below are shell and
# the JSON is parsed in one place.
commit='' dmg='' dmg_sha256='' update_archive='' update_archive_sha256='' update_endpoint=''
fields=$(python3 - "$candidate" "$version" <<'EOF1'
import json, shlex, sys
folder, version = sys.argv[1:3]
c = json.load(open(f"{folder}/candidate.json"))
if c["version"] != version:
    sys.exit(f"candidate.json says version {c['version']}, not {version}")
for key in ("commit", "dmg", "dmg_sha256", "update_archive", "update_archive_sha256", "update_endpoint"):
    print(f"{key}={shlex.quote(c[key])}")
EOF1
) || exit 1
eval "$fields"
# Plain file names: the DMG's goes into the download page's href and a path under
# the candidate folder, so it must be one name, not a path or markup.
for name in "$dmg" "$update_archive"; do
  if [[ ! $name =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "candidate.json names a file that is not a plain file name: $name" >&2
    exit 1
  fi
done
dmg=$candidate/$dmg
archive=$candidate/$update_archive
for file in "$dmg" "$archive" "$archive.sig" "$candidate/latest.json" "$candidate/SHA256SUMS"; do
  test -f "$file" || { echo "missing from the candidate: $file" >&2; exit 1; }
done
# latest.json is served from the root of the updates project. The endpoint
# the candidate was built with must be exactly that file: a copy built with
# any other path would check an address this script never publishes to, and
# stay on that version for good.
updates_url=https://$updates_project.vercel.app
if [[ $update_endpoint != "$updates_url/latest.json" ]]; then
  echo "candidate.json's endpoint $update_endpoint is not $updates_url/latest.json" >&2
  exit 1
fi

# The Linux candidate the same way. Its packages are fetched through
# /linux/pool/<tag>/ on the download site, which site/download/vercel.json
# redirects to the GitHub release of that tag; the metadata was signed over
# that address, so it must be the address this script publishes to.
pool_url=$download_url/linux/pool/$tag
deb='' deb_sha256='' rpm='' rpm_sha256='' linux_commit='' signing_key=''
if (( with_linux )); then
  fields=$(python3 - "$linux" "$version" "$pool_url" <<'EOFL'
import json, shlex, sys
folder, version, pool_url = sys.argv[1:4]
c = json.load(open(f"{folder}/candidate-linux.json"))
if c["version"] != version:
    sys.exit(f"candidate-linux.json says version {c['version']}, not {version}")
if c["arch"] != "x86_64":
    sys.exit(f"candidate-linux.json is for {c['arch']}; Readily ships x86_64 alone")
if c["pool_url"] != pool_url:
    sys.exit(f"candidate-linux.json's metadata fetches packages from {c['pool_url']}, not {pool_url}")
print(f"linux_commit={shlex.quote(c['commit'])}")
for key in ("deb", "deb_sha256", "rpm", "rpm_sha256", "signing_key"):
    print(f"{key}={shlex.quote(c[key])}")
EOFL
  ) || exit 1
  eval "$fields"
  for name in "$deb" "$rpm"; do
    if [[ ! $name =~ ^[A-Za-z0-9._-]+$ ]]; then
      echo "candidate-linux.json names a file that is not a plain file name: $name" >&2
      exit 1
    fi
  done
  if [[ $linux_commit != "$commit" ]]; then
    echo "the Linux candidate is a build of $linux_commit, the Mac one of $commit; a release is one commit" >&2
    exit 1
  fi
  for file in "$linux/$deb" "$linux/$rpm" "$linux/Packages" "$linux/Packages.gz" "$linux/Release" \
    "$linux/InRelease" "$linux/Release.gpg" "$linux/rpm/repodata/repomd.xml" "$linux/rpm/repodata/repomd.xml.asc"; do
    test -f "$file" || { echo "missing from the Linux candidate: $file" >&2; exit 1; }
  done
fi

# ---- refusals ---------------------------------------------------------------

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A release is a commit on main. Checked against origin, since a local main
# can be behind, or ahead of, what the repository has.
git fetch -q origin main
git cat-file -e "$commit^{commit}" 2>/dev/null || { echo "candidate commit $commit is not in this clone" >&2; exit 1; }
if ! git merge-base --is-ancestor "$commit" origin/main; then
  echo "candidate commit $commit is not on origin/main; a release is a build of main" >&2
  exit 1
fi

# An older candidate left in dist-release/ is still a build of main, so it
# would pass the check above and put an older DMG and latest.json in front
# of readers and installed copies. Production refuses a version below the
# one published. The same version again is the redeploy this script allows,
# but only of the same bytes: a rebuild of a published version would give
# new downloads a different build under a name installed copies already
# have, so its signature and SHA256SUMS have to be the published ones.
# A 404 is the first release. Any other failure to read the file is an
# error, not a first release.
if (( prod )); then
  status=$(curl -sS -o "$work/published.json" -w '%{http_code}' "$updates_url/latest.json") || exit 1
  case $status in
    200)
      python3 - "$work/published.json" "$candidate/latest.json" "$version" <<'EOF5' || exit 1
import json, re, sys
published_path, candidate_path, version = sys.argv[1:4]
published = json.load(open(published_path))
def core(v):
    return tuple(int(part) for part in re.split(r"[-+]", v, maxsplit=1)[0].split("."))
if core(version) < core(published["version"]):
    sys.exit(f"{published['version']} is published; {version} is older, and a release never goes backwards")
if published["version"] == version:
    same = published["platforms"] == json.load(open(candidate_path))["platforms"]
    if not same:
        sys.exit(f"{version} is already published with a different archive; a rebuilt version needs a new version number")
EOF5
      if [[ $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$work/published.json") == "$version" ]]; then
        curl -fsS "$download_url/SHA256SUMS" -o "$work/published.sums" || exit 1
        cmp -s "$work/published.sums" "$candidate/SHA256SUMS" || {
          echo "$version is already published with a different DMG; a rebuilt version needs a new version number" >&2
          exit 1
        }
      fi
      ;;
    404) ;;
    *) echo "could not read the published latest.json ($status)" >&2; exit 1 ;;
  esac
fi

# The files still are what candidate.json says they are.
check_sum() {
  local actual
  actual=$(shasum -a 256 "$1" | cut -d' ' -f1)
  if [[ $actual != "$2" ]]; then
    echo "$1 has checksum $actual, but the candidate recorded $2" >&2
    exit 1
  fi
}
check_sum "$dmg" "$dmg_sha256"
check_sum "$archive" "$update_archive_sha256"
# SHA256SUMS is published on the download site for a reader to check a DMG
# against, so it must name these two files at these hashes and nothing else: not one of
# them twice, not a third file, not a hash candidate.json disagrees with.
python3 - "$candidate/SHA256SUMS" "$(basename "$dmg")" "$dmg_sha256" \
  "$(basename "$archive")" "$update_archive_sha256" <<'EOFSUMS' || exit 1
import sys
path, dmg, dmg_sha256, archive, archive_sha256 = sys.argv[1:]
expected = {dmg: dmg_sha256, archive: archive_sha256}
found = {}
lines = open(path).read().splitlines()
for line in lines:
    parts = line.split()
    if len(parts) != 2:
        sys.exit(f"{path} has a line that is not '<sha256>  <file>': {line.rstrip()}")
    found[parts[1].lstrip("*")] = parts[0]
# Two lines as well as two names: a name listed twice would collapse into one.
if len(lines) != 2 or found != expected:
    sys.exit(f"{path} must name exactly {sorted(expected)} at the hashes candidate.json records")
EOFSUMS

# The signature in latest.json is what an installed Readily checks the
# archive against, with the key compiled into it. A file that fails here
# would be fetched by every installed copy and refused by every one.
# The key is read from the commit that was built, which is the key the build
# compiled in. A rehearsal swaps a throwaway key into the working copy, so
# only a preview reads the working copy: a production run after a rehearsal
# that was never put back would otherwise verify against the wrong key.
if (( prod )); then
  git show "$commit:src-tauri/tauri.conf.json" > "$work/tauri.conf.json"
else
  cp src-tauri/tauri.conf.json "$work/tauri.conf.json"
fi
pubkey=$(python3 - "$candidate" "$version" "$update_archive" "$release_url" "$work/archive.sig" "$work/tauri.conf.json" <<'EOF2'
import base64, json, sys
folder, version, archive, release_url, sig_out, config_path = sys.argv[1:7]
latest_path = f"{folder}/latest.json"
latest = json.load(open(latest_path))
if latest["version"] != version:
    sys.exit(f"latest.json offers version {latest['version']}, not {version}")
if sorted(latest["platforms"]) != ["darwin-aarch64", "linux-x86_64"]:
    sys.exit(f"latest.json names platforms {sorted(latest['platforms'])}; Readily ships darwin-aarch64 and linux-x86_64")
# A Linux copy reads the version from this file and updates through its
# package manager (ADR 0016): no archive, no signature, and a url that is
# the page telling a reader so. Anything else here would be an archive the
# shell would try to install.
linux = latest["platforms"]["linux-x86_64"]
if linux != {"signature": "", "url": "https://readily-download.vercel.app/linux/"}:
    sys.exit(f"latest.json's linux-x86_64 entry is {linux}; it must carry no signature and point at the Linux page")
# The archive is fetched from the release, as an asset named after it.
platform = latest["platforms"]["darwin-aarch64"]
if platform["url"] != f"{release_url}/{archive}":
    sys.exit(f"latest.json points installed copies at {platform['url']}, not at {release_url}/{archive}")
# Tauri stores the whole minisign signature file, base64-encoded. The .sig
# is published beside latest.json too, so it has to be the same signature.
if open(f"{folder}/{archive}.sig").read().strip() != platform["signature"].strip():
    sys.exit(f"{archive}.sig is not the signature latest.json carries")
open(sig_out, "wb").write(base64.b64decode(platform["signature"]))
config = json.load(open(config_path))
print(base64.b64decode(config["plugins"]["updater"]["pubkey"]).decode().splitlines()[1])
EOF2
)
minisign -Vq -m "$archive" -x "$work/archive.sig" -P "$pubkey" || {
  echo "latest.json's signature does not verify against the key in src-tauri/tauri.conf.json" >&2
  exit 1
}

# The Linux metadata the same way: what a reader's apt or dnf does, against
# the package signing key committed at site/download/linux/readily.asc,
# read from the built commit for the reason above. The metadata is what
# readers trust, so it must describe the packages beside it at their
# checksums, and nothing else.
if (( with_linux )); then
  check_sum "$linux/$deb" "$deb_sha256"
  check_sum "$linux/$rpm" "$rpm_sha256"
  if (( prod )); then
    git show "$commit:site/download/linux/readily.asc" > "$work/readily.asc"
    # The working copy is what gets served, beside metadata signed with the
    # key at the built commit; a key rotated on main since then would leave
    # every reader with a signature their imported key cannot check.
    if ! cmp -s "$work/readily.asc" site/download/linux/readily.asc; then
      echo "site/download/linux/readily.asc changed since $commit; the served key must be the one the metadata was signed with" >&2
      exit 1
    fi
  else
    cp site/download/linux/readily.asc "$work/readily.asc"
  fi
  gpg --dearmor --output "$work/keyring.gpg" "$work/readily.asc"
  key_fingerprint=$(gpg --show-keys --with-colons "$work/readily.asc" 2>/dev/null | awk -F: '$1 == "fpr" { print $10 }')
  if [[ $key_fingerprint != "$signing_key" ]]; then
    echo "candidate-linux.json was signed by $signing_key, but site/download/linux/readily.asc is $key_fingerprint" >&2
    exit 1
  fi
  for signed in InRelease "Release.gpg Release" "rpm/repodata/repomd.xml.asc rpm/repodata/repomd.xml"; do
    # shellcheck disable=SC2086
    (cd "$linux" && gpgv --keyring "$work/keyring.gpg" $signed 2>/dev/null) || {
      echo "$linux/${signed%% *} does not verify against site/download/linux/readily.asc" >&2
      exit 1
    }
  done
  python3 - "$linux" "$version" "$deb" "$deb_sha256" "$rpm" "$rpm_sha256" "$tag" "$pool_url" <<'EOFM' || exit 1
import gzip, hashlib, os, sys
import xml.etree.ElementTree as ET

folder, version, deb, deb_sha256, rpm, rpm_sha256, tag, pool_url = sys.argv[1:9]

def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

# apt: one stanza in Packages, for this .deb at its hash, fetched from the
# pool; Packages.gz is that index; Release names both at their hashes.
packages = open(f"{folder}/Packages").read()
stanzas = [s for s in packages.split("\n\n") if s.strip()]
if len(stanzas) != 1:
    sys.exit(f"Packages holds {len(stanzas)} stanzas, not one")
fields = dict(line.split(": ", 1) for line in stanzas[0].splitlines() if not line.startswith(" "))
expected = {
    "Package": "readily",
    "Version": version,
    "Architecture": "amd64",
    "Filename": f"linux/pool/{tag}/{deb}",
    "Size": str(os.path.getsize(f"{folder}/{deb}")),
    "SHA256": deb_sha256,
}
for key, value in expected.items():
    if fields.get(key) != value:
        sys.exit(f"Packages says {key}: {fields.get(key)!r}, not {value!r}")
if gzip.open(f"{folder}/Packages.gz").read() != packages.encode():
    sys.exit("Packages.gz is not Packages")
release = open(f"{folder}/Release").read()
for index in ("Packages", "Packages.gz"):
    line = f" {sha256(f'{folder}/{index}')} {os.path.getsize(f'{folder}/{index}')} {index}"
    if line not in release.splitlines():
        sys.exit(f"Release does not name {index} at its hash and size")
# InRelease is Release, clearsigned: the signature verified above, the
# text must be this Release.
inrelease = open(f"{folder}/InRelease").read()
body = inrelease.split("-----BEGIN PGP SIGNATURE-----")[0].split("\n\n", 1)[1]
if body.replace("\n- ", "\n").rstrip("\n") != release.rstrip("\n"):
    sys.exit("InRelease does not carry this Release")

# dnf: every index repomd.xml names (primary, filelists, other) is present
# at its hash, since the signature covers repomd.xml alone; primary lists
# one package, this .rpm at its hash, based at the pool.
ns = {"repo": "http://linux.duke.edu/metadata/repo", "common": "http://linux.duke.edu/metadata/common"}
repomd = ET.parse(f"{folder}/rpm/repodata/repomd.xml").getroot()
indexes = {}
for data in repomd.findall("repo:data", ns):
    href = data.find("repo:location", ns).get("href")
    path = f"{folder}/rpm/{href}"
    if not os.path.isfile(path):
        sys.exit(f"repomd.xml names {href}, which is missing from the candidate")
    checksum = data.find("repo:checksum", ns)
    if checksum.get("type") != "sha256" or checksum.text != sha256(path):
        sys.exit(f"repomd.xml's hash of {href} is not the file's")
    indexes[data.get("type")] = (href, path)
if "primary" not in indexes:
    sys.exit("repomd.xml names no primary index")
href, primary_path = indexes["primary"]
packages = ET.parse(gzip.open(primary_path)).getroot().findall("common:package", ns)
if len(packages) != 1:
    sys.exit(f"{href} lists {len(packages)} packages, not one")
package = packages[0]
location = package.find("common:location", ns)
pkg_checksum = package.find("common:checksum", ns)
pkg_version = package.find("common:version", ns)
found = {
    "name": package.findtext("common:name", namespaces=ns),
    "arch": package.findtext("common:arch", namespaces=ns),
    "version": pkg_version.get("ver"),
    "release": pkg_version.get("rel"),
    "base": location.get("{http://www.w3.org/XML/1998/namespace}base"),
    "href": location.get("href"),
    "checksum type": pkg_checksum.get("type"),
    "checksum": pkg_checksum.text,
    "size": package.find("common:size", ns).get("package"),
}
expected = {
    "name": "readily",
    "arch": "x86_64",
    "version": version,
    "release": "1",
    "base": pool_url,
    "href": rpm,
    "checksum type": "sha256",
    "checksum": rpm_sha256,
    "size": str(os.path.getsize(f"{folder}/{rpm}")),
}
for key, value in expected.items():
    if found[key] != value:
        sys.exit(f"{href} says {key} {found[key]!r}, not {value!r}")
EOFM
fi

# The binaries go on the mirror's GitHub release of this tag, which is made
# by hand (docs/release.md) and must exist before the pages, the metadata
# and latest.json that point at it go live. A draft is not published, so
# its asset addresses would 404 for readers and installed copies.
if (( prod )); then
  draft=$(gh release view "$tag" -R "$mirror" --json isDraft -q .isDraft 2>/dev/null) || {
    echo "no GitHub release $tag on $mirror; make it from the mirror's tag first (docs/release.md)" >&2
    exit 1
  }
  if [[ $draft != false ]]; then
    echo "GitHub release $tag on $mirror is a draft; publish it first" >&2
    exit 1
  fi
fi

# ---- staging ----------------------------------------------------------------

# A deployment is everything in the folder, tracked or not, so the folder
# is cleared down to its tracked files (and the link `vercel` keeps in
# `.vercel/`) before staging: the deployment is this candidate alone, never
# the last run's files or a stray one. Downloads in flight are not cut off
# by it: the binaries are served from the release, which stays.
clear_staging() {
  local entry
  # `find`, not a glob: no glob names an entry that starts with two dots.
  # A package signing key exported but not yet committed stays too: it is
  # the only copy of the public half outside the keyring.
  while IFS= read -r -d '' entry; do
    case $(basename "$entry") in .vercel | readily.asc) continue ;; esac
    git ls-files --error-unmatch "$entry" >/dev/null 2>&1 || rm -rf "$entry"
  done < <(find "$1" -mindepth 1 -maxdepth 1 -print0)
}
clear_staging site/download
clear_staging site/download/linux
clear_staging site/updates

# Everything tracked under site/ is deployed as the working copy has it, and
# this script rewrites the pages. An edit already in the working copy or the
# index would be published under this version, or thrown away by the preview
# restore below, so it has to be committed or discarded.
if [[ -n $(git status --porcelain -- site) ]]; then
  echo "site/ has uncommitted changes; commit or discard them before publishing" >&2
  exit 1
fi
# A release is published by main: the pages, the Vercel configuration and
# this script itself all come from the checkout, so in production the
# checkout is origin/main exactly, with no tracked change on top. A branch
# could otherwise put an unreviewed page, or an unreviewed version of this
# script, in front of readers.
if (( prod )); then
  if [[ $(git rev-parse HEAD) != $(git rev-parse origin/main) ]]; then
    echo "HEAD is not origin/main; a release is published from main" >&2
    exit 1
  fi
  if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
    echo "the tree has uncommitted tracked changes; a release is published from main as committed" >&2
    exit 1
  fi
fi
# A rehearsal must not leave a fake version in the pages, so in preview mode
# the checkout's copies come back whatever happens next. The check above
# refused uncommitted changes under site/, so this restores only the rewrite.
if (( ! prod )); then
  trap 'rm -rf "$work"; git checkout -q -- site/download' EXIT
fi

# No binary is staged: the DMG and the archive are assets on the release,
# where the pages and latest.json point. SHA256SUMS stays on the download
# site for a reader to check a DMG against.
cp "$candidate/SHA256SUMS" site/download/
# The notices of the commit that was built, not of the working tree.
git show "$commit:THIRD-PARTY-NOTICES" > site/download/THIRD-PARTY-NOTICES.txt
cp "$archive.sig" "$candidate/latest.json" site/updates/
# The Linux metadata, not the packages: those are fetched from GitHub
# through the pool redirect in site/download/vercel.json. The key beside
# the metadata is the committed one, already there.
if (( with_linux )); then
  cp "$linux/Packages" "$linux/Packages.gz" "$linux/Release" "$linux/InRelease" "$linux/Release.gpg" site/download/linux/
  mkdir -p site/download/linux/rpm
  cp -R "$linux/rpm/repodata" site/download/linux/rpm/
fi

# The landing page names the version and size; the download page links the
# DMG at its place on the release.
dmg_url=$release_url/$(basename "$dmg")
python3 - "$version" "$dmg_url" "$(stat -f %z "$dmg")" <<'EOF3'
import re, sys
version, dmg_url, size = sys.argv[1:4]
edits = {
    "index.html": {
        r'(<span data-release="version">)[^<]*(</span>)': version,
        r'(<span data-release="size">)[^<]*(</span>)': f"{int(size) / 1_000_000:.0f} MB",
    },
    "download.html": {
        r'(href=")[^"]*(" data-release="dmg")': dmg_url,
    },
}
for name, page_edits in edits.items():
    path = f"site/download/{name}"
    page = open(path).read()
    for pattern, value in page_edits.items():
        # A function, so nothing in the value is read as a group reference.
        page, count = re.subn(pattern, lambda m, v=value: m.group(1) + v + m.group(2), page)
        if count != 1:
            sys.exit(f"expected one match for {pattern} in {path}, found {count}")
    open(path, "w").write(page)
EOF3

for folder in site/download site/updates; do
  total=$(find "$folder" -type f -not -path '*/.vercel/*' -print0 | xargs -0 stat -f %z | awk '{ s += $1 } END { print s }')
  if (( total > limit )); then
    echo "$folder totals $total bytes, over the $limit-byte limit a deployment allows" >&2
    exit 1
  fi
done

# ---- deploy -----------------------------------------------------------------

# Links the folder to its project when this checkout has not been, checks
# the link names that project and no other (the DMG under the update
# endpoint is the one mistake with no recovery), then deploys and prints
# the deployment URL. The CLI uploads what is in the folder, ignored files
# included; the gitignores keep the binaries out of the repository, not out
# of the deployment. On a terminal the CLI prints the URL bare; piped, as
# here, it printed a JSON document in rehearsal, so both are read.
deploy() {
  local folder=$1 project=$2 output
  vercel link --yes --project "$project" --scope "$scope" --cwd "$folder" >/dev/null
  # `link` also writes a short-lived Vercel token into .env.local. The CLI
  # does not upload that file, and nothing here reads it, so it goes.
  rm -f "$folder/.env.local"
  python3 - "$folder/.vercel/project.json" "$project" <<'EOF4'
import json, sys
path, project = sys.argv[1:3]
linked = json.load(open(path)).get("projectName")
if linked != project:
    sys.exit(f"{path} links to {linked!r}, not {project!r}")
EOF4
  if (( prod )); then
    output=$(vercel deploy --prod --yes --cwd "$folder")
  else
    output=$(vercel deploy --yes --cwd "$folder")
  fi
  printf '%s' "$output" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
try:
    print(json.loads(raw)["deployment"]["url"])
except (ValueError, KeyError, TypeError):
    urls = re.findall(r"https://\S+\.vercel\.app", raw)
    if not urls:
        sys.exit("no deployment URL in: " + raw)
    print(urls[0])
'
}
# The binaries go up first: once the pages, latest.json and the metadata
# are live, readers, installed copies and package managers follow them to
# the release, and the assets must be there. An asset already on the
# release has to be these bytes, since an installed copy would otherwise
# fetch an archive the signature in latest.json refuses, and a package
# manager one the signed hashes refuse; GitHub keeps the first upload, so
# the way out is a new version number.
upload_asset() {
  local file=$1 sha256=$2 name actual
  name=$(basename "$file")
  if gh release download "$tag" -R "$mirror" --pattern "$name" --output "$work/asset" --clobber 2>/dev/null; then
    actual=$(shasum -a 256 "$work/asset" | cut -d' ' -f1)
    if [[ $actual != "$sha256" ]]; then
      echo "$mirror release $tag already carries a $name with checksum $actual, not this candidate's $sha256; a rebuilt version needs a new version number" >&2
      exit 1
    fi
  else
    gh release upload "$tag" -R "$mirror" "$file"
  fi
}
if (( prod )); then
  upload_asset "$dmg" "$dmg_sha256"
  upload_asset "$archive" "$update_archive_sha256"
  if (( with_linux )); then
    upload_asset "$linux/$deb" "$deb_sha256"
    upload_asset "$linux/$rpm" "$rpm_sha256"
  fi
fi

download_deployment=$(deploy site/download "$download_project")
updates_deployment=$(deploy site/updates "$updates_project")

# ---- verify what went live --------------------------------------------------

# Production is read at the addresses readers and installed copies use. A
# preview sits behind Vercel Authentication, so it is read through the CLI,
# which mints a bypass token for the project the folder is linked to.
fetch() {
  local folder=$1 base=$2 deployment=$3 path=$4 out=$5
  if (( prod )); then
    curl -fsSL "$base/$path" -o "$out"
  else
    (cd "$folder" && vercel curl "/$path" --deployment "$deployment" --yes) > "$out"
  fi
}
# The binaries at their addresses on the release, in production: a preview
# uploaded none. `curl -L` follows GitHub's redirect to the bytes, as the
# updater and a reader's browser do.
if (( prod )); then
  curl -fsSL "$dmg_url" -o "$work/live.dmg"
  check_sum "$work/live.dmg" "$dmg_sha256"
  curl -fsSL "$release_url/$update_archive" -o "$work/live.tar.gz"
  check_sum "$work/live.tar.gz" "$update_archive_sha256"
fi
# SHA256SUMS is the integrity signal; the landing page is only checked for the version it names.
fetch site/download "$download_url" "$download_deployment" "" "$work/live.html"
if ! grep -qF "data-release=\"version\">$version<" "$work/live.html"; then
  echo "the landing page now served does not name version $version" >&2
  exit 1
fi
fetch site/download "$download_url" "$download_deployment" download.html "$work/live-download.html"
if ! grep -qF "href=\"$dmg_url\" data-release=\"dmg\"" "$work/live-download.html"; then
  echo "the download page now served does not link to the candidate DMG" >&2
  exit 1
fi
fetch site/download "$download_url" "$download_deployment" SHA256SUMS "$work/live.sums"
cmp -s "$work/live.sums" "$candidate/SHA256SUMS" || {
  echo "the SHA256SUMS now served is not the candidate's" >&2
  exit 1
}
fetch site/download "$download_url" "$download_deployment" THIRD-PARTY-NOTICES.txt "$work/live.notices"
cmp -s "$work/live.notices" site/download/THIRD-PARTY-NOTICES.txt || {
  echo "the THIRD-PARTY-NOTICES.txt now served is not the commit's" >&2
  exit 1
}
fetch site/updates "$updates_url" "$updates_deployment" "$update_archive.sig" "$work/live.sig"
cmp -s "$work/live.sig" "$archive.sig" || {
  echo "the $update_archive.sig now served is not the candidate's" >&2
  exit 1
}
fetch site/updates "$updates_url" "$updates_deployment" latest.json "$work/live.json"
cmp -s "$work/live.json" "$candidate/latest.json" || {
  echo "the latest.json now served is not the candidate's" >&2
  exit 1
}
# Every Linux metadata file as staged, byte for byte, plus the key readers
# import; in production the packages too, through the pool redirect apt
# and dnf will follow, at the checksums the metadata signs over.
if (( with_linux )); then
  while IFS= read -r -d '' staged; do
    path=${staged#site/download/}
    fetch site/download "$download_url" "$download_deployment" "$path" "$work/live.linux"
    cmp -s "$work/live.linux" "$staged" || {
      echo "the $path now served is not the candidate's" >&2
      exit 1
    }
  done < <(find site/download/linux -type f -not -name index.html -print0)
  if (( prod )); then
    curl -fsSL "$pool_url/$deb" -o "$work/live.deb"
    check_sum "$work/live.deb" "$deb_sha256"
    curl -fsSL "$pool_url/$rpm" -o "$work/live.rpm"
    check_sum "$work/live.rpm" "$rpm_sha256"
  fi
fi

echo
echo "published Readily $version ($commit)"
if (( prod )); then
  echo "  download site:   $download_url"
  echo "  update endpoint: $update_endpoint"
  echo "  binaries:        https://github.com/$mirror/releases/tag/$tag"
  echo
  echo "site/download now names this release; commit that on its own:"
  git --no-pager diff --stat -- site/download
else
  echo "  preview page:    $download_deployment"
  echo "  preview updates: $updates_deployment"
fi
