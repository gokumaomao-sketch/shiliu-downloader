<div align="center">

**简体中文 | [English](README.en.md)**

# 拾流下载器

### 面向 macOS 的本地下载工具

普通文件下载 · 网页视频识别 · 浏览器联动 · 本地任务管理

![macOS](https://img.shields.io/badge/platform-macOS-blue)
![版本](https://img.shields.io/badge/version-v2.1.2-blue)
![Python](https://img.shields.io/badge/Python-3.14-blue)
![Chrome](https://img.shields.io/badge/Chrome-Extension-blue)

</div>

## 产品简介

拾流下载器是一款面向 macOS 的本地下载工具，目标是让“从网页发现内容 → 创建任务 → 下载 → 管理文件”这条链路更简单、更清晰。

**本地优先 · 界面简洁 · 下载链路清晰 · 浏览器与桌面端协同 · 用户数据尽量留在本机。**

本仓库提供公开源码。拾流下载器自身尚未选择根目录 LICENSE，公开源码不等同于 OSI 开源授权；第三方组件各自遵循原许可证。当前没有正式签名、公证的 V2.1.2 安装包。

## 快速导航

[主界面](#主界面) · [功能特性](#功能特性) · [快速开始](#快速开始) · [macOS 安装](#macos-安装方式) · [源码运行](#源码运行) · [浏览器扩展](#浏览器扩展) · [数据与隐私](#数据与隐私) · [FAQ](#faq) · [更新日志](#更新日志)

## 主界面

![拾流下载器主界面](assets/readme/main-window.png)

实际运行 V2.1.2 截图；6 条独立临时数据目录中的演示任务，不含用户历史、个人路径或真实下载 URL。演示时未启动浏览器服务，截图中的任务状态只用于展示。

## 产品定位

为需要管理普通下载、识别网页视频并在桌面统一查看任务的 macOS 用户提供本地工具。页面视频的可用性取决于网站、授权、网络和上游解析能力，不承诺支持所有网站。

## 功能特性

- 普通 HTTP/HTTPS 文件、多连接下载、暂停与继续、停止及删除。
- 分类筛选、搜索、进度、速度与文件管理。
- 可选 Chrome 扩展：识别页面视频、发送下载请求及接管浏览器下载；V2.1.2 Release App 的真实端到端链路仍在持续验证，不作为主程序发布门槛。
- HLS/DASH 片段下载与媒体合并；通过 yt-dlp 解析部分网站。
- FFmpeg/FFprobe 媒体处理、Node.js 辅助解析。
- 可选视频号集成：依赖独立第三方组件及用户主动配置，受平台与权限限制；持续验证，不保证当前可用，不作为主程序发布门槛。

## 快速开始

1. 按下方说明准备 Python 环境，从源码启动桌面端。
2. 点击“添加任务”，输入有权访问的普通 HTTP/HTTPS 直接文件链接，确认文件名和保存目录后开始下载。
3. 在桌面端管理任务；Chrome 扩展和视频号均为可选能力，普通下载不需要安装扩展或配置视频号。

[Releases](https://github.com/gokumaomao-sketch/shiliu-downloader/releases) 当前不提供正式 V2.1.2 DMG 下载；本地测试包未完成 Developer ID 签名与公证。

## macOS 安装方式

当前验证平台：macOS 26.0+、Apple Silicon（arm64）；实际构建环境 macOS 26.5.2。内置 Python.framework 的实际 minos 为 26.0，不能据旧 plist 宣称兼容 macOS 12；Intel 安装包未验证。

V2.1.2 二进制 Release 暂为 Draft。普通用户安装包尚未正式发布，不提供关闭 Gatekeeper 或绕过系统安全提示的操作。开发者可运行源码或按 [构建说明](docs/构建说明.md) 自行构建用于本地验证。

正式安装包发布后应核验 SHA256：ZIP 解压为 App；DMG 挂载后将 App 拖至 Applications。需要浏览器联动时，Chrome 扩展可独立加载；无需安装扩展即可使用普通下载。

## 源码运行

安装带 Tk 的 Python（本轮验证 Python 3.14），然后在仓库根目录执行：

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

依赖列表见 `requirements.txt`。FFmpeg/FFprobe 与 Node.js 是外部工具，不随公开源码提交；源代码运行时可在 PATH 安装，或按构建说明准备资源。视频号工具需从其上游下载，遵守 MIT + Commons Clause。

开发验证：

```bash
python -m pip install -r requirements-dev.txt
NO_PROXY='*' no_proxy='*' python scripts/run_tests_isolated.py
node tests/test_page_player.js
```

完整测试包含图形环境相关断言及已知旧断言，详见 [验证说明](docs/验证说明.md)，不展示虚假 build passing badge。

## 浏览器扩展

**可选、持续验证**：本轮仅以主程序为发布验收范围，尚未完成 V2.1.2 Release App 的 Chrome 真实端到端验收。普通 HTTP/HTTPS 下载不依赖此扩展。

1. 启动桌面端，确认浏览器服务已启动。
2. 打开 Chrome 的扩展管理页，开启开发者模式，选择“加载未打包的扩展程序”。
3. 选择包含 `manifest.json` 的 `browser_extension` 文件夹。源码路径为仓库内同名目录；打包版路径为 `拾流下载器.app/Contents/Resources/browser_extension`，设置 → 浏览器可查看实际路径。
4. 刷新原网页，使用页面下载按钮、扩展面板或右键菜单。

若配置了可选连接 Token，扩展与桌面端需一致；不要把 Token 放进 Issues。扩展版本为 2.1.2。更新扩展后重新加载并刷新页面。卸载源码目录会使以该目录加载的扩展失效，请使用稳定目录。

## 基础使用说明

- **添加任务**：输入有权访问的直接文件链接，确认文件名与保存位置。
- **网页视频**：保持桌面端运行，在 Chrome 页面点击扩展提供的下载按钮。
- **任务管理**：选中任务后继续、暂停、停止、删除，或打开文件/文件夹。
- **分类与搜索**：左侧筛选状态及类型，顶部按任务名称搜索。
- **下载失败**：检查网络、链接是否过期、站点权限与所需外部工具；本工具不绕过 DRM。

## 数据与隐私

- 默认运行数据：`~/Library/Application Support/拾流下载器`；默认下载：`~/Downloads/拾流下载器`。用户自定义目录会保留。
- 本地保存配置和任务历史；历史可能包含 URL、Referrer、文件名和自定义请求头。分享前必须脱敏。
- 扩展请求只发送到本机 7374 服务；下载、网站解析及依赖获取会访问相应网站，并非完全离线。
- Cookie 权限为可选，默认关闭；若主动启用，可用于本机请求。任务模型不会持久化 `cookie` 字段，但不应据此认为所有历史字段均无敏感信息。
- 仓库不包含用户数据、Cookie、Token、日志或签名凭据；保留一次性旧数据目录识别以兼容升级，正常运行仅使用新目录。
- 视频号能力可能使用本机代理/证书，只有主动启用时才应配置；请了解第三方工具行为。

## 扫码支持项目（自愿赞赏）

<div align="center">
<img src="assets/readme/qrcode.png" width="220" alt="微信赞赏二维码" />
</div>

复用此前多模态项目已经公开使用的 [微信赞赏二维码](https://github.com/gokumaomao-sketch/AI-Video-Research-Pipeline/blob/main/docs/support/wechat-appreciation.png)。用途是自愿赞赏，**不是交流群或技术支持入口**。无需付费即可访问源码或下载未来公开发行版本；赞赏不换取软件、功能、服务或许可证。

## GitHub Issues

通过 [Issues](https://github.com/gokumaomao-sketch/shiliu-downloader/issues) 报告问题。附 macOS/架构、版本、复现步骤及脱敏错误信息；不要上传 Cookie、Token、私人链接、完整任务数据库或原始用户日志。

## FAQ

**为什么没有 V2.1.2 DMG 下载？** 尚缺有效 Apple Developer ID Application 与公证条件，目前只有公开源码与 Draft Release。

**扩展为什么连接失败？** 确认桌面端运行、本机 7374 服务正常、扩展 Token 与配置一致；更新后重载扩展并刷新网页。

**所有视频都能下载吗？** 不保证。站点限制、登录、过期链接、DRM 和上游变动均可能影响可用性。

**文件和历史在哪里？** 默认路径见“数据与隐私”；自定义路径优先。

**能直接采用 MIT 吗？** 不能据此仓库推定。项目根目录尚无许可证；已有第三方许可只覆盖相应组件。

**Intel Mac 可以使用吗？** 当前本地包仅 arm64，Intel 未验证。

## 项目结构

```text
magic_downloader/       桌面端与下载模块
browser_extension/     Chrome 扩展资源
assets/readme/         产品截图及已有二维码
manifests/             扩展清单模板
tests/                 Python / JavaScript 测试
scripts/               可重复构建脚本
docs/                  构建、验证、Release Notes
third_party_licenses/  上游许可与版权声明
拾流下载器-macos.spec   PyInstaller 打包定义
```

公开仓库不保存运行目录、下载文件或第三方可执行文件。

## 第三方组件

| 组件 | 用途 / 许可证 |
| --- | --- |
| Python / Tk | 桌面运行环境；PSF / Tcl-Tk 许可 |
| requests / Pillow / cryptography | 网络、图像、加密；各自上游许可 |
| yt-dlp / yt-dlp-ejs | 网站解析；各自发行包许可 |
| Node.js 24.21.0 | 辅助解析；MIT 与发行包附带第三方许可 |
| FFmpeg / FFprobe 9.0.2 | 官方源码最低充分构建；LGPL-2.1-or-later，无外部编解码库 |
| wx_channels_download v260907 | 视频号组件；MIT + Commons Clause，禁止未经许可的 Sell |

来源、SHA256 与构建参数见 [MAC_BINARY_PROVENANCE.md](MAC_BINARY_PROVENANCE.md) 和 `third_party_licenses`。保留原上游源码 MIT 版权声明，不代表本项目整体选择 MIT。FFmpeg 为独立进程调用；本地包随附对应源码与构建说明。

## 更新日志

### V2.1.2 · 2026-09-30

V2.1.2 为发布与分发收口版本，重点完善 GitHub 首页、公开源码审计、第三方依赖合规和 macOS 发布链路；核心下载业务保持 V2.1.1 行为。

- 新鲜提取正式源码、维护可移植路径与品牌清理。
- 中英产品首页、无隐私演示截图及既有赞赏二维码。
- 官方 FFmpeg 源码构建、依赖来源与分发边界审计。
- 本地 arm64 安装包验证；正式二进制发布仍受签名与公证门槛限制。

## 主程序发布核验

2026-09-30：指定 V2.1.2 Release App 独立启动；在监测接收进程身份的条件下，普通 HTTP/HTTPS 文件下载完成且 SHA256 与源文件一致；172 项隔离核心测试通过（下载、任务管理、暂停、删除清理、持久化、默认目录同步）。这些测试不代替全部主窗口操作的人工体验验收。包内 FFmpeg/FFprobe 转封装与媒体检查通过；ZIP 完整性及 DMG 校验通过，二者内 App 与 Release App 逐文件哈希一致。历史任务、配置与自定义路径保留，SQLite 完整性正常。

签名身份检查为 0 个有效身份，App 仅 ad-hoc 签名，Gatekeeper 拒绝；未公证、未 staple，二进制继续保留 Draft/预览状态，不提供安全绕过步骤。内部 Bundle Identifier 仍为 `com.dodo.downloader`，本轮未改包标识；它不是旧数据或源码目录的加载依赖。

## 当前版本与平台

**V2.1.2 · macOS · Apple Silicon arm64 · 公开源码**。根目录 LICENSE 未选定；未宣称 Apple 官方认证、已公证或全站支持。
