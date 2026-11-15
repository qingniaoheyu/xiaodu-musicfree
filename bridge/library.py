"""本地曲库：扫描目录、按文件名推断元数据、提供检索。

零第三方依赖。文件名约定：`歌手 - 歌名.mp3`，所在目录名作为专辑。
若目录下存在同名的 .lrc 文件，会作为歌词一并返回。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

DEFAULT_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma")

# 文件名里常见的分隔符：全角/半角连字符、减号
_SPLIT_RE = re.compile(r"\s*[-–—]\s*")
_TRACK_NO_RE = re.compile(r"^\s*\d{1,3}[.、_\-\s]+")


@dataclass
class Track:
    id: str
    path: str
    title: str
    artist: str = "未知歌手"
    album: str = "未知专辑"
    size: int = 0
    ext: str = ".mp3"
    duration: int | None = None

    def to_media_item(self, base_url: str, token: str = "") -> dict:
        """转成 MusicFree 的 IMusicItem 结构。"""
        item = {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork": None,
            "duration": self.duration,
            "url": self.stream_url(base_url, token),
        }
        return item

    def stream_url(self, base_url: str, token: str = "") -> str:
        q = f"?token={token}" if token else ""
        return f"{base_url.rstrip('/')}/stream/{self.id}{q}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _parse_name(stem: str) -> tuple[str, str]:
    """把 `歌手 - 歌名` 拆成 (artist, title)；解析不出来时整段当标题。"""
    stem = _TRACK_NO_RE.sub("", stem).strip()
    parts = _SPLIT_RE.split(stem, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[1].strip(), parts[0].strip()
    return stem, "未知歌手"


class Library:
    def __init__(self, dirs: Iterable[str], extensions: Iterable[str] = DEFAULT_EXTENSIONS):
        self.dirs = [os.path.abspath(d) for d in dirs if d]
        self.extensions = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in extensions)
        self.tracks: dict[str, Track] = {}
        self.by_path: dict[str, Track] = {}
        self.errors: list[str] = []

    # ---------- 扫描 ----------

    def scan(self) -> "Library":
        self.tracks.clear()
        self.by_path.clear()
        self.errors.clear()
        for root_dir in self.dirs:
            if not os.path.isdir(root_dir):
                self.errors.append(f"目录不存在: {root_dir}")
                continue
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in self.extensions:
                        continue
                    full = os.path.join(dirpath, fn)
                    self._add(full, root_dir)
        return self

    def _add(self, full_path: str, root_dir: str) -> None:
        try:
            rel = os.path.relpath(full_path, root_dir)
        except ValueError:
            rel = full_path
        digest = hashlib.sha1(rel.replace("\\", "/").encode("utf-8")).hexdigest()[:16]
        title, artist = _parse_name(os.path.splitext(os.path.basename(full_path))[0])
        album = os.path.basename(os.path.dirname(full_path)) or "未知专辑"
        try:
            size = os.path.getsize(full_path)
        except OSError:
            size = 0
        track = Track(
            id=digest,
            path=full_path,
            title=title,
            artist=artist,
            album=album,
            size=size,
            ext=os.path.splitext(full_path)[1].lower(),
        )
        self.tracks[track.id] = track
        self.by_path[os.path.normcase(full_path)] = track

    # ---------- 查询 ----------

    def get(self, track_id: str) -> Track | None:
        return self.tracks.get(track_id)

    def search(self, query: str, limit: int = 30, offset: int = 0) -> list[Track]:
        q = _norm(query)
        if not q:
            return self._slice(list(self.tracks.values()), offset, limit)
        scored: list[tuple[int, Track]] = []
        for t in self.tracks.values():
            hay = _norm(f"{t.title} {t.artist} {t.album}")
            if q not in hay:
                continue
            if _norm(t.title).startswith(q):
                score = 0
            elif q in _norm(t.title):
                score = 1
            elif q in _norm(t.artist):
                score = 2
            else:
                score = 3
            scored.append((score, t))
        scored.sort(key=lambda s: (s[0], _norm(s[1].title)))
        return self._slice([t for _, t in scored], offset, limit)

    @staticmethod
    def _slice(items: list[Track], offset: int, limit: int) -> list[Track]:
        offset = max(0, offset)
        return items[offset : offset + max(0, limit)]

    def lyric_for(self, track: Track) -> str | None:
        lrc = os.path.splitext(track.path)[0] + ".lrc"
        if os.path.isfile(lrc):
            try:
                with open(lrc, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except OSError:
                return None
        return None

    def stats(self) -> dict:
        return {
            "dirs": self.dirs,
            "extensions": list(self.extensions),
            "track_count": len(self.tracks),
            "errors": self.errors,
        }
