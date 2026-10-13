"""Linux backend: KMS framebuffer capture + VAAPI H.264, uinput input, PulseAudio/PipeWire audio.

Runs inside the Docker container (needs CAP_SYS_ADMIN for kmsgrab, /dev/dri and /dev/uinput).
Because it reads the framebuffer and injects at the kernel level it works on Wayland or X11,
on the lock screen and on the display-manager login screen alike.
"""
import asyncio
import glob
import os

from evdev import AbsInfo, UInput, ecodes as e

import keymap

NAME = "linux"
FFMPEG = os.environ.get("RD_FFMPEG", "ffmpeg")


def find_card():
    if os.environ.get("RD_DRI_CARD"):
        return os.environ["RD_DRI_CARD"]
    for status in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            if open(status).read().strip() == "connected":
                return "/dev/dri/" + os.path.basename(os.path.dirname(status)).split("-")[0]
        except OSError:
            pass
    return "/dev/dri/card0"


def describe():
    return {"platform": NAME, "card": find_card()}


def video_cmd(c):
    fps, w, h, br = c["fps"], c["w"], c["h"], c["br"]
    vf = ("hwmap=derive_device=vaapi,"
          f"scale_vaapi=w=min(iw\\,{w}):h=min(ih\\,{h}):format=nv12"
          ":force_original_aspect_ratio=decrease:force_divisible_by=2"
          ":out_color_matrix=bt709:out_range=tv")
    return [
        FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-device", find_card(), "-framerate", str(fps), "-f", "kmsgrab", "-i", "-",
        "-vf", vf,
        "-c:v", "h264_vaapi", "-profile:v", "high", "-bf", "0", "-g", str(fps * 30),
        # capped VBR with a QP floor: without -qmin radeonsi keeps "refining" a static
        # screen until it spends the whole budget; with it an idle desktop is ~0.4 Mb/s
        "-rc_mode", "VBR", "-b:v", f"{br * 0.75:.2f}M", "-maxrate", f"{br}M", "-qmin", "18",
        "-bufsize", f"{br / 4:.3f}M", "-async_depth", "1",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-f", "flv", "-flvflags", "no_duration_filesize+no_metadata",
        "-flush_packets", "1", "pipe:1",
    ]


def audio_source():
    """-> (ffmpeg input args, feed coroutine or None). Captures whatever the speakers play."""
    if not os.environ.get("PULSE_SERVER"):
        return None
    return ["-f", "pulse", "-fragment_size", "3840", "-i", "@DEFAULT_MONITOR@"], None


BUTTONS = [e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT, e.BTN_SIDE, e.BTN_EXTRA]  # browser order
WHEEL = [e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES]


class Input:
    """Three virtual devices: an absolute "tablet" pointer (like a VM's USB tablet), a relative
    mouse for pointer-lock/games, and a keyboard."""

    def __init__(self):
        self.abs = UInput({
            e.EV_KEY: BUTTONS,
            e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, 65535, 0, 0, 0)),
                       (e.ABS_Y, AbsInfo(0, 0, 65535, 0, 0, 0))],
            e.EV_REL: WHEEL,
        }, name="RemoteFunnel Pointer", bustype=e.BUS_USB, vendor=0x1d6b, product=0x0201)
        self.rel = UInput({e.EV_KEY: BUTTONS, e.EV_REL: [e.REL_X, e.REL_Y] + WHEEL},
                          name="RemoteFunnel Mouse", bustype=e.BUS_USB, vendor=0x1d6b, product=0x0202)
        self.kbd = UInput({e.EV_KEY: list(range(1, 249))},
                          name="RemoteFunnel Keyboard", bustype=e.BUS_USB, vendor=0x1d6b, product=0x0203)
        self.keys_down = set()
        self.buttons_down = set()
        self.wheel_acc = [0, 0]

    def move_abs(self, x, y):
        self.abs.write(e.EV_ABS, e.ABS_X, max(0, min(65535, int(x))))
        self.abs.write(e.EV_ABS, e.ABS_Y, max(0, min(65535, int(y))))
        self.abs.syn()

    def move_rel(self, dx, dy):
        self.rel.write(e.EV_REL, e.REL_X, int(dx))
        self.rel.write(e.EV_REL, e.REL_Y, int(dy))
        self.rel.syn()

    def button(self, b, down):
        if not 0 <= b < len(BUTTONS):
            return
        code = BUTTONS[b]
        (self.buttons_down.add if down else self.buttons_down.discard)(code)
        self.abs.write(e.EV_KEY, code, 1 if down else 0)
        self.abs.syn()

    def wheel(self, dx, dy):
        # dx/dy are in hi-res units (120 = one notch), browser sign convention (down = +)
        for axis, v, lo, hi in ((0, -dy, e.REL_WHEEL, e.REL_WHEEL_HI_RES),
                                (1, dx, e.REL_HWHEEL, e.REL_HWHEEL_HI_RES)):
            v = int(v)
            if not v:
                continue
            self.abs.write(e.EV_REL, hi, v)
            self.wheel_acc[axis] += v
            notches = int(self.wheel_acc[axis] / 120)
            if notches:
                self.wheel_acc[axis] -= notches * 120
                self.abs.write(e.EV_REL, lo, notches)
        self.abs.syn()

    def key(self, code, down):
        k = keymap.LINUX.get(code)
        if k is None:
            return
        (self.keys_down.add if down else self.keys_down.discard)(k)
        self.kbd.write(e.EV_KEY, k, 1 if down else 0)
        self.kbd.syn()

    def _tap(self, k, shift=False):
        if shift:
            self.kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        self.kbd.write(e.EV_KEY, k, 1)
        self.kbd.syn()
        self.kbd.write(e.EV_KEY, k, 0)
        if shift:
            self.kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
        self.kbd.syn()

    async def type_text(self, text):
        # uinput can only press keys, so text is typed through a US layout
        for ch in text[:20000]:
            m = keymap.US_CHARS.get(ch)
            if m:
                self._tap(keymap.LINUX[m[0]], m[1])
                await asyncio.sleep(0.004)

    def release_all(self):
        for k in list(self.keys_down):
            self.kbd.write(e.EV_KEY, k, 0)
        self.kbd.syn()
        for b in list(self.buttons_down):
            self.abs.write(e.EV_KEY, b, 0)
        self.abs.syn()
        self.keys_down.clear()
        self.buttons_down.clear()

    def wake(self):
        # tiny relative wiggle: wakes a DPMS-off screen without visibly moving the cursor
        self.move_rel(1, 0)
        self.move_rel(-1, 0)
