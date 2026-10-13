#!/usr/bin/env bash
# RemoteFunnel setup for Linux and macOS.
#
#   ./setup.sh             install, or update an existing install (keeps password and tokens)
#   ./setup.sh password    set a new password (random, or -p to type your own)
#   ./setup.sh status      show what's running and the addresses
#   ./setup.sh uninstall   stop and remove the service (keeps this folder)
#
# Unattended use: set RF_YES=1 and any of
#   RF_PASSWORD=...  RF_CF_TOKEN=...  RF_CF_HOSTNAME=...  RF_TAILSCALE=yes|no  RF_TS_HOSTNAME=...
#
# Linux runs the server in Docker (KMS capture + VAAPI need a kernel-level view of the display).
# macOS runs it natively as a LaunchAgent.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"
SUDO="${SUDO:-sudo}"
YES="${RF_YES:-0}"
OS="$(uname -s)"

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
c_bold=$'\e[1m'; c_dim=$'\e[2m'; c_red=$'\e[31m'; c_grn=$'\e[32m'; c_ylw=$'\e[33m'; c_off=$'\e[0m'
[ -t 1 ] || { c_bold=; c_dim=; c_red=; c_grn=; c_ylw=; c_off=; }
say()  { printf '%s==>%s %s\n' "$c_bold" "$c_off" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ask() {   # ask "question" default -> answer on stdout
  local q="$1" def="${2:-}" a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$def"; return; fi
  read -r -p "$q${def:+ [$def]} " a </dev/tty || true
  printf '%s' "${a:-$def}"
}
yesno() { # yesno "question" y|n
  local a; a="$(ask "$1 (y/n)" "$2")"
  case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

sha256() { if have sha256sum; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi; }
random_password() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# KEY=VALUE config file helpers (the Linux .env doubles as docker compose's env file)
conf_get() { [ -f "$CONF" ] && grep -E "^$1=" "$CONF" | tail -1 | cut -d= -f2- || true; }
conf_set() {
  local k="$1" v="$2" tmp
  touch "$CONF"; chmod 600 "$CONF"
  tmp="$(mktemp)"; grep -vE "^$k=" "$CONF" > "$tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$tmp"; cat "$tmp" > "$CONF"; rm -f "$tmp"
}

extract_cf_token() {  # accepts a bare token or the whole "cloudflared ... --token X" command
  grep -oE 'eyJ[A-Za-z0-9_=.-]+' <<<"$1" | head -1 || true
}

set_password() {   # set_password [-p]
  local pw
  if [ -n "${RF_PASSWORD:-}" ]; then pw="$RF_PASSWORD"
  elif [ "${1:-}" = "-p" ]; then
    read -r -s -p "New password: " pw </dev/tty; echo
    [ ${#pw} -ge 12 ] || die "use at least 12 characters"
  else
    pw="$(random_password)"
    say "Your password (save it in a password manager; only its hash is stored):"
    printf '\n    %s%s%s\n\n' "$c_bold" "$pw" "$c_off"
  fi
  conf_set RD_TOKEN_SHA256 "$(printf %s "$pw" | sha256)"
}

write_launcher() {   # write_launcher host1,host2 -> RemoteFunnel.html next to this script
  local hosts="$1" list="" h
  IFS=, read -r -a arr <<<"$hosts"
  for h in ${arr[@]+"${arr[@]}"}; do [ -n "$h" ] && list+="\"$h\", "; done
  list="${list%, }"
  sed "s|/\*RF_HOSTS\*/|$list|" "$REPO/launcher/RemoteFunnel.html" > "$REPO/RemoteFunnel.html"
  ok "launcher written: $REPO/RemoteFunnel.html (open it from anywhere, even file://)"
}

ask_tunnels() {   # sets CF_TOKEN CF_HOST USE_TS TS_HOST (keeping existing values on update)
  CF_TOKEN="$(conf_get CF_TUNNEL_TOKEN)"; CF_HOST="$(conf_get CF_HOSTNAME)"
  TS_HOST="$(conf_get TS_HOSTNAME)"; USE_TS=0
  [ -n "$TS_HOST" ] && USE_TS=1
  if [ -n "${RF_CF_TOKEN:-}" ]; then CF_TOKEN="$(extract_cf_token "$RF_CF_TOKEN")"; CF_HOST="${RF_CF_HOSTNAME:-$CF_HOST}"
  elif [ -z "$CF_TOKEN" ] && [ "$YES" != 1 ]; then
    echo
    say "Cloudflare Tunnel (optional): publishes the desktop at a hostname on your own domain."
    printf '  %sZero Trust dashboard -> Networks -> Tunnels -> Create -> copy the install command.%s\n' "$c_dim" "$c_off"
    local t; t="$(ask "  Paste the tunnel token or install command (Enter to skip):" "")"
    CF_TOKEN="$(extract_cf_token "$t")"
    [ -n "$t" ] && [ -z "$CF_TOKEN" ] && warn "that doesn't contain a tunnel token; skipping Cloudflare"
    if [ -n "$CF_TOKEN" ]; then
      CF_HOST="$(ask "  Public hostname routed to this tunnel (e.g. desktop.example.com):" "$CF_HOST")"
    fi
  fi
  # an existing tunnel whose hostname was never recorded (older installs)
  if [ -n "$CF_TOKEN" ] && [ -z "$CF_HOST" ]; then
    CF_HOST="${RF_CF_HOSTNAME:-}"
    [ -z "$CF_HOST" ] && CF_HOST="$(ask "  Public hostname of your Cloudflare tunnel (for the login page; Enter to skip):" "")"
  fi
  if [ -n "${RF_TAILSCALE:-}" ]; then
    case "$RF_TAILSCALE" in y*|Y*|1) USE_TS=1 ;; *) USE_TS=0 ;; esac
  elif [ "$USE_TS" = 0 ] && [ "$YES" != 1 ]; then
    echo
    say "Tailscale Funnel (optional): a free https://<name>.<tailnet>.ts.net address, no domain needed."
    yesno "  Set up Tailscale Funnel?" n && USE_TS=1
  fi
  [ "$USE_TS" = 1 ] && TS_HOST="${RF_TS_HOSTNAME:-${TS_HOST:-remotefunnel}}"
  [ -n "$CF_TOKEN" ] || [ "$USE_TS" = 1 ] || \
    warn "no tunnel chosen: the server will only listen on 127.0.0.1 (use your own reverse proxy)"
}

# ---------------------------------------------------------------------------------------------
# Linux (Docker)
# ---------------------------------------------------------------------------------------------
linux_pkg_install() {   # linux_pkg_install pkg...  (best effort across distros)
  if have pacman; then $SUDO pacman -S --needed --noconfirm "$@"
  elif have apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y "$@"
  elif have dnf; then $SUDO dnf install -y "$@"
  elif have zypper; then $SUDO zypper --non-interactive install "$@"
  elif have xbps-install; then $SUDO xbps-install -Sy "$@"
  elif have apk; then $SUDO apk add "$@"
  else die "unknown package manager; install: $*"; fi
}

linux_enable_service() {   # enable + start a system service under systemd, runit or OpenRC
  local s="$1"
  if have systemctl && [ -d /run/systemd/system ]; then $SUDO systemctl enable --now "$s"
  elif [ -d /etc/runit/sv/"$s" ] && [ -d /run/runit/service ]; then          # Artix
    $SUDO ln -sf /etc/runit/sv/"$s" /run/runit/service/
    [ -d /etc/runit/runsvdir/default ] && $SUDO ln -sf /etc/runit/sv/"$s" /etc/runit/runsvdir/default/
  elif [ -d /etc/sv/"$s" ] && [ -d /var/service ]; then                     # Void
    $SUDO ln -sf /etc/sv/"$s" /var/service/
  elif have rc-update; then $SUDO rc-update add "$s" default && $SUDO rc-service "$s" start
  else warn "couldn't enable the $s service automatically; start it yourself"; fi
}

linux_docker() {
  if ! have docker; then
    say "Installing Docker"
    if have pacman; then
      linux_pkg_install docker docker-compose docker-buildx
      pacman -Si docker-runit >/dev/null 2>&1 && linux_pkg_install docker-runit
    elif have apt-get; then linux_pkg_install docker.io docker-compose-v2 || linux_pkg_install docker.io docker-compose-plugin
    elif have dnf; then linux_pkg_install moby-engine docker-compose || linux_pkg_install docker docker-compose
    elif have xbps-install; then linux_pkg_install docker docker-compose
    else linux_pkg_install docker docker-compose; fi
    linux_enable_service docker
    for _ in $(seq 1 20); do $SUDO docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  $SUDO docker info >/dev/null 2>&1 || { linux_enable_service docker; sleep 3; }
  $SUDO docker info >/dev/null 2>&1 || die "Docker isn't running"
  $SUDO docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is missing (docker compose)"
  DOCKER="$SUDO docker"
  ok "Docker $($DOCKER version --format '{{.Server.Version}}' 2>/dev/null)"
}

linux_checks() {
  ls /dev/dri/renderD* >/dev/null 2>&1 || die "no GPU render node in /dev/dri (Intel or AMD with VAAPI is required)"
  local connected=0 s
  for s in /sys/class/drm/card*-*/status; do [ "$(cat "$s" 2>/dev/null)" = connected ] && connected=1; done
  [ "$connected" = 1 ] || warn "no connected monitor found; KMS capture needs an active display (an HDMI dummy plug works)"
  if [ -d /sys/module/nvidia ]; then
    warn "NVIDIA's driver is loaded: KMS+VAAPI capture only works on Intel/AMD GPUs"
  fi
  if [ ! -e /dev/uinput ]; then $SUDO modprobe uinput || true; fi
  [ -e /dev/uinput ] || die "/dev/uinput is missing (kernel module uinput)"
  if [ -d /etc/modules-load.d ] && [ ! -f /etc/modules-load.d/uinput.conf ]; then
    echo uinput | $SUDO tee /etc/modules-load.d/uinput.conf >/dev/null
  fi
  ok "GPU, display and uinput look usable"
}

linux_audio() {   # give the container a PulseAudio socket at a fixed path in $HOME
  local dir="$HOME/.local/state/remotefunnel"
  mkdir -p "$dir"
  conf_set RD_PULSE_DIR "$dir"
  if pgrep -u "$(id -u)" -x pipewire-pulse >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/pipewire/pipewire-pulse.conf.d"
    cat > "$HOME/.config/pipewire/pipewire-pulse.conf.d/90-remotefunnel.conf" <<EOF
# RemoteFunnel: also listen on a socket at a fixed path the Docker container can mount
pulse.properties = {
    server.address = [
        "unix:native"
        "unix:$dir/pulse.sock"
    ]
}
EOF
    if [ ! -S "$dir/pulse.sock" ]; then
      if have systemctl && systemctl --user is-active pipewire-pulse >/dev/null 2>&1; then
        systemctl --user restart pipewire-pulse
      else
        pkill -x pipewire-pulse || true; sleep 1; (setsid pipewire-pulse >/dev/null 2>&1 &)
      fi
      sleep 2
    fi
    conf_set RD_AUDIO 1
    [ -S "$dir/pulse.sock" ] && ok "audio: PipeWire socket at $dir/pulse.sock" || \
      warn "audio: PipeWire config written, the socket appears after your next login"
  elif pgrep -u "$(id -u)" -x pulseaudio >/dev/null 2>&1; then
    local pa="$HOME/.config/pulse/default.pa"
    mkdir -p "$(dirname "$pa")"
    [ -f "$pa" ] || echo ".include /etc/pulse/default.pa" > "$pa"
    grep -q "$dir/pulse.sock" "$pa" || \
      echo "load-module module-native-protocol-unix socket=$dir/pulse.sock auth-anonymous=1" >> "$pa"
    pulseaudio -k 2>/dev/null || true; sleep 2
    conf_set RD_AUDIO 1
    ok "audio: PulseAudio socket at $dir/pulse.sock"
  else
    conf_set RD_AUDIO 0
    warn "audio: no PipeWire or PulseAudio running for $(id -un); audio passthrough disabled"
  fi
}

linux_desktop_tweaks() {   # make the mouse cursor part of the captured framebuffer
  local de="${XDG_CURRENT_DESKTOP:-}"
  if [ -d "$HOME/.config/plasma-workspace" ] || [[ "$de" == *KDE* ]]; then
    mkdir -p "$HOME/.config/plasma-workspace/env"
    cat > "$HOME/.config/plasma-workspace/env/remotefunnel.sh" <<'EOF'
# RemoteFunnel: keep the screen capturable by KMS grab
export KWIN_FORCE_SW_CURSOR=1        # draw the cursor into the frame so it shows in the stream
export KWIN_DRM_NO_DIRECT_SCANOUT=1  # always composite, so fullscreen apps stay capturable
EOF
    ok "KDE: cursor/scanout tweaks installed (take effect at next login)"
  fi
  if [[ "$de" == *GNOME* ]] && [ -d /run/systemd/system ]; then
    mkdir -p "$HOME/.config/environment.d"
    echo "MUTTER_DEBUG_DISABLE_HW_CURSORS=1" > "$HOME/.config/environment.d/remotefunnel.conf"
    ok "GNOME: software cursor enabled (takes effect at next login)"
  fi
}

linux_tailscale_login() {
  local state="$REPO/tailscale/state"
  mkdir -p "$state"
  # already running and logged in? (never start a second tailscaled on the same state)
  if $DOCKER compose exec -T tailscale tailscale status >/dev/null 2>&1; then
    ok "Tailscale: already logged in"; return
  fi
  $DOCKER compose stop tailscale >/dev/null 2>&1 || true
  say "Tailscale login"
  # the stock container gives `tailscale up` only 60 s, so log in with a one-off container that
  # waits as long as needed; it finishes at once if the saved state is already logged in
  $DOCKER rm -f rf-ts-login >/dev/null 2>&1 || true
  $DOCKER run -d --name rf-ts-login -v "$state:/var/lib/tailscale" --entrypoint sh tailscale/tailscale -c \
    "tailscaled --tun=userspace-networking --statedir=/var/lib/tailscale & sleep 3; tailscale up --reset --accept-dns=false --hostname=$TS_HOST --timeout=0 && echo RF-LOGIN-DONE; sleep infinity" >/dev/null
  local url="" logs
  for _ in $(seq 1 30); do
    logs="$($DOCKER logs rf-ts-login 2>&1)"
    grep -q RF-LOGIN-DONE <<<"$logs" && break
    url="$(grep -oE 'https://login.tailscale.com/[^ ]+' <<<"$logs" | tail -1 || true)"
    [ -n "$url" ] && break; sleep 1
  done
  if [ -n "$url" ]; then
    printf '\n  Open this link and approve the machine "%s":\n\n    %s\n\n' "$TS_HOST" "$url"
    printf '  Waiting for approval...'
    until $DOCKER logs rf-ts-login 2>&1 | grep -q RF-LOGIN-DONE; do sleep 3; printf .; done
    echo
  elif ! grep -q RF-LOGIN-DONE <<<"$logs"; then
    $DOCKER logs rf-ts-login 2>&1 | tail -5; $DOCKER rm -f rf-ts-login >/dev/null; die "Tailscale login didn't start"
  fi
  $DOCKER rm -f rf-ts-login >/dev/null
  ok "Tailscale: logged in"
}

linux_tailscale_funnel() {   # make sure HTTPS certs + Funnel are allowed, then report the URL
  local name status link
  for _ in $(seq 1 30); do
    status="$($DOCKER compose exec -T tailscale tailscale status --json 2>/dev/null || true)"
    grep -q '"BackendState": "Running"' <<<"$status" && break; sleep 2
  done
  name="$(grep -oE '"DNSName": "[^"]+' <<<"$status" | head -1 | cut -d'"' -f4 | sed 's/\.$//')"
  if ! grep -q 'cap/funnel' <<<"$status" || ! grep -q '"CertDomains": \[' <<<"$status"; then
    link="$($DOCKER compose exec -T tailscale sh -c 'timeout 8 tailscale funnel --bg http://desktop:8080 2>&1' | grep -oE 'https://login.tailscale.com/[^ ]+' | head -1 || true)"
    printf '\n  Funnel/HTTPS is not enabled for your tailnet yet. Open this and approve it:\n\n    %s\n\n' "${link:-https://login.tailscale.com/admin/dns}"
    printf '  Waiting...'
    until $DOCKER compose exec -T tailscale tailscale status --json 2>/dev/null | grep -q 'cap/funnel'; do sleep 3; printf .; done
    echo; $DOCKER compose restart tailscale >/dev/null
  fi
  TS_URL="$name"
  ok "Tailscale Funnel: https://$name (public DNS for a new name can take a few minutes)"
}

linux_install() {
  CONF="$REPO/.env"
  say "RemoteFunnel for Linux (Docker)"
  linux_checks
  linux_docker
  [ -n "$(conf_get RD_TOKEN_SHA256)" ] || set_password
  ask_tunnels
  linux_audio
  linux_desktop_tweaks
  local profiles=() urls=()
  if [ -n "$CF_TOKEN" ]; then
    conf_set CF_TUNNEL_TOKEN "$CF_TOKEN"; conf_set CF_HOSTNAME "$CF_HOST"
    profiles+=(cloudflare); [ -n "$CF_HOST" ] && urls+=("$CF_HOST")
  fi
  if [ "$USE_TS" = 1 ]; then conf_set TS_HOSTNAME "$TS_HOST"; profiles+=(tailscale); fi
  conf_set COMPOSE_PROFILES "$(IFS=,; echo "${profiles[*]:-}")"
  conf_set RD_PORT "$(conf_get RD_PORT | grep . || echo 47913)"
  [ "$USE_TS" = 1 ] && linux_tailscale_login
  say "Building and starting (the first build compiles ffmpeg: a few minutes)"
  $DOCKER compose up -d --build 2>&1 | grep -vE '^#|^ *$' | tail -5
  for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$(conf_get RD_PORT)/healthz" >/dev/null 2>&1 && break; sleep 1; done
  curl -fs "http://127.0.0.1:$(conf_get RD_PORT)/healthz" >/dev/null || die "the server didn't come up: $DOCKER compose logs desktop"
  ok "server running on 127.0.0.1:$(conf_get RD_PORT)"
  TS_URL=""
  if [ "$USE_TS" = 1 ]; then linux_tailscale_funnel; [ -n "$TS_URL" ] && urls+=("$TS_URL"); fi
  conf_set RD_PUBLIC_URLS "$(IFS=,; echo "${urls[*]:-}")"
  $DOCKER compose up -d desktop >/dev/null 2>&1    # pick up the address list
  write_launcher "$(conf_get RD_PUBLIC_URLS)"
  finish
}

# ---------------------------------------------------------------------------------------------
# macOS (native LaunchAgent)
# ---------------------------------------------------------------------------------------------
MAC_DIR="$HOME/Library/Application Support/RemoteFunnel"
MAC_PLIST="$HOME/Library/LaunchAgents/com.remotefunnel.agent.plist"

mac_tailscale_cli() {
  if have tailscale; then echo tailscale
  elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then echo /Applications/Tailscale.app/Contents/MacOS/Tailscale
  fi
}

mac_install() {
  CONF="$MAC_DIR/config.env"
  say "RemoteFunnel for macOS"
  have brew || die "Homebrew is required: https://brew.sh"
  mkdir -p "$MAC_DIR"
  local pkgs=(ffmpeg python@3.12)
  ask_tunnels
  [ -n "$CF_TOKEN" ] && pkgs+=(cloudflared)
  say "Installing ${pkgs[*]} with Homebrew"
  brew install "${pkgs[@]}" >/dev/null
  local py; py="$(brew --prefix python@3.12)/bin/python3.12"
  [ -x "$py" ] || py="$(command -v python3)"

  say "Installing the agent into $MAC_DIR"
  cp "$REPO"/server/{app.py,media.py,keymap.py,platform_macos.py,client.html} "$MAC_DIR/"
  [ -d "$MAC_DIR/venv" ] || "$py" -m venv "$MAC_DIR/venv"
  "$MAC_DIR/venv/bin/pip" install -q --upgrade pip
  "$MAC_DIR/venv/bin/pip" install -q -r "$REPO/server/requirements-macos.txt"
  [ -n "$(conf_get RD_TOKEN_SHA256)" ] || set_password
  conf_set RD_PORT "$(conf_get RD_PORT | grep . || echo 47913)"
  conf_set RD_BIND 127.0.0.1
  conf_set RD_FFMPEG "$(command -v ffmpeg)"
  conf_set RD_LOG "$MAC_DIR/remotefunnel.log"
  local urls=()
  if [ -n "$CF_TOKEN" ]; then
    conf_set CF_TUNNEL_TOKEN "$CF_TOKEN"; conf_set CF_HOSTNAME "$CF_HOST"
    conf_set RD_RUN_CLOUDFLARED 1; conf_set RD_CLOUDFLARED "$(command -v cloudflared)"
    [ -n "$CF_HOST" ] && urls+=("$CF_HOST")
  else conf_set RD_RUN_CLOUDFLARED 0; fi

  # audio needs a loopback device
  if ! system_profiler SPAudioDataType 2>/dev/null | grep -qi blackhole; then
    if yesno "Install BlackHole (needed for sound passthrough)?" y; then
      brew install blackhole-2ch >/dev/null || warn "BlackHole install failed"
      warn "audio: open Audio MIDI Setup, create a Multi-Output Device with your speakers + BlackHole 2ch,"
      warn "       and choose it as the sound output (see README)."
    fi
  fi

  say "Registering the LaunchAgent"
  mkdir -p "$(dirname "$MAC_PLIST")"
  cat > "$MAC_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.remotefunnel.agent</string>
  <key>ProgramArguments</key><array>
    <string>$MAC_DIR/venv/bin/python</string><string>$MAC_DIR/app.py</string>
    <string>--config</string><string>$CONF</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$MAC_DIR/stderr.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)" "$MAC_PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$MAC_PLIST"
  sleep 3
  curl -fs "http://127.0.0.1:$(conf_get RD_PORT)/healthz" >/dev/null && ok "agent running" || \
    warn "agent not answering yet; see $MAC_DIR/remotefunnel.log"

  local real; real="$(cd "$MAC_DIR/venv/bin" && python -c 'import sys,os;print(os.path.realpath(sys.executable))' 2>/dev/null || true)"
  echo
  say "Grant two permissions to this Python, then the agent restarts by itself:"
  printf '    %s\n' "${real:-$MAC_DIR/venv/bin/python}"
  printf '  - System Settings -> Privacy & Security -> %sScreen Recording%s (+ add it)\n' "$c_bold" "$c_off"
  printf '  - System Settings -> Privacy & Security -> %sAccessibility%s (+ add it)\n' "$c_bold" "$c_off"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true

  if [ "$USE_TS" = 1 ]; then
    local ts; ts="$(mac_tailscale_cli)"
    if [ -z "$ts" ]; then brew install --cask tailscale >/dev/null; ts="$(mac_tailscale_cli)"; fi
    if [ -n "$ts" ]; then
      say "Tailscale: log in from the menu-bar app if you haven't, then enabling Funnel"
      open -a Tailscale 2>/dev/null || true
      until "$ts" status >/dev/null 2>&1; do sleep 3; done
      "$ts" funnel --bg "$(conf_get RD_PORT)" || warn "approve Funnel at the link above, then run: $ts funnel --bg $(conf_get RD_PORT)"
      local name; name="$("$ts" status --json | grep -oE '"DNSName": "[^"]+' | head -1 | cut -d'"' -f4 | sed 's/\.$//')"
      [ -n "$name" ] && urls+=("$name") && ok "Tailscale Funnel: https://$name"
    else warn "Tailscale CLI not found; install the Tailscale app and run: tailscale funnel --bg $(conf_get RD_PORT)"; fi
  fi
  conf_set RD_PUBLIC_URLS "$(IFS=,; echo "${urls[*]:-}")"
  launchctl kickstart -k "gui/$(id -u)/com.remotefunnel.agent" 2>/dev/null || true
  write_launcher "$(conf_get RD_PUBLIC_URLS)"
  finish
}

# ---------------------------------------------------------------------------------------------
finish() {
  echo
  say "Done."
  local u
  IFS=, read -r -a arr <<<"$(conf_get RD_PUBLIC_URLS)"
  for u in ${arr[@]+"${arr[@]}"}; do [ -n "$u" ] && printf '    https://%s\n' "$u"; done
  printf '    or open %s from any device\n' "$REPO/RemoteFunnel.html"
  echo "  New password any time:  ./setup.sh password"
}

status() {
  if [ "$OS" = Linux ]; then
    CONF="$REPO/.env"; DOCKER="$SUDO docker"
    $DOCKER compose ps
  else
    CONF="$MAC_DIR/config.env"
    launchctl print "gui/$(id -u)/com.remotefunnel.agent" 2>/dev/null | grep -E "state|pid" || echo "agent not loaded"
  fi
  curl -fs "http://127.0.0.1:$(conf_get RD_PORT | grep . || echo 47913)/healthz" >/dev/null && ok "server answering" || warn "server not answering"
  echo "addresses: $(conf_get RD_PUBLIC_URLS)"
}

uninstall() {
  if [ "$OS" = Linux ]; then
    $SUDO docker compose down
    ok "stopped and removed the containers (config kept in .env)"
  else
    launchctl bootout "gui/$(id -u)" "$MAC_PLIST" 2>/dev/null || true
    rm -f "$MAC_PLIST"
    ok "LaunchAgent removed (files kept in $MAC_DIR)"
  fi
}

case "${1:-install}" in
  install|update)
    case "$OS" in Linux) linux_install ;; Darwin) mac_install ;; *) die "use setup.ps1 on Windows" ;; esac ;;
  password)
    if [ "$OS" = Linux ]; then CONF="$REPO/.env"; set_password "${2:-}"; $SUDO docker compose up -d desktop
    else CONF="$MAC_DIR/config.env"; set_password "${2:-}"; launchctl kickstart -k "gui/$(id -u)/com.remotefunnel.agent"; fi
    ok "password changed" ;;
  status) status ;;
  uninstall) uninstall ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
