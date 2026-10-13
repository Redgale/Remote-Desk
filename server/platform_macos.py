"""macOS backend: AVFoundation screen capture, VideoToolbox H.264, Quartz CGEvent input,
audio through a BlackHole loopback device.

Needs two privacy permissions for the Python binary that runs the agent: Screen Recording
(capture) and Accessibility (input). macOS has no built-in way to capture system audio from
ffmpeg, so audio needs the free BlackHole driver plus a Multi-Output Device (see README).
"""
import asyncio
import logging
import os
import re
import subprocess
import time

import Quartz as Q

import keymap

NAME = "macos"
FFMPEG = os.environ.get("RD_FFMPEG", "ffmpeg")
log = logging.getLogger("rd.macos")


def _avf_devices():
    """-> (video devices, audio devices) as lists of (index, name) from ffmpeg's listing."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True)
    video, audio, cur = [], [], None
    for line in r.stderr.splitlines():
        if "video devices" in line:
            cur = video
        elif "audio devices" in line:
            cur = audio
        else:
            m = re.search(r"\[(\d+)\] (.+)$", line)
            if m and cur is not None:
                cur.append((int(m.group(1)), m.group(2).strip()))
    return video, audio


def _screen_index():
    if os.environ.get("RD_SCREEN"):
        return os.environ["RD_SCREEN"]
    video, _ = _avf_devices()
    screens = [i for i, n in video if n.startswith("Capture screen")]
    return str(screens[0]) if screens else "1"


def describe():
    return {"platform": NAME, "screen": _screen_index()}


def video_cmd(c):
    fps, w, h, br = c["fps"], c["w"], c["h"], c["br"]
    vf = (f"scale=w=min(iw\\,{w}):h=min(ih\\,{h}):force_original_aspect_ratio=decrease"
          ":force_divisible_by=2:flags=fast_bilinear:out_color_matrix=bt709:out_range=tv,format=nv12")
    return [
        FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "avfoundation", "-capture_cursor", "1", "-capture_mouse_clicks", "0",
        "-framerate", str(fps), "-pixel_format", "nv12", "-i", f"{_screen_index()}:none",
        "-vf", vf,
        "-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "1", "-profile:v", "high",
        "-bf", "0", "-g", str(fps * 30),
        "-b:v", f"{br * 0.75:.2f}M", "-maxrate", f"{br}M", "-bufsize", f"{br / 4:.3f}M",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-f", "flv", "-flvflags", "no_duration_filesize+no_metadata",
        "-flush_packets", "1", "pipe:1",
    ]


def audio_source():
    """Capture from a loopback device (BlackHole) if one is installed."""
    want = os.environ.get("RD_AUDIO_DEVICE", "BlackHole")
    _, audio = _avf_devices()
    for i, n in audio:
        if want.lower() in n.lower():
            return ["-f", "avfoundation", "-i", f":{i}"], None
    log.warning("audio disabled: no '%s' input device (install BlackHole, see README)", want)
    return None


# --------------------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------------------

# browser button order: left, middle, right, back, forward -> (CG button, down, up, dragged)
_BTN = [
    (Q.kCGMouseButtonLeft, Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventLeftMouseDragged),
    (Q.kCGMouseButtonCenter, Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged),
    (Q.kCGMouseButtonRight, Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGEventRightMouseDragged),
    (3, Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged),
    (4, Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged),
]
# modifier keycodes -> event flag
_MODS = {0x38: Q.kCGEventFlagMaskShift, 0x3C: Q.kCGEventFlagMaskShift,
         0x3B: Q.kCGEventFlagMaskControl, 0x3E: Q.kCGEventFlagMaskControl,
         0x3A: Q.kCGEventFlagMaskAlternate, 0x3D: Q.kCGEventFlagMaskAlternate,
         0x37: Q.kCGEventFlagMaskCommand, 0x36: Q.kCGEventFlagMaskCommand}
CAPS = 0x39


def _post(ev):
    Q.CGEventPost(Q.kCGHIDEventTap, ev)


def _cursor():
    return Q.CGEventGetLocation(Q.CGEventCreate(None))


class Input:
    def __init__(self):
        self.pos = _cursor()
        self.held = []            # browser button indices currently down
        self.mods = {}            # keycode -> flag, for modifiers held
        self.caps = False
        self.keys_down = set()
        self.last_click = (0.0, -1, 0.0, 0.0)   # time, button, x, y
        self.click_count = 1

    def _flags(self):
        f = 0
        for v in self.mods.values():
            f |= v
        if self.caps:
            f |= Q.kCGEventFlagMaskAlphaShift
        return f

    def _bounds(self):
        return Q.CGDisplayBounds(Q.CGMainDisplayID())

    def _mouse(self, kind, button, dx=0, dy=0, clicks=None):
        ev = Q.CGEventCreateMouseEvent(None, kind, self.pos, button)
        if clicks:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, clicks)
        if button > 2:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventButtonNumber, button)
        if dx or dy:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, int(dx))
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, int(dy))
        Q.CGEventSetFlags(ev, self._flags())
        _post(ev)

    def _moved(self, dx=0, dy=0):
        if self.held:
            btn, _, _, drag = _BTN[self.held[-1]]
            self._mouse(drag, btn, dx, dy)
        else:
            self._mouse(Q.kCGEventMouseMoved, Q.kCGMouseButtonLeft, dx, dy)

    def move_abs(self, x, y):
        b = self._bounds()
        self.pos = Q.CGPointMake(b.origin.x + x / 65535.0 * (b.size.width - 1),
                                 b.origin.y + y / 65535.0 * (b.size.height - 1))
        self._moved()

    def move_rel(self, dx, dy):
        b = self._bounds()
        self.pos = Q.CGPointMake(min(max(self.pos.x + dx, b.origin.x), b.origin.x + b.size.width - 1),
                                 min(max(self.pos.y + dy, b.origin.y), b.origin.y + b.size.height - 1))
        self._moved(dx, dy)

    def button(self, b, down):
        if not 0 <= b < len(_BTN):
            return
        btn, d, u, _ = _BTN[b]
        if down:
            # macOS apps only see double-clicks if the click count is set on the events
            t, lb, lx, ly = self.last_click
            now = time.monotonic()
            close = abs(self.pos.x - lx) < 5 and abs(self.pos.y - ly) < 5
            self.click_count = self.click_count + 1 if (lb == b and now - t < 0.5 and close) else 1
            self.last_click = (now, b, self.pos.x, self.pos.y)
            if b not in self.held:
                self.held.append(b)
            self._mouse(d, btn, clicks=self.click_count)
        else:
            if b in self.held:
                self.held.remove(b)
            self._mouse(u, btn, clicks=self.click_count)

    def wheel(self, dx, dy):
        # incoming values: 120 per notch, browser sign (down/right = +); ~100 px per notch
        ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 2,
                                             int(-dy / 1.2), int(-dx / 1.2))
        _post(ev)

    def key(self, code, down):
        kc = keymap.MACOS.get(code)
        if kc is None:
            return
        (self.keys_down.add if down else self.keys_down.discard)(code)
        ev = Q.CGEventCreateKeyboardEvent(None, kc, down)
        if kc in _MODS:
            if down:
                self.mods[kc] = _MODS[kc]
            else:
                self.mods.pop(kc, None)
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        elif kc == CAPS:
            if down:
                self.caps = not self.caps
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        Q.CGEventSetFlags(ev, self._flags())
        _post(ev)

    async def type_text(self, text):
        # CGEventKeyboardSetUnicodeString types any character regardless of keyboard layout
        for ch in text[:20000]:
            if ch == "\n":
                self.key("Enter", True)
                self.key("Enter", False)
            else:
                for down in (True, False):
                    ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                    Q.CGEventKeyboardSetUnicodeString(ev, len(ch.encode("utf-16-le")) // 2, ch)
                    _post(ev)
            await asyncio.sleep(0.002)

    def release_all(self):
        for code in list(self.keys_down):
            self.key(code, False)
        for b in list(self.held):
            self.button(b, False)
        self.mods.clear()

    def wake(self):
        self.pos = _cursor()     # the local user may have moved the mouse since our last event
        self.move_rel(1, 0)
        self.move_rel(-1, 0)
