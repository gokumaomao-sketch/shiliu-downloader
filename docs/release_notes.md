# 拾流下载器 V2.1.2 · macOS arm64 预览版

此版本公开为 **Pre-release**，不是正式稳定 Release。免费提供 macOS 26.0+ Apple Silicon arm64 DMG/ZIP；Intel 未验证。复用已核验的 V2.1.2 二进制，本轮没有修改业务代码、历史快照或版本标签。

## 签名与安全限制

App 仅有 **ad-hoc 签名**，未完成 **Developer ID Application 签名与 Apple notarization**，未附带公证票据。macOS Gatekeeper 可能阻止直接启动；本机评估为 rejected。可下载不代表可无警告运行，不宣称 Apple 已验证或已公证，不提供关闭安全机制或绕过 Gatekeeper 的步骤。

## 下载文件

- `shiliu-downloader-v2.1.2-macOS-arm64.dmg`：挂载后可拖入 Applications。
- `shiliu-downloader-v2.1.2-macOS-arm64.zip`：解压得到拾流下载器 App。
- `SHA256SUMS.txt`：对应上述公开附件名称，下载后用于核验 SHA256。
- `THIRD_PARTY_BINARY_PROVENANCE.md`：第三方来源、版本、许可及二进制哈希。

## 功能与数据

普通 HTTP/HTTPS 文件下载无需 Chrome 扩展或视频号配置。Chrome 扩展与微信视频号是**可选、持续验证能力**，当前真实端到端可用性不作为本次预览发布保证，也不阻塞核心下载发布。页面视频受平台权限、网络、链接有效期与上游变动影响，不绕过 DRM 或登录权限。

默认数据目录 `~/Library/Application Support/拾流下载器`；默认下载目录 `~/Downloads/拾流下载器`，保留用户自定义路径。公开附件不包含个人任务历史、Cookie、Token 或原始用户日志。内部 Bundle Identifier 保留 `com.dodo.downloader`，不依赖旧源码目录。

## 核心复核 · 2026-10-01

- Release App、ZIP、DMG 的 1,609 个文件及符号链接条目一致；ZIP 完整性和 DMG 校验通过。
- 指定 Release App 实际 HTTPS 下载任务 `5d9a9521c372` 完成，15,086 字节，SHA256 与独立获取的源文件一致。
- 包内 FFmpeg/FFprobe 9.0.2 转封装 MP4→TS→MP4 成功；ffprobe 正常读取 H.264 160×90、AAC、1.044898 秒。
- ad-hoc 签名完整性核验通过；Gatekeeper rejected，未 staple。
- 2026-09-30 已核验普通 HTTP/HTTPS、172 项隔离核心测试、历史任务及自定义目录；自动测试不替代全部 UI 的真实体验验收。

## 第三方组件与其他限制

Node.js 24.21.0；FFmpeg/FFprobe 9.0.2 官方源码最低充分 LGPL-2.1+ 构建，随包提供对应源码与构建说明；wx_channels_download v260907（MIT + Commons Clause，仅按现有免费无对价分发边界提供）。详见来源清单。项目自身尚无根目录 LICENSE，公开源码不等于 OSI 开源授权。
