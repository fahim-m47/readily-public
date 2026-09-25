#!/usr/bin/env bash
# Puts one release candidate where readers and installed copies look for it,
# and nowhere else: the DMG on the download page, the update archive at the
# endpoint. Publishing a candidate, not announcing one: nothing here makes a
# tag, a GitHub release or a message (those steps are by hand).
#
#   scripts/publish-release.sh <version> [--preview]
#
# Reads dist-release/<version>/ as scripts/release-candidate.sh wrote it and
# refuses to go on when
#   - the candidate's commit is not on main;
#   - the DMG or the archive is over Vercel's 100 MB limit, or either
#     project's folder would be, since a deployment that size is refused at
#     upload;
#   - latest.json's signature does not verify against the public key in
#     src-tauri/tauri.conf.json, or names a version, archive or host other
#     than this candidate's, since installed copies would fetch it and refuse
#     it, or fetch nothing;
#   - a checksum in candidate.json disagrees with the file beside it.
#
# Then it stages site/download (the landing page with this version's name
# and size written in, the download page linking this DMG, the DMG,
# SHA256SUMS, the notices) and site/updates
# (latest.json, archive, signature), deploys each folder with the Vercel CLI,
# fetches what went live and compares its checksums with candidate.json.
# Running it again for the same version stages and deploys the same bytes.
#
# `--preview` deploys to preview URLs no reader or installed copy reads, for
# rehearsing with a fake candidate. docs/release.md walks through both.
#
# Needs: minisign (brew install minisign), the Vercel CLI logged in as a
# member of the team below, and network. No secret: the deploy is
# authenticated by the CLI's own login.
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
for tool in minisign python3 curl shasum bunx; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
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
# latest.json is served from the root of the updates project, the archive
# beside it. The endpoint the candidate was built with must be exactly that
# file: a copy built with any other path would check an address this script
# never publishes to, and stay on that version for good.
updates_url=https://$updates_project.vercel.app
if [[ $update_endpoint != "$updates_url/latest.json" ]]; then
  echo "candidate.json's endpoint $update_endpoint is not $updates_url/latest.json" >&2
  exit 1
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
    echo "$1 has checksum $actual, but candidate.json recorded $2" >&2
    exit 1
  fi
}
check_sum "$dmg" "$dmg_sha256"
check_sum "$archive" "$update_archive_sha256"
# SHA256SUMS is published beside the DMG for a reader to check against, so
# it must name these two files at these hashes and nothing else: not one of
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

# Vercel refuses a file over the limit at upload, after the rest went up.
for file in "$dmg" "$archive"; do
  size=$(stat -f %z "$file")
  if (( size > limit )); then
    echo "$file is $size bytes, over the $limit-byte limit a deployment allows" >&2
    exit 1
  fi
done

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
pubkey=$(python3 - "$candidate" "$version" "$update_archive" "$updates_url" "$work/archive.sig" "$work/tauri.conf.json" <<'EOF2'
import base64, json, sys
folder, version, archive, updates_url, sig_out, config_path = sys.argv[1:7]
latest_path = f"{folder}/latest.json"
latest = json.load(open(latest_path))
if latest["version"] != version:
    sys.exit(f"latest.json offers version {latest['version']}, not {version}")
if list(latest["platforms"]) != ["darwin-aarch64"]:
    sys.exit(f"latest.json names platforms {sorted(latest['platforms'])}; Readily ships darwin-aarch64 alone")
platform = latest["platforms"]["darwin-aarch64"]
if platform["url"] != f"{updates_url}/{archive}":
    sys.exit(f"latest.json points installed copies at {platform['url']}, not at {updates_url}/{archive}")
# Tauri stores the whole minisign signature file, base64-encoded. The .sig
# beside the archive is published too, so it has to be the same signature.
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

# ---- staging ----------------------------------------------------------------

# A deployment is everything in the folder, tracked or not, so the folder
# is cleared down to its tracked files (and the link `vercel` keeps in
# `.vercel/`) before staging: the deployment is this candidate alone, never
# the last run's files or a stray one. That means a download or an update in
# flight when the next release goes up gets a 404 and starts over: the Hobby
# plan's 100 MB deployment cap leaves no room for the previous DMG or archive
# beside the new one.
clear_staging() {
  local entry
  # `find`, not a glob: no glob names an entry that starts with two dots.
  while IFS= read -r -d '' entry; do
    [[ $(basename "$entry") == .vercel ]] && continue
    git ls-files --error-unmatch "$entry" >/dev/null 2>&1 || rm -rf "$entry"
  done < <(find "$1" -mindepth 1 -maxdepth 1 -print0)
}
clear_staging site/download
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

cp "$dmg" "$candidate/SHA256SUMS" site/download/
# The notices of the commit that was built, not of the working tree.
git show "$commit:THIRD-PARTY-NOTICES" > site/download/THIRD-PARTY-NOTICES.txt
cp "$archive" "$archive.sig" "$candidate/latest.json" site/updates/

# The landing page names the version and size; the download page links the DMG.
python3 - "$version" "$(basename "$dmg")" "$(stat -f %z "$dmg")" <<'EOF3'
import re, sys
version, dmg, size = sys.argv[1:4]
edits = {
    "index.html": {
        r'(<span data-release="version">)[^<]*(</span>)': version,
        r'(<span data-release="size">)[^<]*(</span>)': f"{int(size) / 1_000_000:.0f} MB",
    },
    "download.html": {
        r'(href=")[^"]*(" data-release="dmg")': f"/{dmg}",
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
fetch site/download "$download_url" "$download_deployment" "$(basename "$dmg")" "$work/live.dmg"
check_sum "$work/live.dmg" "$dmg_sha256"
# SHA256SUMS beside the DMG is the integrity signal; the landing page is only checked for the version it names.
fetch site/download "$download_url" "$download_deployment" "" "$work/live.html"
if ! grep -qF "data-release=\"version\">$version<" "$work/live.html"; then
  echo "the landing page now served does not name version $version" >&2
  exit 1
fi
fetch site/download "$download_url" "$download_deployment" download.html "$work/live-download.html"
if ! grep -qF "href=\"/$(basename "$dmg")\" data-release=\"dmg\"" "$work/live-download.html"; then
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
fetch site/updates "$updates_url" "$updates_deployment" "$update_archive" "$work/live.tar.gz"
check_sum "$work/live.tar.gz" "$update_archive_sha256"
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

echo
echo "published Readily $version ($commit)"
if (( prod )); then
  echo "  download site:   $download_url"
  echo "  update endpoint: $update_endpoint"
  echo
  echo "site/download now names this release; commit that on its own:"
  git --no-pager diff --stat -- site/download
else
  echo "  preview page:    $download_deployment"
  echo "  preview updates: $updates_deployment"
fi
