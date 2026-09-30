# 拾流下载器 V2.1.2

发布与分发收口版本：完善 GitHub 首页、公开源码审计、第三方依赖合规和 macOS 发布链路。核心下载业务保持 V2.1.1 行为。

## 发布状态

本版本源码可公开；二进制仅本地测试，Release 保持 Draft。缺 Developer ID Application / notarization 条件，未宣称普通用户可无警告安装。

## 文件与安装

仅面向 macOS 26.0+ Apple Silicon arm64；Intel 未验证。正式签名/公证通过并发布后，ZIP 解压得到 App，DMG 挂载将 App 拖入 Applications。当前这些文件不作为公开安装包提供。

Chrome 扩展需加载 App 内 Contents/Resources/browser_extension 或源码同名目录，更新后重载并刷新网页。桌面端监听本机 7374。

默认数据为 ~/Library/Application Support/拾流下载器；下载目录 ~/Downloads/拾流下载器，自定义路径保留。不要公开个人历史、Cookie、Token 或日志。

## 第三方组件

Node.js 24.21.0（MIT 及组件许可）；FFmpeg/FFprobe 9.0.2（官方源码最低充分 LGPL-2.1+ 构建，随包提供对应源码）；wx_channels_download v260907（MIT + Commons Clause，免费无对价分发，不允许未经授权销售）。完整来源、许可与 SHA256 见 THIRD_PARTY_BINARY_PROVENANCE.md，ZIP/DMG 校验见 SHA256SUMS.txt。

## 已知限制

无 Developer ID 与公证；站点支持受授权、网络、链接过期和上游解析变动影响；完整测试中已有视觉/模拟多屏/YouTube 配置旧断言，未通过改变业务来绕过。项目自身无根目录 LICENSE，公开源码不等于 OSI 开源。
