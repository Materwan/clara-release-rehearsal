#!/usr/bin/env bash
# First installation of Clara on one Linux machine (run as root). Later updates go through the updater (/update).
#
#   sudo deploy/install.sh <release-tarball.tar.gz>      a release built by the release workflow (run it from the
#                                                        unpacked tarball, or from a checkout of the same tag)
#   sudo deploy/install.sh --from-source <repo>          a source tree (development; web assets must be built)
#
# What it does, each step said as it runs: the `clara` system user and its rootless Podman needs (subuid/subgid,
# linger), /opt/clara/releases and the `current` link, /etc/clara (settings, credentials), the secret key as an
# encrypted systemd credential, the systemd units, `tailscale set --operator=clara`, three first tokens.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
# Paths given are taken from where you are; the script then works from the tree it belongs to (deploy/ is next to it).
args=()
for arg in "$@"; do if [ -e "$arg" ]; then args+=("$(realpath "$arg")"); else args+=("$arg"); fi; done
set -- "${args[@]}"
cd "$(dirname "$(realpath "$0")")/.."
for tool in git podman systemd-creds tailscale; do
    command -v "$tool" >/dev/null || die "$tool is missing (apt install ${tool/systemd-creds/systemd})"
done
command -v uv >/dev/null || die "uv is missing: curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh"

say "The clara user"
install -m 644 deploy/clara.sysusers.conf /etc/sysusers.d/clara.conf
systemd-sysusers
install -d -m 700 -o clara -g clara /var/lib/clara-home /var/lib/clara
if ! grep -q '^clara:' /etc/subuid; then usermod --add-subuids 200000-265535 --add-subgids 200000-265535 clara; fi
loginctl enable-linger clara

say "Releases"
install -d -m 755 -o clara -g clara /opt/clara /opt/clara/releases
if [ "${1:-}" = "--from-source" ]; then
    source_dir=$(realpath "${2:?give the source tree}")
    tag="source"
    release=/opt/clara/releases/$tag
    rm -rf "$release"; install -d -o clara -g clara "$release"
    sudo -u clara env UV_PYTHON_INSTALL_DIR=/var/lib/clara-home/.python uv venv --python 3.12 "$release/.venv"
    sudo -u clara uv pip install --python "$release/.venv/bin/python" "$source_dir[discord]"
else
    tarball=$(realpath "${1:?give the release tarball}")
    tag=$(basename "$tarball" .tar.gz); tag=${tag#clara-}
    release=/opt/clara/releases/$tag
    install -d -o clara -g clara "$release"
    sudo -u clara tar -xzf "$tarball" -C "$release"
    sudo -u clara env UV_PYTHON_INSTALL_DIR=/var/lib/clara-home/.python uv venv --python 3.12 "$release/.venv"
    sudo -u clara uv pip install --python "$release/.venv/bin/python" --require-hashes -r "$release/requirements.lock"
    sudo -u clara uv pip install --python "$release/.venv/bin/python" --no-deps "$release"/clara-*.whl
fi
ln -sfn "$release" /opt/clara/current
chown -h clara:clara /opt/clara/current

say "Settings and the secret key"
install -d -m 750 -o root -g clara /etc/clara
[ -f /etc/clara/clara.env ] || install -m 600 -o clara -g clara deploy/clara.env.example /etc/clara/clara.env
if [ ! -f /etc/clara/secret-key.cred ]; then
    /opt/clara/current/.venv/bin/clara secrets new-key | systemd-creds encrypt --name=clara-secret-key - /etc/clara/secret-key.cred
    echo "The key is encrypted for this machine in /etc/clara/secret-key.cred. Keep a copy of it apart from backups:"
    echo "  sudo systemd-creds decrypt --name=clara-secret-key /etc/clara/secret-key.cred -"
fi

say "Tailscale"
tailscale set --operator=clara || echo "warning: could not make clara the Tailscale operator (tailscale serve needs it)"

say "systemd units"
install -m 644 deploy/clara.service /etc/systemd/system/clara.service
install -m 644 deploy/clara-discord.service /etc/systemd/system/clara-discord.service
systemctl daemon-reload

say "First tokens (shown once: keep them)"
# The key goes in the environment, never on a command line (every user can read /proc/*/cmdline, not /proc/*/environ).
CLARA_SECRET_KEY=$(systemd-creds decrypt --name=clara-secret-key /etc/clara/secret-key.cred -)
export CLARA_SECRET_KEY
for client in console cli web; do
    sudo --preserve-env=CLARA_SECRET_KEY -u clara env CLARA_ENV_FILE=/etc/clara/clara.env CLARA_DATA_DIR=/var/lib/clara \
        /opt/clara/current/.venv/bin/clara token create "$client" "$client" || true
done
unset CLARA_SECRET_KEY

say "Checks"
sudo -u clara env CLARA_ENV_FILE=/etc/clara/clara.env CLARA_DATA_DIR=/var/lib/clara XDG_RUNTIME_DIR=/run/user/$(id -u clara) \
    /opt/clara/current/.venv/bin/clara doctor || echo "Fix what FAILs above (clara doctor), then:"

cat <<EOF

Next:
  1. Edit /etc/clara/clara.env (zone, Ollama hosts, release repository).
  2. Build or pull the sandbox image as clara:  sudo -u clara podman pull <image>   (or podman build sandbox/)
  3. sudo systemctl enable --now clara
  4. Discord (optional, your private server: docs/OPERATIONS.md): either set CLARA_DISCORD_AUTOSTART=true, the ids
     (CLARA_DISCORD_USER_ID, CLARA_DISCORD_CHANNEL_GENERAL/_NOTIFICATION/_APPROVAL/_ALERTS) and the two token files
     (DISCORD_BOT_TOKEN_FILE, CLARA_TOKEN_FILE) in /etc/clara/clara.env, so the server starts the bot; or put the ids
     in /etc/clara/discord.env, encrypt the two credentials (discord-token, clara-token: a token made with
     'clara token create discord discord'), then sudo systemctl enable --now clara-discord
EOF
