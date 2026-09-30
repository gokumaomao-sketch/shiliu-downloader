#!/bin/bash
# FFmpeg 官方源码构建；无外部编解码库，GPL/nonfree/version3 均关闭。
set -euo pipefail
build_root=${1:?用法：bash scripts/build_ffmpeg_macos.sh 构建目录}
mkdir -p "$build_root"
cd "$build_root"
archive=ffmpeg-9.0.2.tar.xz
expected=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
if [ ! -f "$archive" ]; then
  curl --fail --location --retry 3 --output "$archive" https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
fi
printf '%s  %s\n' "$expected" "$archive" | shasum -a 256 -c -
test ! -e build || { printf '构建目录已存在，拒绝覆盖\n' >&2; exit 1; }
tar -xJf "$archive"
mkdir build
cd build
../ffmpeg-9.0.2/configure \
  --prefix=../install --arch=arm64 --target-os=darwin \
  --disable-everything --disable-autodetect --disable-gpl --disable-version3 \
  --disable-nonfree --disable-doc --disable-debug --disable-ffplay \
  --enable-ffmpeg --enable-ffprobe --enable-securetransport \
  --enable-protocol=file,pipe,http,https,tcp,tls,crypto \
  --enable-demuxer=mov,matroska,mpegts,aac,mp3,ogg,flac,wav,flv,hls,concat \
  --enable-muxer=mp4,mov,matroska,webm,mpegts,adts,mp3,ogg,flac,wav \
  --enable-parser=aac,h264,hevc,vp8,vp9,av1,mpegaudio,opus,vorbis,flac,mpeg4video \
  --enable-decoder=h264,hevc,vp8,vp9,aac,mp3,opus,vorbis,flac,pcm_s16le,mpeg4 \
  --enable-encoder=aac,mpeg4 \
  --enable-filter=null,anull,aformat,format,atrim,trim,aresample,scale \
  --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,extract_extradata \
  --extra-cflags=-mmacosx-version-min=12.0 \
  --extra-ldflags=-mmacosx-version-min=12.0
make -j"${SHILIU_BUILD_JOBS:-8}"
make install
../install/bin/ffmpeg -version
../install/bin/ffprobe -version
shasum -a 256 ../install/bin/ffmpeg ../install/bin/ffprobe
