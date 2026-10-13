# RemoteFunnel

A fast, self-hosted remote desktop you use from a web browser. The server captures your screen,
encodes it with the GPU, and streams it with sound over a single WebSocket. Your browser decodes it
with WebCodecs and sends mouse and keyboard input back. You can publish it through a
**Cloudflare Tunnel** or a **Tailscale Funnel**, so you never open ports on your router.

- Hardware H.264 at up to 60 fps, typically 15–20 ms from frame sent to frame shown on a LAN.
  An idle desktop costs about 0.4 Mb/s.
- Low-latency audio passthrough (Opus) with in-page volume control.
- The client is one self-contained HTML page. It works served from the machine itself, opened
  from `file://`, or loaded into a `blob:` tab; the server accepts any Origin.
- Password login. Only a SHA-256 hash is stored, and repeated wrong guesses are throttled.
- The only on-screen UI is a small, faint ••• button you can drag to either edge. It opens a menu
  with fullscreen (captures Alt+Tab and the Windows/Super key), mouse lock for games, cursor
  style, quality settings, sound, special keys and paste-as-typing.

## Platform support

| | Linux | Windows | macOS |
|---|---|---|---|
| Runs as | Docker containers | SYSTEM boot task that runs the agent inside the console session | LaunchAgent |
| Capture | KMS framebuffer (works on Wayland, X11, the lock screen and the login screen) | Desktop Duplication (`ddagrab`) | AVFoundation |
| Encoder | VAAPI (Intel or AMD GPU) | NVENC, AMF or Quick Sync if available, otherwise Media Foundation or x264 | VideoToolbox |
| Input | uinput | SendInput | Quartz CGEvent |
| Audio | PipeWire or PulseAudio | WASAPI loopback | BlackHole loopback device |
| Sign-in / lock screen | Yes | Yes (includes UAC prompts and Ctrl+Alt+Del) | No |
| Status | **Tested** (Artix, KDE Plasma 6 Wayland, AMD) | Win32 parts tested under Wine; not yet on real Windows | Untested, please report issues |

Known limits:
- **Linux:** NVIDIA's proprietary driver isn't supported (KMS + VAAPI capture needs Intel or AMD).
  A monitor or an HDMI dummy plug must be connected.
- **Windows:** the default install needs administrator rights. It works like Parsec or Sunshine:
  a SYSTEM task starts at boot and keeps the agent running inside whichever session owns the
  console. That lets it show and control the sign-in screen, the lock screen, UAC prompts and
  elevated windows, and turn the menu's Ctrl+Alt+Del into a real one. If you install with
  `-UserMode` (no admin needed), none of those are reachable.
- **macOS:** you must grant the agent's Python *Screen Recording* and *Accessibility*
  permissions. Sound needs BlackHole and a Multi-Output Device (see below). The login window
  isn't reachable.

## Install

Clone or download this repository onto the machine you want to control, then run:

**Linux / macOS**
```sh
./setup.sh
```
**Windows** (PowerShell; it asks for administrator rights, or add `-UserMode` to skip them)
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Setup installs what's missing, generates a random 64-character password (shown once; store it in
a password manager), and asks which tunnels you want:

- **Cloudflare Tunnel** needs a domain on Cloudflare. In the Zero Trust dashboard go to
  Networks → Tunnels → Create a tunnel, then paste the install command or token when setup asks.
  Add a Public Hostname to the tunnel with the service set to `http://desktop:8080` on Linux
  (the Docker service name) or `http://localhost:47913` on Windows and macOS (the default port; set `RD_PORT` in the
  config file to change it).
- **Tailscale Funnel** is free and needs no domain. You get `https://<name>.<tailnet>.ts.net`.
  Setup prints a login link, then a second link to allow HTTPS certificates and Funnel for your
  tailnet. Public DNS for a new name can take a few minutes to appear.

At the end, setup writes `RemoteFunnel.html` next to the script. Copy that file anywhere: it
loads the current client from your server and pre-fills the address. To rebuild it later (for
example after adding a tunnel) or on another computer, run `python3 make_launcher.py`. It reads
the addresses setup saved, or asks for them. You can also pass them directly:
`python3 make_launcher.py desk.example.com name.tailnet.ts.net`.

Other commands:

| | Linux / macOS | Windows |
|---|---|---|
| New password | `./setup.sh password` (`-p` to type your own) | `.\setup.ps1 -SetPassword` (`-CustomPassword`) |
| Status | `./setup.sh status` | `.\setup.ps1 -Status` |
| Uninstall | `./setup.sh uninstall` | `.\setup.ps1 -Uninstall` |
| Update | `git pull && ./setup.sh` | `git pull; .\setup.ps1` |

Unattended install (Linux and macOS): set `RF_YES=1`, plus any of `RF_PASSWORD`, `RF_CF_TOKEN`,
`RF_CF_HOSTNAME`, `RF_TAILSCALE=yes` and `RF_TS_HOSTNAME`. On Windows, use `-Yes`,
`-CloudflareToken`, `-CloudflareHostname`, `-Tailscale` and `-TailscaleHostname`.

### Linux notes
- On KDE, setup sets `KWIN_FORCE_SW_CURSOR=1` so the mouse cursor appears in the stream. On
  GNOME it sets `MUTTER_DEBUG_DISABLE_HW_CURSORS=1`. Both take effect at your next login.
- For sound, PipeWire or PulseAudio is told to also listen on a socket in
  `~/.local/state/remotefunnel/`. The container mounts that folder, which exists at boot and
  survives logouts (unlike `/run/user/<uid>`).
- KWin scans out 10-bit framebuffers that stock ffmpeg can't read, so the image builds a small
  ffmpeg with `server/ffmpeg/10bit-rgb.py` applied. The first build takes a few minutes.

### macOS sound
macOS can't capture system audio on its own. Setup offers to install
[BlackHole](https://github.com/ExistentialAudio/BlackHole). Then:
1. Open **Audio MIDI Setup**.
2. Click **+**, then **Create Multi-Output Device**, and tick both your speakers and
   **BlackHole 2ch**.
3. Choose that device as your sound output.

You'll keep hearing sound locally, and the agent streams the BlackHole copy.

## Using it

Open your address (or `RemoteFunnel.html`), enter the password, and connect. The **•••** button
on the screen edge opens the menu; drag it to move it. Esc or a click outside closes the menu
without passing that click to the remote machine. Stats (fps, bitrate, ping, frame round-trip)
are at the top of the menu, or as a small overlay if you turn that on.

Only one viewer at a time: a new login disconnects the previous one with "another client
connected". If you see that and it wasn't you, change the password.

## How it works

```
screen ──ffmpeg (GPU capture + H.264)──FLV──┐
speakers ──ffmpeg (Opus)──Ogg───────────────┤ app.py ══WebSocket══ browser: WebCodecs + WebAudio
                     uinput / SendInput / CGEvent ◀──── mouse & keyboard events ┘
```

- FLV and Ogg are used as framing: each frame or packet is forwarded as soon as it's complete.
- The browser acknowledges every decoded frame. If too many frames are in flight for the current
  round-trip time, the server drops frames and restarts the encoder for a fresh keyframe, so a
  slow link never builds up seconds of delay.
- Audio has its own small queue and is never held back by video.

Files:

| Path | What it is |
|---|---|
| `server/app.py` | Auth, WebSocket, flow control, cloudflared supervision (native installs) |
| `server/media.py` | ffmpeg supervisor plus the FLV and Ogg parsers |
| `server/platform_*.py` | Per-OS capture command, audio source and input injection |
| `server/winsvc.py`, `server/winsys.py` | Windows: SYSTEM watchdog, session launch, secure-desktop following, SendSAS |
| `server/keymap.py` | Browser `KeyboardEvent.code` to evdev, Windows scancode and macOS keycode |
| `server/client.html` | The browser client |
| `launcher/RemoteFunnel.html` | Template for the `file://` launcher |
| `make_launcher.py` | Builds `RemoteFunnel.html` from the template and your addresses |

Tests: `python -m unittest discover -s tests -v` (some tests need ffmpeg and python-evdev), and
`pwsh -File tests/check_ps1.ps1 setup.ps1`, which catches PowerShell variables that differ only in case.

## Security

- Anyone who has the password controls the machine. Use the generated 64-character password.
- The server only listens on `127.0.0.1`; the tunnels are the only way in.
- On Windows the default install runs the agent as SYSTEM, the same as other remote desktops
  that can show the sign-in screen. Its folder (`%ProgramData%\RemoteFunnel`) is readable only
  by SYSTEM and Administrators.
- After 8 wrong passwords, an IP is blocked for 5 minutes.
