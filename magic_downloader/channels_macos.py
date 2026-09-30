"""Darwin lifecycle for the official wx_channels_download helper."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from magic_downloader.channels_service import ChannelsService
from magic_downloader.paths import DATA_ROOT, RESOURCE_ROOT


PROXY_HOST = "127.0.0.1"
PROXY_PORT = "2023"
KEYCHAIN = Path.home() / "Library/Keychains/login.keychain-db"


def _run(*args: str, timeout: int = 15) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:2])} 失败：{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _scutil_value(key: str, field: str) -> str:
    result = subprocess.run(["/usr/sbin/scutil"], input=f"show {key}\n", capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"无法读取 macOS 网络服务：{key}")
    match = re.search(rf"^\s*{re.escape(field)}\s*:\s*(.+)$", result.stdout, re.M)
    return match.group(1).strip() if match else ""


def _primary_service() -> str:
    for protocol in ("IPv4", "IPv6"):
        service_id = _scutil_value(f"State:/Network/Global/{protocol}", "PrimaryService")
        if service_id:
            if not re.fullmatch(r"[0-9A-Fa-f-]{36}", service_id):
                raise RuntimeError("主网络服务标识格式异常")
            name = _scutil_value(f"Setup:/Network/Service/{service_id}", "UserDefinedName")
            if name:
                return name
    raise RuntimeError("无法确定当前主网络服务")


def _proxy_state(service: str, kind: str) -> dict:
    flag = "-getwebproxy" if kind == "http" else "-getsecurewebproxy"
    raw = _run("/usr/sbin/networksetup", flag, service)
    values = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
    return {
        "enabled": values.get("Enabled", "").strip().lower() == "yes",
        "server": values.get("Server", "").strip(),
        "port": values.get("Port", "").strip(),
        "authenticated": values.get("Authenticated Proxy Enabled", "0").strip() == "1",
    }


def _set_proxy(service: str, kind: str, server: str, port: str, enabled: bool) -> None:
    prefix = "web" if kind == "http" else "secureweb"
    if server and port:
        _run("/usr/sbin/networksetup", f"-set{prefix}proxy", service, server, port)
    _run("/usr/sbin/networksetup", f"-set{prefix}proxystate", service, "on" if enabled else "off")


class MacChannelsService(ChannelsService):
    """Own only the proxy and certificate installed for this app session."""

    def __init__(self) -> None:
        self.runtime = DATA_ROOT / "channels"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime / "active-macos.json"
        self.process: subprocess.Popen | None = None
        self.recover_if_needed()

    def start(self, download_dir: Path) -> None:
        if self.running:
            return
        helper = RESOURCE_ROOT / "third_party" / "wx_video_download"
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise RuntimeError(f"缺少可执行的 macOS arm64 视频号组件：{helper}")
        if self._port_is_open(2023) or self._port_is_open(2022):
            raise RuntimeError("视频号组件端口 2022 或 2023 已被占用")
        service = _primary_service()
        prior = {kind: _proxy_state(service, kind) for kind in ("http", "https")}
        if any(value["authenticated"] for value in prior.values()):
            raise RuntimeError("当前代理需要身份验证，无法安全接入视频号上游代理")
        upstream = prior["http"]
        upstream_address = f"{upstream['server']}:{upstream['port']}" if upstream["enabled"] and upstream["server"] else None
        name = f"拾流下载器 Video Channels {os.getpid()} {int(time.time())}"
        cert, key, thumbprint = self._write_ca(name)
        key.chmod(0o600)
        state = {"service": service, "prior": prior, "thumbprint": thumbprint, "certificate_name": name, "helper": str(helper), "pid": None}
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        try:
            _run("/usr/bin/security", "add-trusted-cert", "-r", "trustRoot", "-k", str(KEYCHAIN), str(cert), timeout=120)
            config = self.runtime / "config.yaml"
            self._write_config(config, download_dir, cert, key, name, upstream_address)
            log = (self.runtime / "helper.log").open("ab")
            try:
                self.process = subprocess.Popen(
                    [str(helper), "--config", str(config)], cwd=self.runtime,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log.close()
            state["pid"] = self.process.pid
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            if not self._wait_port(2023, 15) or not self._wait_port(2022, 5):
                raise RuntimeError(f"视频号组件启动失败，请查看 {self.runtime / 'helper.log'}")
            for kind in ("http", "https"):
                _set_proxy(service, kind, PROXY_HOST, PROXY_PORT, True)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        self._restore_saved_state()

    def recover_if_needed(self) -> None:
        if self.state_path.exists():
            self._restore_saved_state()

    def _restore_saved_state(self) -> None:
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        pid = state.get("pid")
        if pid and pid != os.getpid():
            command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
            if command.startswith(state["helper"] + " "):
                os.kill(pid, signal.SIGTERM)
        service = state["service"]
        errors = []
        for kind in ("http", "https"):
            try:
                current = _proxy_state(service, kind)
                if current["enabled"] and current["server"] == PROXY_HOST and current["port"] == PROXY_PORT:
                    old = state["prior"][kind]
                    _set_proxy(service, kind, old["server"], old["port"], old["enabled"])
            except Exception as exc:
                errors.append(str(exc))
        try:
            query = subprocess.run(
                ["/usr/bin/security", "find-certificate", "-a", "-c", state["certificate_name"], "-Z", str(KEYCHAIN)],
                capture_output=True, text=True, timeout=15,
            )
            if query.returncode:
                raise RuntimeError(query.stderr.strip())
            if f"SHA-1 hash: {state['thumbprint']}" in query.stdout:
                _run("/usr/bin/security", "delete-certificate", "-Z", state["thumbprint"], str(KEYCHAIN), timeout=30)
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise RuntimeError("视频号退出清理失败：" + "; ".join(errors))
        for name in ("test-ca.pem", "test-ca-key.pem", "active-macos.json"):
            (self.runtime / name).unlink(missing_ok=True)
