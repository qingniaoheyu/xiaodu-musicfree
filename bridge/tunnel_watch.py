"""Cloudflare Tunnel 辅助：自动发现隧道地址，免去手工改配置。

它做三件事：
  1. 从 cloudflared 的日志里抓 https://xxx.trycloudflare.com
  2. 抓不到就托盘轮询 cloudflared 的 metrics 接口
  3. 拿到后回调通知服务端（服务端据此更新 public_base）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


def scan_log(path: str) -> str | None:
    """从 cloudflared 输出文件里找隧道地址，取最后一个。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            found = URL_RE.findall(f.read())
        return found[-1] if found else None
    except OSError:
        return None


def scan_metrics(port: int = 20241) -> str | None:
    """cloudflared 的 metrics 端点有时能拿到 hostname。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/quicktunnel", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        host = (data.get("hostname") or "").strip()
        if host:
            return host if host.startswith("http") else f"https://{host}"
    except Exception:  # noqa: BLE001
        pass
    return None


def discover(*, log_paths: list[str] | None = None, metrics_port: int = 20241) -> str | None:
    for p in log_paths or []:
        u = scan_log(p)
        if u:
            return u
    return scan_metrics(metrics_port)


class AutoBase:
    """后台线程持续跟踪隧道地址，变了就通知。"""

    def __init__(self, log_paths: list[str], on_change, *, interval: float = 15.0):
        self.log_paths = log_paths
        self.on_change = on_change
        self.interval = interval
        self.current: str | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> "AutoBase":
        self._t = threading.Thread(target=self._run, name="tunnel-watch", daemon=True)
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            u = discover(log_paths=self.log_paths)
            if u and u != self.current:
                self.current = u
                try:
                    self.on_change(u)
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.interval)
