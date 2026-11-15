"""音频转码：把小度不支持的格式（WAV/FLAC/OGG/OPUS/WMA）转成 MP3。

小度 AudioPlayer 只认 AUDIO_MP3 / AUDIO_M4A / AUDIO_M3U8。
FLAC、WAV 等必须转码，否则设备会 PlaybackFailed。

策略：
  * 惰性转码：第一次被请求时才转，转到缓存目录后复用；
  * 转码结果按 (路径, mtime, 大小) 做指纹，源文件变了自动失效；
  * 找不到 ffmpeg 时降级：needs_transcode 仍返回 True，url_for 抛错，
    由上层给出明确提示，而不是静默播放失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading

from dueros_protocol import stream_format_for

_PASSTHROUGH = (".mp3", ".m4a", ".aac", ".m3u8")


class Transcoder:
    def __init__(self, cache_dir: str, library, public_base_getter, ffmpeg: str | None = None):
        self.cache_dir = cache_dir
        self.library = library
        self.base_url = public_base_getter
        self.ffmpeg = ffmpeg or self._find_ffmpeg()
        self.lock = threading.Lock()
        self._index_path = os.path.join(cache_dir, "_index.json")
        self._index: dict[str, dict] = self._load_index()
        os.makedirs(cache_dir, exist_ok=True)

    # ---------- 环境 ----------
    @staticmethod
    def _find_ffmpeg() -> str | None:
        found = shutil.which("ffmpeg")
        if found:
            return found
        # 项目内自带的 ffmpeg（vendor/ffmpeg/bin/ffmpeg.exe）
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (
            os.path.join(here, "..", "vendor", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(here, "..", "tools", "ffmpeg", "bin", "ffmpeg.exe"),
        ):
            p = os.path.abspath(cand)
            if os.path.isfile(p):
                return p
        return None

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    # ---------- 索引 ----------
    def _load_index(self) -> dict:
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_index(self) -> None:
        try:
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f)
        except OSError:
            pass

    # ---------- 判定 ----------
    def needs_transcode(self, track_id: str) -> bool:
        track = self.library.get(track_id)
        if not track:
            return False
        return stream_format_for(track.path) is None

    def _key(self, track) -> str:
        try:
            st = os.stat(track.path)
            sig = f"{track.path}|{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            sig = track.path
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]

    def _out_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, key + ".mp3")

    # ---------- 转码 ----------
    def ensure(self, track_id: str) -> str:
        """确保存在 MP3，返回本地文件路径。"""
        track = self.library.get(track_id)
        if not track:
            raise RuntimeError(f"曲目不存在: {track_id}")
        key = self._key(track)
        out = self._out_path(key)

        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out

        if not self.ffmpeg:
            raise RuntimeError(
                "需要转码但未找到 ffmpeg。请安装 ffmpeg 后重试，"
                "或把该曲目换成 mp3/m4a 格式。"
            )

        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = out + ".part"
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", track.path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "2",
            "-ar", "44100",
            "-ac", "2",
            # .part 后缀会让 ffmpeg 无法推断容器，必须显式指定
            "-f", "mp3",
            tmp,
        ]
        with self.lock:
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                return out
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0 or not os.path.isfile(tmp):
                err = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
                if os.path.isfile(tmp):
                    os.remove(tmp)
                raise RuntimeError(f"转码失败: {err or 'ffmpeg 返回码 ' + str(proc.returncode)}")
            os.replace(tmp, out)
            self._index[key] = {"src": track.path, "out": out}
            self._save_index()
        return out

    def url_for(self, track_id: str) -> str:
        self.ensure(track_id)
        track = self.library.get(track_id)
        key = self._key(track)
        base = self.base_url().rstrip("/")
        return f"{base}/stream/t/{key}"

    def resolve_generated(self, key: str) -> str | None:
        p = self._out_path(key)
        return p if os.path.isfile(p) else None

    def stats(self) -> dict:
        files = [f for f in os.listdir(self.cache_dir) if f.endswith(".mp3")] if os.path.isdir(self.cache_dir) else []
        size = sum(os.path.getsize(os.path.join(self.cache_dir, f)) for f in files)
        return {
            "ffmpeg": self.ffmpeg,
            "available": self.available,
            "cached_files": len(files),
            "cached_bytes": size,
        }
