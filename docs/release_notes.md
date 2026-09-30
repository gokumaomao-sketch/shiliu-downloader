# 拾流下载器 V2.1.2

发布与分发收口版本：完善 GitHub 首页、公开源码审计、第三方依赖合规和 macOS 发布链路。核心下载业务保持 V2.1.1 行为。

## 发布状态

本版本源码可公开；二进制仅本地测试，Release 保持 Draft。缺 Developer ID Application / notarization 条件，未宣称普通用户可无警告安装。

## 文件与安装

仅面向 macOS 26.0+ Apple Silicon arm64；Intel 未验证。正式签名/公证通过并发布后，ZIP 解压得到 App，DMG 挂载将 App 拖入 Applications。当前这些文件不作为公开安装包提供。

普通 HTTP/HTTPS 下载可直接从主程序添加任务，无需 Chrome 扩展或视频号配置。Chrome 扩展为可选、持续验证能力，V2.1.2 Release App 真实端到端验收尚未完成；如需使用，加载 App 内 Contents/Resources/browser_extension 或源码同名目录。视频号为可选、持续验证能力，依赖第三方组件与平台权限，不保证当前可用。两者均不阻塞主程序发布；本轮不修改扩展或视频号。

默认数据为 ~/Library/Application Support/拾流下载器；下载目录 ~/Downloads/拾流下载器，自定义路径保留。不要公开个人历史、Cookie、Token 或日志。

## 第三方组件

Node.js 24.21.0（MIT 及组件许可）；FFmpeg/FFprobe 9.0.2（官方源码最低充分 LGPL-2.1+ 构建，随包提供对应源码）；wx_channels_download v260907（MIT + Commons Clause，免费无对价分发，不允许未经授权销售）。完整来源、许可与 SHA256 见 THIRD_PARTY_BINARY_PROVENANCE.md，ZIP/DMG 校验见 SHA256SUMS.txt。

## 已知限制

无 Developer ID 与公证；站点支持受授权、网络、链接过期和上游解析变动影响；完整测试中已有视觉/模拟多屏/YouTube 配置旧断言，未通过改变业务来绕过。项目自身无根目录 LICENSE，公开源码不等于 OSI 开源。

## 本轮主程序核验（2026-09-30）

- 指定 Release App 独立启动，普通 HTTP/HTTPS 下载均由 V2.1.2 进程接收、完成并通过源文件 SHA256 校验。
- 172 项隔离核心测试通过，覆盖下载、任务管理、暂停、删除清理、持久化与默认目录同步；全部主窗口操作的真实体验验收未完成，不能用自动测试替代。
- 包内 FFmpeg/FFprobe 9.0.2 转封装及媒体检查正常；ZIP 完整性、DMG 校验通过；两种包内 App 与 Release App 的 1585 个文件哈希一致。
- 基线历史任务、配置及用户自定义目录保持一致，SQLite integrity_check 为 ok；默认数据与下载路径见上文。
- 无有效 Developer ID 签名身份；仅 ad-hoc 签名，Gatekeeper rejected，未公证、未 staple。GitHub 二进制 Release 保持 Draft/预览，不绕过系统安全机制。
- 内部 Bundle Identifier 保留 com.dodo.downloader；它不是旧数据目录或开发源码路径加载依赖，本轮不修改包标识。
