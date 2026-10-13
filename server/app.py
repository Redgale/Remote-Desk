#!/usr/bin/env python3
"""RemoteFunnel remote desktop server.

Video: platform capture -> hardware H.264 (ffmpeg) -> FLV pipe -> WebSocket -> WebCodecs.
Audio: system output -> Opus (ffmpeg) -> Ogg pipe -> WebSocket -> WebCodecs AudioDecoder.
Input: browser events -> WebSocket -> platform injection (uinput / SendInput / CGEvent).

The WebSocket accepts any Origin (file://, blob:, anything); auth is a shared secret whose
SHA-256 is RD_TOKEN_SHA256. Configuration comes from environment variables, optionally loaded
from a KEY=VALUE file given with --config.
"""
import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import logging
import math
import os
import shutil
import struct
import sys
import time

from aiohttp import web, WSMsgType

import media

log = logging.getLogger("rd")
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    """KEY=VALUE lines (# comments). Real environment variables win over the file."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_platform():
    if sys.platform.startswith("linux"):
        import platform_linux as p
    elif sys.platform == "win32":
        import platform_windows as p
    elif sys.platform == "darwin":
        import platform_macos as p
    else:
        raise SystemExit(f"unsupported platform {sys.platform}")
    return p


# ---------------------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------------------

class Session:
    """One connected viewer: frame flow control plus the outgoing queues."""

    def __init__(self, ws, peer, server):
        self.ws, self.peer, self.server = ws, peer, server
        self.seq = 0
        self.acked = 0
        self.sent_at = {}
        self.need_key = True
        self.rtt = 80.0
        self.fps = 60
        self.bytes_sent = 0
        self.dropped = 0
        self.grace_until = 0.0
        self.video_q = collections.deque()
        self.audio_q = collections.deque(maxlen=25)   # ~0.5 s of 20 ms packets, oldest dropped
        self.wakeup = asyncio.Event()
        self.audio_on = False

    def max_inflight(self):
        frame_ms = 1000.0 / self.fps
        return max(6, min(self.fps, math.ceil((self.rtt + 150) / frame_ms)))

    def on_video(self, kind, payload, key):
        if kind == media.VIDEO_CONFIG:
            # new encoder run: the client rebuilds its decoder and never acks older frames
            self.need_key = True
            self.acked = self.seq
            self.sent_at.clear()
            self.grace_until = time.monotonic() + 1.5   # let the browser's decoder warm up
            self.video_q.clear()
            self._push_video(struct.pack(">B", media.VIDEO_CONFIG) + payload)
            return
        if self.need_key and not key:
            return
        inflight = self.seq - self.acked
        late = inflight >= self.max_inflight() and time.monotonic() > self.grace_until
        if not key and (late or len(self.video_q) >= 4):
            self.dropped += 1
            self.need_key = True
            self.server.video.request_restart()      # an IDR comes with the restart
            return
        if key:
            # nothing after a keyframe depends on older frames, so stop waiting on them
            self.acked = self.seq
            self.sent_at.clear()
            self.video_q.clear()
        self.need_key = False
        self.seq += 1
        self.sent_at[self.seq] = time.monotonic()
        self._push_video(struct.pack(">BBI", media.VIDEO_FRAME, 1 if key else 0, self.seq) + payload)

    def _push_video(self, msg):
        self.video_q.append(msg)
        self.wakeup.set()

    def on_audio(self, kind, payload, _key):
        if not self.audio_on:
            return
        self.audio_q.append(struct.pack(">B", kind) + payload)
        self.wakeup.set()

    async def sender(self):
        while True:
            await self.wakeup.wait()
            self.wakeup.clear()
            while self.audio_q or self.video_q:
                q = self.audio_q if self.audio_q else self.video_q   # audio first: it's tiny
                msg = q.popleft()
                self.bytes_sent += len(msg)
                await self.ws.send_bytes(msg)

    def ack(self, s):
        if s > self.acked:
            t = self.sent_at.pop(s, None)
            for k in [k for k in self.sent_at if k < s]:
                del self.sent_at[k]
            self.acked = s
            if t is not None:
                self.rtt = 0.9 * self.rtt + 0.1 * (time.monotonic() - t) * 1000


# ---------------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------------

class Server:
    def __init__(self, plat):
        self.plat = plat
        self.token_sha = os.environ.get("RD_TOKEN_SHA256", "").strip().lower()
        self.inp = plat.Input()
        self.session = None
        self.fails = {}   # ip -> [timestamps]
        self.cfg = {"w": 1920, "h": 1080, "fps": 60, "br": 15.0}
        self.video = media.FFmpegStream("video", lambda: plat.video_cmd(self.cfg), media.read_flv,
                                        self._video_sink, before_start=self.inp.wake,
                                        spawn=getattr(plat, "spawn_video", None))
        self.audio_enabled = os.environ.get("RD_AUDIO", "1") not in ("0", "false", "no")
        self.audio_feed = None
        self.audio_head = None        # last OpusHead, resent to viewers who join a running stream
        self.audio_last = 0.0         # when the last audio packet arrived
        self.audio = media.FFmpegStream("audio", self._audio_cmd, media.read_ogg, self._audio_sink,
                                        feed=self._feed_audio)
        servers = [s.strip() for s in os.environ.get("RD_PUBLIC_URLS", "").split(",") if s.strip()]
        html = open(os.path.join(HERE, "client.html"), encoding="utf-8").read()
        self.client_html = html.replace("/*RF_SERVERS*/", json.dumps(servers)[1:-1]).encode()

    # ----- media plumbing

    def _audio_cmd(self):
        if not self.audio_enabled:
            return None
        src = self.plat.audio_source()
        if not src:
            return None
        args, self.audio_feed = src
        return [self.plat.FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-fflags", "nobuffer", *args,
                "-ac", "2", "-ar", "48000", "-c:a", "libopus", "-b:a", "128k",
                "-application", "lowdelay", "-frame_duration", "20",
                "-f", "ogg", "-page_duration", "20000", "-flush_packets", "1", "pipe:1"]

    async def _feed_audio(self, stdin):
        if self.audio_feed:
            await self.audio_feed(stdin)
        else:
            await asyncio.Event().wait()     # ffmpeg reads its own input; nothing to feed

    async def _video_sink(self, kind, payload, key):
        if self.session:
            self.session.on_video(kind, payload, key)

    async def _audio_sink(self, kind, payload, key):
        if kind == media.AUDIO_CONFIG:
            self.audio_head = payload
        self.audio_last = time.monotonic()
        if self.session:
            self.session.on_audio(kind, payload, key)

    def set_audio(self, sess, on):
        sess.audio_on = on and self.audio_enabled
        if sess.audio_on:
            if not self.audio.running:
                self.audio.start()
            elif self.audio_head:     # stream already running: resend the header
                sess.on_audio(media.AUDIO_CONFIG, self.audio_head, True)
        elif self.audio.running:
            asyncio.create_task(self.audio.stop())

    # ----- HTTP

    def peer_ip(self, req):
        return (req.headers.get("CF-Connecting-IP")
                or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or req.remote or "?")

    async def index(self, req):
        return web.Response(body=self.client_html, content_type="text/html", headers={
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Cache-Control": "no-cache",
        })

    async def health(self, req):
        return web.Response(text="ok", headers={"Access-Control-Allow-Origin": "*"})

    def throttled(self, ip):
        now = time.monotonic()
        recent = [t for t in self.fails.get(ip, []) if now - t < 300]
        self.fails[ip] = recent
        return len(recent) >= 8

    async def ws_handler(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20, compress=False)
        await ws.prepare(req)
        ip = self.peer_ip(req)
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=15)
        except asyncio.TimeoutError:
            await ws.close(code=4000, message=b"auth timeout")
            return ws
        try:
            hello = json.loads(msg.data) if msg.type == WSMsgType.TEXT else {}
        except ValueError:
            hello = {}
        token = str(hello.get("token", ""))
        if self.throttled(ip):
            log.warning("auth throttled for %s", ip)
            await ws.close(code=4029, message=b"too many attempts, wait 5 minutes")
            return ws
        ok = bool(self.token_sha) and hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), self.token_sha)
        if not ok:
            self.fails.setdefault(ip, []).append(time.monotonic())
            log.warning("bad password from %s", ip)
            await asyncio.sleep(1.5)
            await ws.close(code=4001, message=b"wrong password")
            return ws
        self.fails.pop(ip, None)

        if self.session:
            log.info("replacing session from %s", self.session.peer)
            await self.session.ws.close(code=4002, message=b"another client connected")
        sess = Session(ws, ip, self)
        self.session = sess
        c = hello.get("cfg") or {}
        self.cfg.update(self._cfg(c))
        sess.fps = self.cfg["fps"]
        log.info("session start from %s cfg=%s", ip, self.cfg)
        await ws.send_json({"t": "hello", "audio": self.audio_enabled, **self.plat.describe()})
        sender = asyncio.create_task(sess.sender())
        stats = asyncio.create_task(self._stats(sess))
        watcher = asyncio.create_task(self._watch_desktop()) if hasattr(self.plat, "input_desktop") else None
        self.video.start()
        self.set_audio(sess, bool(c.get("audio", True)))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    await self._handle(sess, json.loads(msg.data))
                except (ValueError, KeyError, TypeError) as ex:
                    log.debug("bad msg %r: %s", msg.data[:100], ex)
        finally:
            sender.cancel()
            stats.cancel()
            if watcher:
                watcher.cancel()
            self.inp.release_all()
            if self.session is sess:
                self.session = None
                await self.video.stop()
                await self.audio.stop()
            log.info("session end from %s", ip)
        return ws

    async def _watch_desktop(self):
        """Windows system mode: when the input desktop switches (sign-in screen, UAC prompt and
        back), restart capture on the new desktop instead of streaming a frozen picture."""
        last = self.plat.input_desktop()
        while True:
            await asyncio.sleep(0.3)
            cur = self.plat.input_desktop()
            if cur and cur != last:
                log.info("input desktop: %s -> %s", last, cur)
                self.video.restart()
            last = cur or last

    def _cfg(self, c):
        cur = self.cfg
        return {"w": int(max(320, min(7680, int(c.get("w", cur["w"]))))) & ~1,
                "h": int(max(240, min(4320, int(c.get("h", cur["h"]))))) & ~1,
                "fps": int(max(5, min(120, int(c.get("fps", cur["fps"]))))),
                "br": max(0.5, min(150.0, float(c.get("br", cur["br"]))))}

    async def _stats(self, sess):
        last = 0
        while True:
            await asyncio.sleep(1)
            b = sess.bytes_sent
            kbps, last = (b - last) * 8 // 1000, b
            await sess.ws.send_json({"t": "st", "rtt": round(sess.rtt), "kbps": kbps,
                                     "drop": sess.dropped, "audio": sess.audio_on and time.monotonic() - self.audio_last < 2})

    async def _handle(self, sess, m):
        t = m["t"]
        inp = self.inp
        if t == "m":
            inp.move_abs(m["x"], m["y"])
        elif t == "r":
            inp.move_rel(m["x"], m["y"])
        elif t == "b":
            inp.button(int(m["b"]), bool(m["d"]))
        elif t == "w":
            inp.wheel(m.get("x", 0), m.get("y", 0))
        elif t == "k":
            inp.key(m["c"], bool(m["d"]))
        elif t == "a":
            sess.ack(int(m["s"]))
        elif t == "kf":
            sess.need_key = True
            self.video.request_restart()
        elif t == "type":
            await inp.type_text(str(m["text"]))
        elif t == "rel":
            inp.release_all()
        elif t == "cfg":
            self.cfg.update(self._cfg(m))
            sess.fps = self.cfg["fps"]
            sess.need_key = True
            self.video.start()
        elif t == "audio":
            self.set_audio(sess, bool(m.get("on")))
        elif t == "ping":
            await sess.ws.send_json({"t": "pong", "c": m.get("c")})


# ---------------------------------------------------------------------------------------
# Optional: keep a Cloudflare tunnel running (native installs; the Docker setup uses its own
# cloudflared container instead)
# ---------------------------------------------------------------------------------------

async def run_cloudflared(port):
    token = os.environ.get("CF_TUNNEL_TOKEN", "")
    exe = os.environ.get("RD_CLOUDFLARED") or shutil.which("cloudflared")
    if not token or not exe:
        if token:
            log.error("CF_TUNNEL_TOKEN is set but cloudflared was not found")
        return
    env = dict(os.environ, TUNNEL_TOKEN=token)
    while True:
        log.info("starting cloudflared")
        proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--no-autoupdate", "run", "--url", f"http://127.0.0.1:{port}",
            env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))   # no console
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if " ERR " in text or "Registered tunnel connection" in text:
                log.info("cloudflared: %s", text)
        await proc.wait()
        log.warning("cloudflared exited (%s); restarting in 5 s", proc.returncode)
        await asyncio.sleep(5)


def main():
    ap = argparse.ArgumentParser(description="RemoteFunnel remote desktop server")
    ap.add_argument("--config", help="KEY=VALUE config file")
    args = ap.parse_args()
    if args.config:
        load_config(args.config)
    logfile = os.environ.get("RD_LOG")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=logfile or None)
    token = os.environ.get("RD_TOKEN_SHA256", "").strip()
    if len(token) != 64:
        raise SystemExit("RD_TOKEN_SHA256 must be set to the sha256 hex of your password")
    plat = load_platform()
    port = int(os.environ.get("RD_PORT", "47913"))
    bind = os.environ.get("RD_BIND", "127.0.0.1")
    srv = Server(plat)
    app = web.Application()
    app.router.add_get("/", srv.index)
    app.router.add_get("/client.html", srv.index)
    app.router.add_get("/healthz", srv.health)
    app.router.add_get("/favicon.ico", lambda req: web.Response(status=204))
    app.router.add_get("/ws", srv.ws_handler)

    async def start_tunnel(_app):
        _app["cloudflared"] = asyncio.create_task(run_cloudflared(port))

    if os.environ.get("RD_RUN_CLOUDFLARED", "0") == "1":
        app.on_startup.append(start_tunnel)
    log.info("RemoteFunnel on %s:%d (%s)", bind, port, plat.NAME)
    web.run_app(app, host=bind, port=port, access_log=None, print=None)


if __name__ == "__main__":
    main()
