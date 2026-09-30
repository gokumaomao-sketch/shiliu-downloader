# V2.1.2 macOS arm64 二进制来源与分发审计

源码仓库不提交可执行文件。下列资源仅用于本地测试打包；正式二进制发布另受 Developer ID 与 notarization 门槛限制。

| 组件 | 版本 | 原始可执行文件 SHA256 |
| --- | --- | --- |
| Node.js | 24.21.0 | `e4b5a3af0e05c75de2eae013904145f40fe7fc2a6e6f17510128bf45cca4e79b` |
| FFmpeg | 9.0.2 | `45815084565c4ae1e95d737b5b575dceef1256a348675702e8f730478aefd8e8` |
| FFprobe | 9.0.2 | `7c966a0d1f39fea5bae8cd6c23b753236c90c3e9d7b9fe9c63c84923ab2fe0ee` |
| wx_video_download | v260907 | `2667cec365beb4de81ba14f2ae0ef07b83815fc5cc46307c36619fa89c6b950b` |

## Node.js

[官方 arm64 发布包](https://nodejs.org/dist/v24.21.0/node-v24.21.0-darwin-arm64.tar.xz)，包 SHA256：`6239d4cf92d864487ec8cd3615038f7b67e7f58b77b21cd2f09ea9fbd68065fe`；对照 [官方校验表](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt)。MIT 及发行包附带组件许可全文随 App 分发，见 `third_party_licenses/node-v24.21.0-LICENSE.txt`。保留版权与许可后可免费再分发。

## FFmpeg / FFprobe

仅使用 [FFmpeg 官方 9.0.2 源码](https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz)，源码 SHA256：`8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e`。未修改上游源码。构建脚本及完整 configure 参数：`scripts/build_ffmpeg_macos.sh`。

实际环境：macOS 26.5.2 arm64、Apple clang 17.0.0（clang-1700.6.3.2）、Apple make 3.81；部署目标 macOS 12.0。关闭 autodetect/GPL/version3/nonfree、无外部编解码库；TLS 使用 macOS SecureTransport。仅系统库/框架与 FFmpeg 官方源码内部库。许可证为 LGPL-2.1-or-later，见 [官方许可说明](https://ffmpeg.org/legal.html) 和随附完整 LGPLv2.1 文本。

FFmpeg 作为独立命令行进程使用；FFmpeg 自身静态包含其 LGPL 内部库，不链接进 Python 程序。App 内 `third_party_sources` 随附对应官方源码包、构建脚本及构建说明，可自行修改和重建 FFmpeg；不限制对该组件的调试、修改或替换。未来提供正式二进制下载时，必须继续随包提供对应源码，不能只提供不对应的上游链接。旧预编译 GPLv3 构建不进入本轮包。

实际用例已通过：H.264/AAC TS→MP4、两个片段 concat→MP4、MP4→TS；调用拾流现有 `mux_to_mp4`、`concat_via_demuxer`、`mux_to_ts`，并用新 ffprobe 确认音视频流与时长。该最低充分构建不承诺任意编码器/格式的转码能力。

## wx_video_download

来源：[v260907 上游 Release](https://github.com/ltaoo/wx_channels_download/releases/tag/v260907)，macOS arm64 包 SHA256：`c7b302db0b9e9f041c99c1bb60d086a9060eaed6244501f6d435848b0cc6df12`。原始二进制已与官方压缩包逐字节核对。

已读取 [版本许可证全文](https://github.com/ltaoo/wx_channels_download/blob/v260907/LICENSE)，包括 Commons Clause v1.0、MIT 及 copyright 2025 ltaoo。许可明确包括 wx_video_download 名称下的二进制。MIT 分发权受到禁止 Sell 的条件约束，不能只按 MIT 判断。

本轮结论仅限：不收取费用、不以其他对价交换软件或服务的免费 GitHub 分发，保留完整版权、MIT 与 Commons Clause 条件，可以随免费 App/Release 分发；无需强制改为用户自行安装。禁止销售、付费下载、付费功能或主要价值来自该组件的收费服务，除非取得另行许可。README 的可选赞赏不换取软件/功能/服务/许可，不构成下载前提。收费分发须重新审计，不沿用本结论。

## 其他许可与构建后的校验

`third_party_licenses/upstream-MIT.txt` 保留原源码作者 Bayoumi 的版权许可，不代表拾流整体的根目录 LICENSE。Python 包各自许可随 PyInstaller 收集的发行包元数据保留。App 中另附实际运行依赖的许可清单。

PyInstaller 的 ad-hoc 重签可能改变 Mach-O 文件字节。上述为打包前 SHA256；最终包内 SHA256 必须另列在 Release 目录的 `THIRD_PARTY_BINARY_PROVENANCE.md`，不得混为一谈。免费再分发边界不等于已取得 Apple 公证。
