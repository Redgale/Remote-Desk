"""ffmpeg supervision and the two stream parsers.

Video: ffmpeg muxes H.264 into FLV on stdout. FLV tags carry explicit sizes, so each frame
can be forwarded the moment its tag is complete (no "wait for the next start code" delay).
Audio: ffmpeg muxes Opus into Ogg with one packet per page, parsed the same way.
"""
import asyncio
import logging
import struct
import time

log = logging.getLogger("rd.media")

VIDEO_CONFIG, VIDEO_FRAME, AUDIO_FRAME, AUDIO_CONFIG = 0, 1, 2, 3


async def read_flv(reader, sink):
    """Parse an FLV stream; calls sink(kind, payload, key). Returns True if any frame came."""
    hdr = await reader.readexactly(9)
    if hdr[:3] != b"FLV":
        raise ConnectionError("not flv")
    await reader.readexactly(struct.unpack(">I", hdr[5:9])[0] - 9 + 4)
    got = False
    while True:
        try:
            th = await reader.readexactly(11)
        except asyncio.IncompleteReadError:
            return got
        size = int.from_bytes(th[1:4], "big")
        data = (await reader.readexactly(size + 4))[:size]
        if th[0] != 9 or len(data) < 5:          # 9 = video tag
            continue
        frame_type, codec, pkt_type = data[0] >> 4, data[0] & 15, data[1]
        if codec != 7:                           # 7 = AVC
            continue
        if pkt_type == 0:
            await sink(VIDEO_CONFIG, data[5:], True)
        elif pkt_type == 1:
            got = True
            await sink(VIDEO_FRAME, data[5:], frame_type == 1)


async def read_ogg(reader, sink):
    """Parse an Ogg/Opus stream; calls sink(kind, payload, False). Returns True if audio came."""
    pending = b""
    index = 0
    got = False
    while True:
        try:
            hdr = await reader.readexactly(27)
        except asyncio.IncompleteReadError:
            return got
        if hdr[:4] != b"OggS":
            raise ConnectionError("lost ogg sync")
        segs = await reader.readexactly(hdr[26])
        body = await reader.readexactly(sum(segs))
        if not hdr[5] & 1:                       # not a continued packet
            pending = b""
        pos = 0
        for s in segs:
            pending += body[pos:pos + s]
            pos += s
            if s < 255:                          # a lacing value < 255 ends the packet
                if index == 0:
                    await sink(AUDIO_CONFIG, pending, True)    # OpusHead
                elif index > 1:                                # index 1 is OpusTags
                    got = True
                    await sink(AUDIO_FRAME, pending, False)
                index += 1
                pending = b""


class FFmpegStream:
    """Keeps one ffmpeg process running, restarting it when it exits.

    make_cmd() builds the command line each start; parse(reader, sink) consumes stdout.
    feed, if given, is an async callable(stdin) that pumps input (e.g. PCM audio) into ffmpeg.
    before_start, if given, runs before every launch (e.g. wake the screen).
    """

    def __init__(self, name, make_cmd, parse, sink, feed=None, before_start=None, env=None, spawn=None):
        self.name, self.make_cmd, self.parse, self.sink = name, make_cmd, parse, sink
        self.feed, self.before_start, self.env = feed, before_start, env
        self.spawn = spawn        # optional async (cmd, stdin) -> process; default: asyncio subprocess
        self.proc = None
        self.task = None
        self.last_restart = 0.0
        self.kf_backoff = 1.0
        self.ok = None            # last run produced output?

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self):
        if not self.running:
            self.task = asyncio.create_task(self._loop())
        else:
            self.restart()

    def restart(self):
        self.last_restart = time.monotonic()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()

    def request_restart(self):
        """Restart soon, but back off if asked repeatedly (e.g. a link that keeps falling behind)."""
        since = time.monotonic() - self.last_restart
        if since > 10:
            self.kf_backoff = 1.0
        if since > self.kf_backoff:
            self.kf_backoff = min(8.0, self.kf_backoff * 2)
            self.restart()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def _loop(self):
        fails = 0
        while True:
            if self.before_start:
                self.before_start()
            cmd = self.make_cmd()
            if not cmd:
                await asyncio.sleep(5)
                continue
            log.info("%s: %s", self.name, " ".join(cmd))
            try:
                if self.spawn:
                    self.proc = await self.spawn(cmd, bool(self.feed))
                else:
                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.PIPE if self.feed else asyncio.subprocess.DEVNULL,
                        env=self.env)
            except OSError as ex:
                log.error("%s: cannot start ffmpeg: %s", self.name, ex)
                await asyncio.sleep(5)
                continue
            helpers = [asyncio.create_task(self._pump_stderr(self.proc.stderr))]
            if self.feed:
                helpers.append(asyncio.create_task(self._run_feed()))
            got = False
            try:
                got = await self.parse(self.proc.stdout, self.sink)
            except (asyncio.IncompleteReadError, ConnectionError) as ex:
                log.debug("%s: stream ended: %s", self.name, ex)
            finally:
                if self.proc.returncode is None:
                    self.proc.kill()
                await self.proc.wait()
                for h in helpers[1:]:
                    h.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)
            self.ok = got
            fails = 0 if got else fails + 1
            log.info("%s exited (%s)", self.name, self.proc.returncode)
            if fails:
                # nothing to capture right now (screen off, logged out, no audio server): back off
                await asyncio.sleep(min(10.0, 0.5 * fails))

    async def _run_feed(self):
        try:
            await self.feed(self.proc.stdin)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("%s: input feed failed", self.name)
            self.restart()

    async def _pump_stderr(self, stream):
        async for line in stream:
            log.warning("%s: %s", self.name, line.decode(errors="replace").rstrip())
