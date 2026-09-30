# -*- mode: python ; coding: utf-8 -*-
"""Apple Silicon macOS application bundle for 拾流下载器.

Build from the project root with:
  pyinstaller --noconfirm --distpath ../01-应用程序 --workpath <temporary-build-dir> 拾流下载器-macos.spec
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("browser_extension", "browser_extension"),
    ("INSTALL_BROWSER.txt", "."),
    ("MAC_BINARY_PROVENANCE.md", "."),
    ("logo_toolbar.png", "."),
    ("third_party_licenses", "third_party_licenses"),
    ("third_party/wx_video_download", "third_party"),
    ("third_party/macos_arm64/bin/node", "bin"),
    ("third_party/macos_arm64/bin/ffmpeg", "bin"),
    ("third_party/macos_arm64/bin/ffprobe", "bin"),
]
binaries = []
hiddenimports = []

# yt-dlp discovers its site extractors and EJS challenge assets dynamically.
for package in ("yt_dlp", "yt_dlp_ejs"):
    package_datas, package_bins, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_bins
    hiddenimports += package_hidden

hiddenimports += collect_submodules("cryptography")
hiddenimports += ["PIL.Image", "PIL.ImageTk"]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "mypy", "pystray", "winreg"],
    noarchive=False,
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="拾流下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="拾流下载器",
)

app = BUNDLE(
    coll,
    name="拾流下载器.app",
    icon="拾流下载器.icns",
    bundle_identifier="com.dodo.downloader",
    target_arch="arm64",
    info_plist={
        "CFBundleName": "拾流下载器",
        "CFBundleDisplayName": "拾流下载器",
        "CFBundleShortVersionString": "2.1.0",
        "CFBundleVersion": "2.1.0",
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
    },
)
