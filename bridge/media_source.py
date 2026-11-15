"""统一音源层：本地曲库优先，其次 MusicFree 插件。

对外只暴露两个动作，语音链路和 HTTP API 都走这里：
  search(keyword, limit)  -> [Song]
  resolve(song)           -> {"url":..., "headers":..., "title":..., "artist":...}

Song 是一个轻量结构，带 source 标记，方便后续取流时知道该走哪条路。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from plugin_source import MusicFreePlugin, PluginError


@dataclass
class Song:
    id: str
    title: str
    artist: str = ""
    album: str = ""
    source: str = "local"                 # local | plugin
    duration: int | None = None
    artwork: str = ""
    track_id: str = ""                    # 本地曲库的 track id
    plugin_item: dict = field(default_factory=dict)  # 插件原始条目
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "source": self.source,
            "duration": self.duration,
            "artwork": self.artwork,
        }


class SourceError(RuntimeError):
    pass


class MediaSource:
    """把本地曲库和插件统一起来。"""

    def __init__(self, library, plugin: MusicFreePlugin | None = None, *, prefer_local: bool = True):
        self.library = library
        self.plugin = plugin
        self.prefer_local = prefer_local
        self._cache: dict[str, tuple[float, dict]] = {}
        self._proxy: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._cache_ttl = 600  # 播放链接缓存 10 分钟

    # ---------------- 搜索 ----------------
    def search(self, keyword: str, limit: int = 10) -> list[Song]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        local_hits: list[Song] = []
        if self.library:
            for t in self.library.search(keyword, limit=limit):
                local_hits.append(
                    Song(
                        id=f"local:{t.id}",
                        title=t.title,
                        artist=t.artist,
                        album=t.album,
                        source="local",
                        track_id=t.id,
                    )
                )

        plugin_hits: list[Song] = []
        if self.plugin and self.plugin.available:
            try:
                # 优先移动端接口（免登录、免 PC 端风控）
                items = self.plugin.search_mobile(keyword, limit=limit)
                if not items:
                    # 回退到插件自带搜索（可能被风控）
                    items = self.plugin.search(keyword, 1)[:limit]
            except PluginError:
                try:
                    items = self.plugin.search(keyword, 1)[:limit]
                except PluginError:
                    items = []
            for it in items:
                pid = str(it.get("id") or "")
                if not pid:
                    continue
                plugin_hits.append(
                    Song(
                        id=f"plugin:{pid}",
                        title=it.get("title") or "未知曲目",
                        artist=it.get("artist") or "",
                        album=it.get("album") or "",
                        source="plugin",
                        duration=_as_int(it.get("duration")),
                        artwork=it.get("artwork") or "",
                        plugin_item=it,
                    )
                )

        if self.prefer_local:
            merged = local_hits + [s for s in plugin_hits if s.title not in {x.title for x in local_hits}]
        else:
            merged = plugin_hits + [s for s in local_hits if s.title not in {x.title for x in plugin_hits}]
        return merged[:limit]

    def search_with_related(self, keyword: str, limit: int = 20) -> list[Song]:
        """搜歌 + 同歌手扩展，返回播放队列。

        第一首是精确匹配；后续是同歌手的其他歌（去重），实现"相关推荐"连播。
        这样搜"甲乙丙丁"不会全是一堆翻唱，而是"李佳薇版 + 李佳薇其他歌"。
        """
        hits = self.search(keyword, limit=5)
        if not hits:
            return []
        queue = [hits[0]]
        seen = {hits[0].id}
        # 用第一首的歌手名再搜，作为后续队列
        artist = (hits[0].artist or "").strip()
        if artist:
            try:
                more = self.search(artist, limit=limit)
                for m in more:
                    if m.id not in seen and m.title != hits[0].title:
                        seen.add(m.id)
                        queue.append(m)
            except Exception:  # noqa: BLE001
                pass
        # 如果同歌手扩展后队列还是太少（<3首），补上原搜索结果里剩下的
        if len(queue) < 3:
            for h in hits[1:]:
                if h.id not in seen:
                    seen.add(h.id)
                    queue.append(h)
        return queue

    # ---------------- 取流 ----------------
    def resolve(self, song: Song) -> dict:
        if song.source == "local":
            return self._resolve_local(song)
        return self._resolve_plugin(song)

    def _resolve_local(self, song: Song) -> dict:
        track = self.library.get(song.track_id) if self.library else None
        if not track:
            raise SourceError("本地曲目不存在")
        return {
            "kind": "local",
            "track_id": song.track_id,
            "title": track.title,
            "artist": track.artist,
        }

    def _resolve_plugin(self, song: Song) -> dict:
        if not self.plugin or not self.plugin.available:
            raise SourceError("插件音源不可用")
        key = song.id
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._cache_ttl:
                return hit[1]

        try:
            src = self.plugin.media_source(song.plugin_item, "standard")
        except PluginError as e:
            raise SourceError(str(e)) from None

        url = src.get("url") or ""
        if not url:
            raise SourceError("插件未返回播放地址")
        result = {
            "kind": "plugin",
            "url": url,
            "headers": src.get("headers") or {},
            "title": song.title,
            "artist": song.artist,
        }
        with self._lock:
            self._cache[key] = (now, result)
        return result

    # ---------------- 代理（解决小度无法带 Referer 的问题）----------------
    def proxy_key(self, song: Song) -> str | None:
        """为需要代理的插件音源生成一个【稳定】key。

        key 基于 song.id（歌曲ID），而不是 B站 URL —— 因为 B站 URL 带
        deadline 时间戳、每次都变，若按 URL 取 hash 会导致同一首歌每次
        都换新 key、每次都重新转码，小度会因等待超时而放弃播放。
        用稳定 key 后，同一首歌只转码一次，后续秒开。
        """
        resolved = self.resolve(song)
        url = resolved.get("url") or ""
        headers = resolved.get("headers") or {}
        if not url:
            return None
        key = hashlib.sha1(song.id.encode("utf-8")).hexdigest()[:24]
        with self._lock:
            self._proxy[key] = {"url": url, "headers": headers, "ts": time.time()}
        return key

    def proxy_resolve(self, key: str) -> dict | None:
        with self._lock:
            return self._proxy.get(key)

    def proxy_warm(self, song: Song) -> None:
        """预热：预先取流并生成代理 key（触发转码缓存，供真机秒开）。

        在返回 AudioPlayer 指令前调用，让小度收到指令后立即能拉到已
        转码好的 MP3，避免现场转码导致设备等待超时。
        """
        try:
            self.proxy_key(song)
        except Exception:  # noqa: BLE001
            pass

    def new_songs(self, limit: int = 20) -> list[Song]:
        """网易云新歌榜（免登录），返回 Song 列表。"""
        if not self.plugin or not self.plugin.available:
            return []
        try:
            items = self.plugin.new_songs(limit=limit)
        except PluginError:
            return []
        songs = []
        for it in items:
            pid = str(it.get("id") or "")
            if not pid:
                continue
            songs.append(
                Song(
                    id=f"plugin:{pid}",
                    title=it.get("title") or "未知曲目",
                    artist=it.get("artist") or "",
                    album=it.get("album") or "",
                    source="plugin",
                    duration=_as_int(it.get("duration")),
                    artwork=it.get("artwork") or "",
                    plugin_item=it,
                )
            )
        return songs

    def lyric(self, song: Song) -> str:
        try:
            if song.source == "local":
                track = self.library.get(song.track_id)
                return self.library.lyric_for(track) or "" if track else ""
            if self.plugin and self.plugin.available:
                data = self.plugin.lyric(song.plugin_item)
                return data.get("rawLrc") or ""
        except Exception:  # noqa: BLE001
            return ""
        return ""

    # ---------------- 状态 ----------------
    def stats(self) -> dict:
        out: dict[str, Any] = {
            "prefer_local": self.prefer_local,
            "local_tracks": len(self.library.tracks) if self.library else 0,
        }
        if self.plugin:
            out["plugin"] = self.plugin.describe()
        return out


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
