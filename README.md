# 拾流下载器

拾流下载器是一款 macOS 下载工具，提供任务管理、网页视频识别和浏览器扩展连接。

源码包含主程序、浏览器扩展、视频号集成代码及测试。安装依赖见 `requirements.txt`，浏览器扩展安装见 `INSTALL_BROWSER.txt`。

源码运行时，资源从项目目录读取，运行数据写入 `~/Library/Application Support/拾流下载器`。打包运行时，资源从应用包读取，运行数据仍写入 Application Support。历史项目路径只在设置 `SHILIU_LEGACY_PROJECT_ROOT` 时解析；可用 `SHILIU_PROJECT_ROOT` 指定新项目根目录，默认使用当前资源目录。

仓库未附带第三方视频号组件与 ffmpeg 可执行文件；生成包含这些组件的完整应用包前，需要按各组件许可自行提供。
