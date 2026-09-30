# macOS arm64 内置运行库来源

仅在 macOS `.app` 中打包以下可执行文件。Node 来自 Node.js 官方发布；FFmpeg/FFprobe 来自 Martin Riedl 的 macOS arm64 静态构建。FFmpeg 项目只发布源码，不发布 macOS 可执行文件。

| 文件 | 版本 | 下载源 | 发布包 SHA-256 | 解包原始可执行文件 SHA-256 |
| --- | --- | --- | --- | --- |
| `bin/node` | v24.21.0 LTS | [Node.js 官方 arm64 tar.xz](https://nodejs.org/dist/v24.21.0/node-v24.21.0-darwin-arm64.tar.xz) | `6239d4cf92d864487ec8cd3615038f7b67e7f58b77b21cd2f09ea9fbd68065fe` | `e4b5a3af0e05c75de2eae013904145f40fe7fc2a6e6f17510128bf45cca4e79b` |
| `bin/ffmpeg` | 9.0.2 | [Martin Riedl arm64 ZIP](https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip) | `c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924` | `2e11c6f90993cdb79fff84d3f90044d28316b310e75b3e030cfc9a54f2c9d384` |
| `bin/ffprobe` | 9.0.2 | [Martin Riedl arm64 ZIP](https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip) | `fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6` | `2738aa46a7f9acbc8ab09a6715554c90f7de603171ac403726765685df6a0059` |

Node 发布包 SHA-256 对照 [官方 SHASUMS256.txt](https://nodejs.org/dist/v24.21.0/SHASUMS256.txt)。FFmpeg 两个发布包 SHA-256 分别对照发布者同目录的 `ffmpeg.zip.sha256` 与 `ffprobe.zip.sha256`，本地校验均一致。三个原始可执行文件经 `file` 确认都是 Mach-O arm64；`otool -L` 显示只引用 macOS 系统库和框架。

Node 许可证见 `third_party_licenses/node-v24.21.0-LICENSE.txt`。FFmpeg 构建启用了 GPL 与 version3，许可证见 `third_party_licenses/ffmpeg-9.0.2-COPYING.GPLv3`；[FFmpeg 9.0.2 源码](https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz)及[构建脚本](https://git.martin-riedl.de/ffmpeg/build-script)可从上游获取。

PyInstaller 可能重新签署包内 Mach-O 文件，因此最终 `.app` 内可执行文件的 SHA-256 需在打包后另行记录，不能与上述解包原始值混同。
