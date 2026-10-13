# --- stage 1: a small ffmpeg with kmsgrab + VAAPI (video) and pulse + libopus (audio),
#     patched so kmsgrab/VAAPI accept the 10-bit scanout buffers KWin uses ---
FROM archlinux:latest AS ffmpeg
ARG FFMPEG_VERSION=9.0.2
RUN pacman -Syu --noconfirm --needed base-devel nasm libva libdrm libpulse opus python curl
WORKDIR /build
RUN curl -sSfL https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz | tar xJ --strip-components=1
COPY ffmpeg/10bit-rgb.py /build/
RUN python 10bit-rgb.py \
 && ./configure --prefix=/opt/ffmpeg --disable-everything --disable-doc --disable-debug \
      --disable-ffplay --disable-ffprobe --disable-network --disable-autodetect \
      --enable-libdrm --enable-vaapi --enable-libpulse --enable-libopus \
      --enable-indev=kmsgrab,pulse --enable-decoder=wrapped_avframe,pcm_s16le,pcm_f32le \
      --enable-filter=hwmap,hwupload,hwdownload,scale_vaapi,format,null,fps,aresample,aformat,anull \
      --enable-encoder=h264_vaapi,hevc_vaapi,libopus \
      --enable-muxer=flv,ogg,null --enable-protocol=pipe,file \
 && make -j"$(nproc)" && make install

# --- stage 2: runtime ---
FROM archlinux:latest
RUN pacman -Syu --noconfirm --needed mesa libva libdrm libpulse opus python python-aiohttp python-evdev \
 && pacman -Scc --noconfirm
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
WORKDIR /app
COPY app.py media.py keymap.py platform_linux.py client.html ./
ENV PYTHONUNBUFFERED=1 RD_BIND=0.0.0.0
CMD ["python", "/app/app.py"]
