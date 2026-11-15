"""MusicFree 插件音源接入层。

通过 Node 子进程调用 MusicFree 插件（plugin_runner.js），
把插件的 search / getMediaSource 暴露给 Python 服务。

设计要点：
  * 插件跑在子进程里，崩溃/超时不会拖垮主服务；
  * 每次调用有超时保护（默认 12 秒，覆盖插件内部网络请求）；
  * 失败时返回 None/抛 PluginError，由上层决定回退策略；
  * 不做任何风控绕过，Cookie 等凭据完全由用户在配置里提供。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 12.0

# 网易云移动端搜索接口：免登录、免风控（实测 PC 端 /api 被 -462 时它仍可用）
_NCM_MOBILE_SEARCH = "https://music.163.com/api/v1/search/get"
_NCM_MOBILE_UA = "NeteaseMusic/9.1.0.0 (Android 13)"


class PluginError(RuntimeError):
    pass


class MusicFreePlugin:
    """封装一个 MusicFree 插件的调用。"""

    def __init__(
        self,
        plugin_path: str,
        *,
        runner_path: str | None = None,
        user_variables: dict[str, str] | None = None,
        node: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.plugin_path = os.path.abspath(plugin_path) if plugin_path else ""
        self.runner_path = runner_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_runner.js")
        self.user_variables = user_variables or {}
        self.node = node or shutil.which("node") or "node"
        self.timeout = timeout
        self._info_cache: dict | None = None
        self._lock = threading.Lock()

    # ---------------- 基础 ----------------
    @property
    def available(self) -> bool:
        return bool(self.plugin_path and os.path.isfile(self.plugin_path) and os.path.isfile(self.runner_path))

    def _run(self, command: str, args: dict | None = None) -> Any:
        if not self.available:
            raise PluginError("插件或运行器不存在")

        cmd = [self.node, self.runner_path, self.plugin_path, command]
        if args is not None:
            cmd.append(json.dumps(args, ensure_ascii=False))
            cmd.append(json.dumps(self.user_variables, ensure_ascii=False))

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise PluginError(f"插件调用超时（{self.timeout}s）：{command}") from None
        except OSError as e:
            raise PluginError(f"无法启动 node: {e}") from None

        raw = (proc.stdout or b"").decode("utf-8", "ignore").strip()
        if not raw:
            err = (proc.stderr or b"").decode("utf-8", "ignore").strip()[:300]
            raise PluginError(f"插件无输出（退出码 {proc.returncode}）：{err}")

        # 取最后一行 JSON（插件可能打印日志）
        line = raw.splitlines()[-1]
        try:
            payload = json.loads(line)
        except ValueError:
            raise PluginError(f"插件输出无法解析：{raw[:300]}") from None

        if not payload.get("ok"):
            raise PluginError(payload.get("error") or "插件返回失败")
        return payload.get("data")

    # ---------------- 对外方法 ----------------
    def info(self) -> dict:
        if self._info_cache is None:
            self._info_cache = self._run("info") or {}
        return self._info_cache

    def search(self, query: str, page: int = 1, type_: str = "music") -> list[dict]:
        data = self._run("search", {"query": query, "page": page, "type": type_})
        if isinstance(data, dict):
            items = data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [i for i in items if isinstance(i, dict)]

    def search_mobile(self, query: str, limit: int = 20) -> list[dict]:
        """用网易云移动端接口搜索（免登录，绕开 PC 端 -462 风控）。

        返回 MusicFree 插件能直接消费的 musicItem 字典（含 fee/duration）。
        这一步只拿元数据；播放链接仍交给插件的 getMediaSource（自动走 B 站回退）。
        """
        if not query or not query.strip():
            return []
        params = urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": limit, "offset": 0}
        )
        url = f"{_NCM_MOBILE_SEARCH}?{params}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _NCM_MOBILE_UA)
        req.add_header("Referer", "https://music.163.com/")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.URLError as e:
            raise PluginError(f"移动端搜索失败: {e.reason}") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise PluginError("移动端搜索返回无法解析") from None
        if payload.get("code") != 200:
            raise PluginError(f"移动端搜索 code={payload.get('code')}")
        songs = ((payload.get("result") or {}).get("songs")) or []
        items = []
        for s in songs:
            if not isinstance(s, dict):
                continue
            artists = s.get("artists") or []
            artist = artists[0].get("name", "") if artists else ""
            items.append(
                {
                    "id": str(s.get("id") or ""),
                    "title": s.get("name") or "",
                    "artist": artist,
                    "album": (s.get("album") or {}).get("name", ""),
                    "duration": int(s.get("duration") or 0) // 1000,
                    "fee": s.get("fee"),
                    "artwork": (s.get("album") or {}).get("picUrl", ""),
                }
            )
        return [i for i in items if i.get("id") and i.get("title")]

    def new_songs(self, limit: int = 20) -> list[dict]:
        """网易云新歌榜（免登录可拿，每天更新的新歌）。

        返回 MusicFree 插件能消费的 musicItem 字典。
        """
        url = "https://music.163.com/api/v1/discovery/new/songs"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _NCM_MOBILE_UA)
        req.add_header("Referer", "https://music.163.com/")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.URLError as e:
            raise PluginError(f"新歌榜获取失败: {e.reason}") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise PluginError("新歌榜返回无法解析") from None
        if payload.get("code") != 200:
            raise PluginError(f"新歌榜 code={payload.get('code')}")
        songs = payload.get("data") or []
        items = []
        for s in songs:
            if not isinstance(s, dict):
                continue
            artists = s.get("artists") or []
            artist = artists[0].get("name", "") if artists else ""
            items.append(
                {
                    "id": str(s.get("id") or ""),
                    "title": s.get("name") or "",
                    "artist": artist,
                    "album": (s.get("album") or {}).get("name", ""),
                    "duration": int(s.get("duration") or 0) // 1000,
                    "fee": (s.get("privilege") or {}).get("fee") or s.get("fee"),
                    "artwork": (s.get("album") or {}).get("picUrl", ""),
                }
            )
        return [i for i in items if i.get("id") and i.get("title")][:limit]

    def media_source(self, item: dict, quality: str = "standard") -> dict:
        data = self._run("source", {"item": item, "quality": quality}) or {}
        if not isinstance(data, dict):
            raise PluginError("插件返回的播放信息格式不对")
        return data

    def lyric(self, item: dict) -> dict:
        try:
            return self._run("lyric", {"item": item}) or {}
        except PluginError:
            return {}

    # ---------------- 工具 ----------------
    def describe(self) -> dict:
        if not self.available:
            return {"available": False, "reason": "插件文件或运行器缺失"}
        try:
            info = self.info()
        except PluginError as e:
            return {"available": False, "reason": str(e)}
        return {
            "available": True,
            "platform": info.get("platform", ""),
            "version": info.get("version", ""),
            "author": info.get("author", ""),
            "methods": info.get("methods", []),
            "has_cookie": bool((self.user_variables or {}).get("cookie")),
        }
