#!/usr/bin/env bash
# Build a release into dist/release/<tag>/, in the exact shape the updater and deploy/install.sh expect:
#
#   clara-<tag>.tar.gz   the wheel (with the web site built in), requirements.lock (hash-pinned, Discord included),
#                        deploy/, sandbox/, the documents
#   manifest.json        tag, version, Python, the sandbox image pinned by digest, the commit
#   SHA256SUMS           sums of the two files above
#   SHA256SUMS.sig       Ed25519 signature, when CLARA_RELEASE_SIGNING_KEY is set (scripts/release_signing.py)
#
#   scripts/build-release.sh v1.2.0
#   SANDBOX_IMAGE=ghcr.io/me/clara-sandbox@sha256:… scripts/build-release.sh v1.2.0
#   SKIP_WEB=1 …   reuse the site already in src/clara/web/dist
set -euo pipefail

tag="${1:?give the tag, for example v1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)
[[ "$tag" == "v$version" ]] || { echo "the tag $tag does not match the version in pyproject.toml ($version)" >&2; exit 1; }
[[ -z "${SANDBOX_IMAGE:-}" || "$SANDBOX_IMAGE" == *@sha256:* ]] || { echo "SANDBOX_IMAGE must be pinned by digest (…@sha256:…)" >&2; exit 1; }

if [[ "${SKIP_WEB:-}" != 1 ]]; then
  scripts/build-web.sh
fi
[[ -f src/clara/web/dist/index.html ]] || { echo "the web site is not built (scripts/build-web.sh)" >&2; exit 1; }

out="$ROOT/dist/release/$tag"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
rm -rf "$out"; mkdir -p "$out"

"$UV" build --wheel --out-dir "$stage" --quiet
rm -f "$stage/.gitignore"  # uv marks its output folder; it does not belong in the release
"$UV" export --frozen --no-dev --extra discord --no-emit-project --no-header --format requirements-txt \
  --output-file "$stage/requirements.lock" --quiet
grep -q -- "--hash=sha256:" "$stage/requirements.lock" || { echo "requirements.lock has no hashes" >&2; exit 1; }
cp -r deploy sandbox "$stage/"
cp README.md FEATURES.md CHANGELOG.md SECURITY.md LICENSE "$stage/"
cp -r docs "$stage/"
find "$stage" -name '__pycache__' -prune -exec rm -rf {} +

# Reproducible-ish: fixed order, owner and times; only files and folders (the updater refuses anything else).
stamp="$(git log -1 --format=%ct 2>/dev/null || date +%s)"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$stamp" -C "$stage" -czf "$out/clara-$tag.tar.gz" \
  $(cd "$stage" && ls -A)

commit="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
cat > "$out/manifest.json" <<EOF
{
  "tag": "$tag",
  "version": "$version",
  "python": "3.12",
  "sandbox_image": "${SANDBOX_IMAGE:-}",
  "commit": "$commit",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

(cd "$out" && sha256sum "clara-$tag.tar.gz" manifest.json > SHA256SUMS)
if [[ -n "${CLARA_RELEASE_SIGNING_KEY:-}" ]]; then
  "$UV" run --frozen --quiet python scripts/release_signing.py sign "$out/SHA256SUMS"
  if [[ -n "${CLARA_RELEASE_PUBKEY:-}" ]]; then
    "$UV" run --frozen --quiet python scripts/release_signing.py verify "$out/SHA256SUMS" "$CLARA_RELEASE_PUBKEY"
  fi
else
  echo "note: CLARA_RELEASE_SIGNING_KEY is not set: the release is unsigned (machines with CLARA_RELEASE_PUBKEY refuse it)"
fi

# The release must pass the updater's own checks before it leaves this machine.
"$UV" run --frozen --quiet python - "$out" "$tag" <<'PY'
import sys
import tempfile
from pathlib import Path

from clara.ops.updater import check_sums, safe_extract

out, tag = Path(sys.argv[1]), sys.argv[2]
check_sums((out / "SHA256SUMS").read_text(), out, [f"clara-{tag}.tar.gz", "manifest.json"])
with tempfile.TemporaryDirectory() as folder:
    safe_extract(out / f"clara-{tag}.tar.gz", Path(folder))
    root = Path(folder)
    assert list(root.glob("clara-*.whl")), "no wheel"
    assert (root / "requirements.lock").is_file() and (root / "deploy" / "install.sh").is_file()
print("release checked: sums, safe archive, wheel, lock, installer")
PY

ls -l "$out"
