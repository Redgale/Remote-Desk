"""Windows backend: Desktop Duplication capture, best available H.264 encoder, SendInput, WASAPI
loopback audio.

Two ways to run:
  - "system" (default install): winsvc.py, a SYSTEM watchdog, starts this agent inside the console
    session with a SYSTEM token (RD_SYSTEM_AGENT=1). The agent follows the input desktop, so the
    sign-in screen, lock screen and UAC prompts are visible and controllable, input reaches
    elevated windows, and Ctrl+Alt+Del is a real Secure Attention Sequence.
  - "user" (setup.ps1 -UserMode, no admin needed): a logon task running as you. The secure desktop
    then can't be captured or controlled, and elevated windows ignore input.
"""
import asyncio
import ctypes
import logging
import os
import queue
import subprocess
import threading
import time
from ctypes import wintypes

import keymap
import winsys

NAME = "windows"
FFMPEG = os.environ.get("RD_FFMPEG", "ffmpeg")
SYSTEM_MODE = os.environ.get("RD_SYSTEM_AGENT") == "1"
log = logging.getLogger("rd.windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:   # pixel-exact coordinates on scaled (HiDPI) displays
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
except (AttributeError, OSError):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


# --------------------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------------------

def _enc_args(name, br, gop):
    rate = ["-b:v", f"{br * 0.75:.2f}M", "-maxrate", f"{br}M", "-bufsize", f"{br / 4:.3f}M"]
    common = ["-bf", "0", "-g", str(gop)]
    return {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1",
                       "-rc", "vbr", "-delay", "0", "-profile:v", "high"] + rate + common,
        "h264_amf": ["-c:v", "h264_amf", "-usage", "ultralowlatency", "-quality", "speed",
                     "-rc", "vbr_peak"] + rate + common,
        "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0",
                     "-async_depth", "1"] + rate + common,
        "h264_mf": ["-c:v", "h264_mf", "-hw_encoding", "1", "-scenario", "display_remoting"]
                   + rate + ["-g", str(gop)],
        "libx264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]
                   + rate + common,
    }[name]


_encoder = None


def pick_encoder():
    """First encoder that actually works on this machine (hardware first, x264 last)."""
    global _encoder
    if _encoder:
        return _encoder
    order = ["h264_nvenc", "h264_amf", "h264_qsv", "h264_mf", "libx264"]
    forced = os.environ.get("RD_ENCODER")
    if forced:
        order = [forced]
    for enc in order:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=640x360:r=30", "-frames:v", "5", "-vf", "format=nv12",
               *_enc_args(enc, 4, 60), "-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            log.info("using encoder %s", enc)
            _encoder = enc
            return enc
        log.info("encoder %s unavailable: %s", enc, r.stderr.decode(errors="replace").strip()[-200:])
    _encoder = "libx264"
    return _encoder


def describe():
    return {"platform": NAME, "encoder": pick_encoder(), "mode": "system" if SYSTEM_MODE else "user"}


def input_desktop():
    """Current input desktop name; the server restarts capture when it changes."""
    return winsys.input_desktop_name() if SYSTEM_MODE else None


# in system mode ffmpeg is started on the input desktop, so ddagrab sees the sign-in screen/UAC
spawn_video = winsys.spawn_on_input_desktop if SYSTEM_MODE else None


def video_cmd(c):
    fps, w, h, br = c["fps"], c["w"], c["h"], c["br"]
    output = int(os.environ.get("RD_SCREEN", "0"))
    vf = ("hwdownload,format=bgra,"
          f"scale=w=min(iw\\,{w}):h=min(ih\\,{h}):force_original_aspect_ratio=decrease"
          ":force_divisible_by=2:flags=fast_bilinear:out_color_matrix=bt709:out_range=tv,"
          "format=nv12")
    return [
        FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "lavfi", "-i", f"ddagrab=output_idx={output}:framerate={fps}:draw_mouse=1:dup_frames=1",
        "-vf", vf, *_enc_args(pick_encoder(), br, fps * 30),
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-f", "flv", "-flvflags", "no_duration_filesize+no_metadata",
        "-flush_packets", "1", "pipe:1",
    ]


# --------------------------------------------------------------------------------------
# Audio: WASAPI loopback of the default output device, fed to ffmpeg as raw PCM
# --------------------------------------------------------------------------------------

def audio_source():
    try:
        import numpy  # noqa: F401
        import soundcard  # noqa: F401
    except ImportError:
        log.warning("audio disabled: pip install soundcard numpy")
        return None

    async def feed(stdin):
        loop = asyncio.get_running_loop()
        q = asyncio.Queue(maxsize=50)
        stop = threading.Event()

        def put(b):
            if q.full():
                q.get_nowait()          # drop the oldest block rather than adding latency
            q.put_nowait(b)

        def record():
            import numpy as np
            import soundcard as sc          # imported in this thread: it sets up COM per thread
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            with mic.recorder(samplerate=48000, channels=2, blocksize=960) as rec:
                while not stop.is_set():
                    data = rec.record(numframes=960)
                    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                    loop.call_soon_threadsafe(put, pcm)

        t = threading.Thread(target=record, name="wasapi-loopback", daemon=True)
        t.start()
        try:
            while t.is_alive() or not q.empty():
                try:
                    b = await asyncio.wait_for(q.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                stdin.write(b)
                await stdin.drain()
        finally:
            stop.set()

    return ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"], feed


# --------------------------------------------------------------------------------------
# Input: SendInput
# --------------------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE, ABSOLUTE, WHEEL, HWHEEL = 0x0001, 0x8000, 0x0800, 0x1000
# browser button order: left, middle, right, back, forward -> (down flag, up flag, mouseData)
BTN = [(0x0002, 0x0004, 0), (0x0020, 0x0040, 0), (0x0008, 0x0010, 0),
       (0x0080, 0x0100, 1), (0x0080, 0x0100, 2)]
K_EXT, K_UP, K_UNICODE, K_SCAN = 0x0001, 0x0002, 0x0004, 0x0008
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)


def _send(*inputs):
    """-> number of events Windows accepted (0 when the target desktop refused them)."""
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))


def _key(vk=0, scan=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


class Input:
    """All injection happens on one worker thread. In system mode that thread re-attaches itself to
    the input desktop whenever it changes (SetThreadDesktop only works on a thread that owns no
    windows, which a dedicated thread guarantees)."""

    def __init__(self):
        self.keys_down = set()
        self.buttons_down = set()
        self.q = queue.Queue()
        self.desk = winsys.ThreadDesktop() if SYSTEM_MODE else None
        self._checked = 0.0
        threading.Thread(target=self._worker, name="input", daemon=True).start()

    # ---- worker
    def _worker(self):
        while True:
            fn, args = self.q.get()
            try:
                if self.desk and time.monotonic() - self._checked > 0.1:
                    self._checked = time.monotonic()
                    self.desk.ensure()
                if fn(*args) == 0 and self.desk:          # SendInput refused: desktop just switched
                    self.desk.ensure()
                    fn(*args)
            except Exception:
                log.exception("input")
            finally:
                self.q.task_done()

    def _do(self, *inputs):
        self.q.put((_send, inputs))

    def flush(self):
        self.q.join()

    # ---- mouse
    def move_abs(self, x, y):
        # absolute SendInput coordinates are 0..65535 across the primary monitor, same as ours
        self._do(_mouse(MOVE | ABSOLUTE, int(x), int(y)))

    def move_rel(self, dx, dy):
        self._do(_mouse(MOVE, int(dx), int(dy)))

    def button(self, b, down):
        if not 0 <= b < len(BTN):
            return
        d, u, data = BTN[b]
        (self.buttons_down.add if down else self.buttons_down.discard)(b)
        self._do(_mouse(d if down else u, data=data))

    def wheel(self, dx, dy):
        # both use 120 per notch; Windows wheel is positive = away from the user (scroll up)
        if dy:
            self._do(_mouse(WHEEL, data=-int(dy)))
        if dx:
            self._do(_mouse(HWHEEL, data=int(dx)))

    # ---- keyboard
    def _key_input(self, code, down):
        v = keymap.WINDOWS.get(code)
        if v is None:
            return None
        if v & keymap.VK:
            return _key(vk=v & 0xFF, flags=0 if down else K_UP)
        flags = K_SCAN | (K_EXT if v & keymap.EXT else 0) | (0 if down else K_UP)
        return _key(scan=v & 0xFF, flags=flags)

    def key(self, code, down):
        held = self.keys_down
        if (down and code == "Delete" and held & {"ControlLeft", "ControlRight"}
                and held & {"AltLeft", "AltRight"}):
            # Windows ignores an injected Ctrl+Alt+Del; the SYSTEM agent can ask for a real one
            if SYSTEM_MODE:
                self.q.put((lambda: winsys.send_sas() and 1, ()))
                return
        ki = self._key_input(code, down)
        if ki is None:
            return
        (held.add if down else held.discard)(code)
        self._do(ki)

    async def type_text(self, text):
        # KEYEVENTF_UNICODE types any character regardless of the keyboard layout
        def job():
            for ch in text[:20000]:
                if ch == "\n":
                    _send(self._key_input("Enter", True), self._key_input("Enter", False))
                else:
                    units = ch.encode("utf-16-le")
                    for i in range(0, len(units), 2):
                        u = int.from_bytes(units[i:i + 2], "little")
                        _send(_key(scan=u, flags=K_UNICODE), _key(scan=u, flags=K_UNICODE | K_UP))
                time.sleep(0.002)
            return 1
        self.q.put((job, ()))

    def release_all(self):
        for code in list(self.keys_down):
            self.key(code, False)
        for b in list(self.buttons_down):
            self.button(b, False)

    def wake(self):
        self.move_rel(1, 0)
        self.move_rel(-1, 0)
