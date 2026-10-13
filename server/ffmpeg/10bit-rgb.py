"""Patch ffmpeg so kmsgrab + VAAPI accept the 10-bit RGB scanout buffers KWin uses
(AR30 / XB30 / AB30). Alpha is treated as padding, which is what scanout does anyway."""
import sys

def patch(path, anchor, extra):
    src = open(path).read()
    if extra in src:
        return
    if anchor not in src:
        sys.exit(f"anchor not found in {path}: {anchor!r}")
    open(path, "w").write(src.replace(anchor, anchor + extra, 1))

patch("libavdevice/kmsgrab.c",
      "    { AV_PIX_FMT_X2RGB10BE, DRM_FORMAT_XRGB2101010 | DRM_FORMAT_BIG_ENDIAN },\n",
      "    { AV_PIX_FMT_X2RGB10LE, DRM_FORMAT_ARGB2101010 },\n"
      "    { AV_PIX_FMT_X2BGR10LE, DRM_FORMAT_XBGR2101010 },\n"
      "    { AV_PIX_FMT_X2BGR10LE, DRM_FORMAT_ABGR2101010 },\n")
patch("libavutil/hwcontext_vaapi.c",
      "    MAP(X2R10G10B10, RGB32_10, X2RGB10, 0),\n",
      "    MAP(X2B10G10R10, RGB32_10, X2BGR10, 0),\n")
patch("libavutil/hwcontext_vaapi.c",
      "    DRM_MAP(X2R10G10B10, 1, DRM_FORMAT_XRGB2101010),\n",
      "    DRM_MAP(X2R10G10B10, 1, DRM_FORMAT_ARGB2101010),\n"
      "    DRM_MAP(X2B10G10R10, 1, DRM_FORMAT_XBGR2101010),\n"
      "    DRM_MAP(X2B10G10R10, 1, DRM_FORMAT_ABGR2101010),\n")
print("patched")
