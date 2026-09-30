"""Lifecycle for the bundled WeChat Channels helper.

The helper is only started when the user selects the video-channel action.
While active it uses a short-lived, per-user certificate and a local proxy; the
previous Windows proxy values are restored when it stops.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import time
import uuid
import sys
if sys.platform == "win32":
    import winreg
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from magic_downloader.paths import DATA_ROOT, RESOURCE_ROOT


_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_PROXY = "127.0.0.1:2023"


@dataclass
class ChannelsState:
    proxy_enable: int
    proxy_server: str | None
    proxy_override: str | None
    auto_config_url: str | None
    certificate_thumbprint: str


class ChannelsService:
    """Start and stop the bundled local video-channel helper safely."""

    def __init__(self) -> None:
        self.runtime = DATA_ROOT / "channels"
        self.state_path = self.runtime / "active.json"
        self.process: subprocess.Popen | None = None
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.recover_if_needed()

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def start(self, download_dir: Path) -> None:
        if self.running:
            return
        helper = RESOURCE_ROOT / "third_party" / "wx_video_download.exe"
        if not helper.exists():
            raise RuntimeError("视频号组件缺失，请重新安装拾流下载器。")
        if self._port_is_open(2023):
            raise RuntimeError("本机端口 2023 正在被占用，无法启动视频号下载。")

        state = self._read_proxy_state()
        cert_name = f"拾流下载器 Video Channels {uuid.uuid4().hex[:12]}"
        cert_path, key_path, thumbprint = self._write_ca(cert_name)
        self._install_certificate(cert_path)
        try:
            config_path = self.runtime / "config.yaml"
            self._write_config(config_path, download_dir, cert_path, key_path, cert_name, state.proxy_server)
            self.state_path.write_text(
                json.dumps({**state.__dict__, "certificate_thumbprint": thumbprint}, ensure_ascii=False),
                encoding="utf-8",
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.process = subprocess.Popen(
                [str(helper), "--config", str(config_path)],
                cwd=self.runtime,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            if not self._wait_port(2023, 8):
                raise RuntimeError("视频号组件未能启动，请关闭占用 2023 端口的软件后重试。")
            self._set_proxy(_PROXY, 1, state.proxy_override, state.auto_config_url)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._restore_saved_state()

    def recover_if_needed(self) -> None:
        """Repair a prior cleanly-stopped session before a new helper starts."""
        if self.state_path.exists() and self._current_proxy_server() == _PROXY:
            self._restore_saved_state()

    def _restore_saved_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = ChannelsState(**raw)
        except (OSError, ValueError, TypeError):
            return
        if self._current_proxy_server() == _PROXY:
            self._set_proxy(state.proxy_server, state.proxy_enable, state.proxy_override, state.auto_config_url)
        subprocess.run(
            ["certutil.exe", "-user", "-delstore", "Root", state.certificate_thumbprint],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for name in ("test-ca.pem", "test-ca-key.pem", "active.json"):
            try:
                (self.runtime / name).unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_value(key, name: str) -> str | None:
        try:
            return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def _read_proxy_state(self) -> ChannelsState:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_READ) as key:
            enabled = int(self._read_value(key, "ProxyEnable") or 0)
            return ChannelsState(
                proxy_enable=enabled,
                proxy_server=self._read_value(key, "ProxyServer"),
                proxy_override=self._read_value(key, "ProxyOverride"),
                auto_config_url=self._read_value(key, "AutoConfigURL"),
                certificate_thumbprint="",
            )

    def _current_proxy_server(self) -> str | None:
        try:
            return self._read_proxy_state().proxy_server
        except OSError:
            return None

    def _set_proxy(self, server: str | None, enabled: int, override: str | None, auto_url: str | None) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(enabled))
            for name, value in (("ProxyServer", server), ("ProxyOverride", override), ("AutoConfigURL", auto_url)):
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)
        wininet.InternetSetOptionW(0, 37, 0, 0)

    def _write_ca(self, name: str) -> tuple[Path, Path, str]:
        from datetime import datetime, timedelta, timezone

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256())
        )
        cert_path, key_path = self.runtime / "test-ca.pem", self.runtime / "test-ca-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        return cert_path, key_path, cert.fingerprint(hashes.SHA1()).hex().upper()

    @staticmethod
    def _install_certificate(cert_path: Path) -> None:
        result = subprocess.run(
            ["certutil.exe", "-user", "-addstore", "Root", str(cert_path)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("无法安装视频号所需的本地证书。")

    def _write_config(self, path: Path, download_dir: Path, cert: Path, key: Path, name: str, upstream: str | None) -> None:
        download_dir.mkdir(parents=True, exist_ok=True)
        def quote(value: Path | str) -> str:
            return '"' + str(value).replace('\\', '/').replace('"', '\\"') + '"'
        path.write_text(
            "\n".join((
                "download:", f"  dir: {quote(download_dir)}", "  playDoneAudio: false",
                "api:", "  hostname: \"127.0.0.1\"", "  port: 2022",
                "db:", "  type: \"sqlite\"", "  filepath: \"%CWD%/data.db\"",
                "proxy:", "  enabled: true", "  system: false", "  hostname: \"127.0.0.1\"", "  port: 2023",
                "  skipInstallRootCert: true", f"  upstreamProxy: {quote('http://' + upstream) if upstream else '\"\"'}",
                "update:", "  sources:", "    - enabled: false",
                "cert:", f"  file: {quote(cert)}", f"  key: {quote(key)}", f"  name: {quote(name)}", "",
            )), encoding="utf-8")

    @staticmethod
    def _port_is_open(port: int) -> bool:
        with socket.socket() as sock:
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _wait_port(self, port: int, seconds: int) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                return False
            if self._port_is_open(port):
                return True
            time.sleep(.1)
        return False
