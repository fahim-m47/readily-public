#!/usr/bin/env bash
# Builds the Linux half of a release candidate: the .deb and .rpm on the
# Linux machine, and the package repository metadata that lets apt and dnf
# install and update them, signed here. Preparing an artifact, not
# publishing one: nothing here touches GitHub or a website (that is
# scripts/publish-release.sh's job, and it wants this and the Mac candidate
# built from the same commit).
#
#   scripts/linux-candidate.sh
#
# Needs READILY_LINUX_HOST=user@host (see CONTRIBUTING.md): the box that
# `bun run verify:linux` uses, with the packaging tools CONTRIBUTING.md lists.
# The committed tree is copied there with rsync and built with `bun tauri
# build`, the packages are opened and checked the way ci.yml's smoke-linux
# checks them, and `createrepo_c` (fetched by uvx, pinned) writes the dnf
# metadata. The apt metadata is a Packages file and a Release file this
# script writes itself.
#
# Needs on this Mac: gpg, holding the secret half of the key whose public
# half is committed at site/download/linux/readily.asc. That is the key
# readers' package managers trust, so the metadata is signed with it and
# nothing else; docs/release.md's one-time setup makes it. The packages
# themselves are not signed: apt and dnf check each package against the
# checksum in the signed metadata, and the key never leaves this Mac.
#
# Output lands in dist-release/<version>/linux/:
#   readily_<version>_amd64.deb, readily-<version>-1.x86_64.rpm
#   Packages, Packages.gz, Release, InRelease, Release.gpg   the apt repository
#   rpm/repodata/                                            the dnf repository
#   candidate-linux.json   commit, version, checksums, signing key
set -euo pipefail

cd "$(dirname "$0")/.."

# ---- inputs -----------------------------------------------------------------

HOST=${READILY_LINUX_HOST:-}
if [[ -z $HOST ]]; then
  echo "READILY_LINUX_HOST is unset. Point it at a Linux machine with ssh key auth (user@host); see CONTRIBUTING.md." >&2
  exit 2
fi
# Its own checkout on the box, beside verify:linux's: a candidate is a build
# of a clean commit, and the verify copy is whatever working tree was last
# synced.
DIR=${READILY_LINUX_RELEASE_DIR:-readily-release}
for tool in gpg gpgv rsync ssh shasum python3; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done

# Where the packages will be fetched from once published. The pool path is
# a redirect in site/download/vercel.json to the GitHub release of the same
# version on the public mirror, and both package managers follow it. Written
# into the metadata here, so the signature covers the address readers use.
download_url=https://readily-download.vercel.app
pool_url=$download_url/linux/pool

# A candidate is a build of a commit, so the tree has to be one; untracked
# files count, as in scripts/release-candidate.sh.
if [[ -n $(git status --porcelain) ]]; then
  echo "working tree has uncommitted or untracked files; a candidate names a commit" >&2
  git status --short >&2
  exit 1
fi
commit=$(git rev-parse HEAD)
version=$(scripts/check-version.sh)
deb=readily_${version}_amd64.deb
rpm=readily-${version}-1.x86_64.rpm

# The key readers trust is the committed one, and the clean-tree check above
# means the working copy is the committed copy. One key, with its secret
# half in this Mac's keyring; the metadata is signed by exactly that key.
pubkey=site/download/linux/readily.asc
test -f "$pubkey" || {
  echo "no package signing key at $pubkey; docs/release.md's one-time setup makes it" >&2
  exit 1
}
fingerprints=$(gpg --show-keys --with-colons "$pubkey" 2>/dev/null | awk -F: '$1 == "fpr" { print $10 }')
case $(grep -c . <<<"$fingerprints") in
  1) fingerprint=$fingerprints ;;
  0) echo "$pubkey holds no OpenPGP public key" >&2; exit 1 ;;
  *) echo "$pubkey holds more than one key; readers import one" >&2; exit 1 ;;
esac
gpg --list-secret-keys "$fingerprint" >/dev/null 2>&1 || {
  echo "the secret half of $fingerprint ($pubkey) is not in this Mac's keyring" >&2
  exit 1
}

if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null; then
  echo "Cannot reach $HOST over ssh with key auth. Set READILY_LINUX_HOST." >&2
  exit 2
fi
remote() { ssh -o BatchMode=yes "$HOST" "export PATH=\$HOME/.cargo/bin:\$HOME/.local/bin:\$HOME/.bun/bin:\$PATH; cd $DIR && $1"; }

# ---- build, on the box ------------------------------------------------------

# The commit itself goes over, from `git archive` rather than the working
# tree, so nothing gitignored here (prepared resources, a staged DMG)
# reaches the build. --delete removes whatever is there and not in the
# commit; the excludes keep the box's caches, and dist-linux is this
# script's own output there.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf '\033[1m── copying %s to %s:%s\033[0m\n' "${commit:0:12}" "$HOST" "$DIR"
mkdir "$work/tree"
git archive "$commit" | tar -x -C "$work/tree"
rsync -a --delete --exclude node_modules --exclude src-tauri/target \
  --exclude 'engine/.venv*' --exclude dist-linux \
  "$work/tree/" "$HOST:$DIR/" || { echo "rsync failed" >&2; exit 2; }

# What the box needs beyond verify:linux: rpm2cpio and cpio to open the
# .rpm, uvx for createrepo_c.
# shellcheck disable=SC2016
remote '
  pkg-config --exists webkit2gtk-4.1 || { echo "webkit2gtk4.1-devel and friends are not installed; see CONTRIBUTING.md"; exit 1; }
  for tool in ffmpeg rpm2cpio cpio uvx bun cargo python3; do
    command -v $tool >/dev/null || { echo "$tool is not installed on the Linux machine"; exit 1; }
  done'

# `--no-sign` as ci.yml builds it: the Linux build makes no update archive
# for the updater to check (ADR 0016), and the minisign key stays on this Mac.
printf '\033[1m── building the .deb and .rpm on %s\033[0m\n' "$HOST"
remote "bun install --frozen-lockfile && bun tauri build --no-sign --bundles deb,rpm"

# Tauri names each bundle after productName (Readily_…, Readily-…); the
# package inside is `readily`, so the file takes the package's name, as
# dpkg-deb and rpmbuild would have named it.
out=dist-linux/$version
unpacked=dist-linux/$version.unpacked
remote "rm -rf $out $unpacked && mkdir -p $out $unpacked/rpm &&
  cp src-tauri/target/release/bundle/deb/*_${version}_amd64.deb $out/$deb &&
  cp src-tauri/target/release/bundle/rpm/*-${version}-1.x86_64.rpm $out/$rpm &&
  cp $out/$rpm $unpacked/rpm/"

# Opens the .deb the way dpkg would and writes the apt index from its
# control file: one Packages stanza, with the file's name, size and hashes
# added, and a Release file naming the index's hashes. apt resolves
# `Filename` from the archive root, which is the download site, so the pool
# path is written out in full.
printf '\033[1m── indexing the .deb\033[0m\n'
ssh -o BatchMode=yes "$HOST" "cd $DIR && python3 - $out/$deb $unpacked/deb $out $version" <<'EOF1'
import datetime, gzip, hashlib, io, os, sys, tarfile

deb_path, unpack_dir, out, version = sys.argv[1:5]

def ar_members(data: bytes):
    """Yield (name, bytes) for each member of a GNU ar archive, as a .deb is."""
    if not data.startswith(b"!<arch>\n"):
        sys.exit(f"{deb_path} is not an ar archive")
    at = 8
    while at < len(data):
        header = data[at:at + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58])
        at += 60
        yield name, data[at:at + size]
        at += size + (size & 1)

members = dict(ar_members(open(deb_path, "rb").read()))
if members.get("debian-binary", b"").strip() != b"2.0":
    sys.exit(f"{deb_path} is not a version 2.0 .deb")
control_tar = next((members[n] for n in members if n.startswith("control.tar")), None)
data_tar = next((members[n] for n in members if n.startswith("data.tar")), None)
if control_tar is None or data_tar is None:
    sys.exit(f"{deb_path} lacks control.tar or data.tar")

# Tauri writes the member as `control`, dpkg-deb as `./control`.
with tarfile.open(fileobj=io.BytesIO(control_tar)) as tar:
    member = next(m for m in tar if os.path.normpath(m.name) == "control")
    control = tar.extractfile(member).read().decode()
with tarfile.open(fileobj=io.BytesIO(data_tar)) as tar:
    tar.extractall(unpack_dir, filter="data")

fields = dict(
    line.split(": ", 1) for line in control.splitlines() if line and not line.startswith(" ")
)
expected = {"Package": "readily", "Version": version, "Architecture": "amd64"}
for key, value in expected.items():
    if fields.get(key) != value:
        sys.exit(f"{deb_path}'s control says {key}: {fields.get(key)!r}, not {value!r}")

deb = open(deb_path, "rb").read()
packages = control.rstrip("\n") + "\n" + "".join(
    f"{key}: {value}\n"
    for key, value in (
        ("Filename", f"linux/pool/v{version}/{os.path.basename(deb_path)}"),
        ("Size", len(deb)),
        ("MD5sum", hashlib.md5(deb).hexdigest()),
        ("SHA1", hashlib.sha1(deb).hexdigest()),
        ("SHA256", hashlib.sha256(deb).hexdigest()),
    )
) + "\n"
packages_bytes = packages.encode()
# mtime=0 so the same index gzips to the same bytes.
gz = io.BytesIO()
with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as f:
    f.write(packages_bytes)
packages_gz = gz.getvalue()
open(f"{out}/Packages", "wb").write(packages_bytes)
open(f"{out}/Packages.gz", "wb").write(packages_gz)

indexes = {"Packages": packages_bytes, "Packages.gz": packages_gz}
release = (
    "Origin: Readily\n"
    "Label: Readily\n"
    "Architectures: amd64\n"
    f"Date: {datetime.datetime.now(datetime.timezone.utc):%a, %d %b %Y %H:%M:%S UTC}\n"
    "Description: Readily for Debian and Ubuntu on x86_64\n"
)
for name, algorithm in (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256")):
    release += f"{name}:\n" + "".join(
        f" {hashlib.new(algorithm, body).hexdigest()} {len(body)} {index}\n"
        for index, body in indexes.items()
    )
open(f"{out}/Release", "w").write(release)
EOF1

# Both packages carry what a first run needs, checked as the installed tree
# a reader gets: the .deb's data.tar above, the .rpm's cpio here.
printf '\033[1m── checking what the packages carry\033[0m\n'
remote "mkdir -p $unpacked/rpm-root && (cd $unpacked/rpm-root && rpm2cpio ../rpm/$rpm | cpio -idm --quiet) &&
  scripts/check-bundle.sh $unpacked/deb/usr/lib/Readily &&
  scripts/check-bundle.sh $unpacked/rpm-root/usr/lib/Readily"

# The dnf repository: createrepo_c reads the .rpm and writes repodata/, with
# the package's location based at the pool so the metadata can live on the
# download site while the package lives on GitHub. Gzip for primary and the
# rest (the default is zstd) and plain file names, so publish-release.sh
# can read primary.xml.gz back with Python's standard library.
printf '\033[1m── writing the dnf repository\033[0m\n'
remote "mkdir -p $out/rpm && uvx --from createrepo_c==1.2.4.post1 createrepo_c --quiet \
  --baseurl $pool_url/v$version --simple-md-filenames --no-database \
  --general-compress-type gz --outputdir $out/rpm $unpacked/rpm"

printf '\033[1m── copying the candidate back\033[0m\n'
dest=dist-release/$version/linux
rm -rf "$dest"
mkdir -p "$dest"
rsync -a "$HOST:$DIR/$out/" "$dest/" || { echo "rsync failed" >&2; exit 2; }

# ---- sign, here -------------------------------------------------------------

# InRelease and Release.gpg are the two forms apt reads; repomd.xml.asc is
# what dnf reads with repo_gpgcheck. Each is a signature over the hashes of
# the indexes, which carry the hashes of the packages.
sign() { gpg --batch --yes --local-user "$fingerprint!" --digest-algo SHA256 "$@"; }
sign --clearsign --output "$dest/InRelease" "$dest/Release"
sign --detach-sign --armor --output "$dest/Release.gpg" "$dest/Release"
sign --detach-sign --armor --output "$dest/rpm/repodata/repomd.xml.asc" "$dest/rpm/repodata/repomd.xml"

# Verified the way a reader's apt does: gpgv, against the committed public
# key and nothing else in this Mac's keyring.
gpg --dearmor --output "$work/keyring.gpg" "$pubkey"
gpgv --keyring "$work/keyring.gpg" "$dest/InRelease" 2>/dev/null
gpgv --keyring "$work/keyring.gpg" "$dest/Release.gpg" "$dest/Release" 2>/dev/null
gpgv --keyring "$work/keyring.gpg" "$dest/rpm/repodata/repomd.xml.asc" "$dest/rpm/repodata/repomd.xml" 2>/dev/null

# ---- record -----------------------------------------------------------------

deb_sha=$(shasum -a 256 "$dest/$deb" | cut -d' ' -f1)
rpm_sha=$(shasum -a 256 "$dest/$rpm" | cut -d' ' -f1)
python3 - "$dest/candidate-linux.json" <<EOF2
import json, sys
json.dump({
  "version": "$version",
  "commit": "$commit",
  "arch": "x86_64",
  "deb": "$deb",
  "deb_sha256": "$deb_sha",
  "rpm": "$rpm",
  "rpm_sha256": "$rpm_sha",
  "pool_url": "$pool_url/v$version",
  "signing_key": "$fingerprint",
}, open(sys.argv[1], "w"), indent=2)
print(file=open(sys.argv[1], "a"))
EOF2

echo
echo "linux candidate $version at $commit"
cat "$dest/candidate-linux.json"
