"""小度 × MusicFree 技能服务（零第三方依赖）。

三重角色：
  1. DuerOS 自定义技能 Web Service —— 接收云端请求，返回 AudioPlayer 指令
  2. 公网音频流服务 —— 支持 HTTP Range，供小度边下边播
  3. MusicFree 插件的数据后端

启动：
    python bridge/server.py --config bridge/config.json [--watch-tunnel]

路由：
    POST /dueros              DuerOS 技能入口（需公网 HTTPS）
    GET  /stream/<id>         原始音频流（Range）
    GET  /stream/t/<key>      转码后的 MP3 流（Range）
    GET  /api/search          搜索（MusicFree 插件也用）
    GET  /api/lyric/<id>      歌词
    GET  /api/health          健康检查
    GET  /api/stats           曲库、队列、转码缓存
    GET  /api/now             当前播放状态
    POST /api/set-base        手动设置公网基址
    POST /api/volume          音量
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dueros_protocol as P  # noqa: E402
from dueros_skill import Skill  # noqa: E402
from library import Library  # noqa: E402
from transcoder import Transcoder  # noqa: E402

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "public_base": "",
    "token": "",
    "music_dirs": ["./music"],
    "extensions": [".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus"],
    "transcode_dir": "./cache",
    "state_file": "./state.json",
    "ffmpeg": "",
    "tunnel_logs": ["../logs/cloudflared.err"],
}

START_TIME = time.time()


def load_config(path: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if v is not None})

    def abspath(p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    cfg["music_dirs"] = [abspath(d) for d in cfg["music_dirs"]]
    cfg["transcode_dir"] = abspath(cfg["transcode_dir"])
    cfg["state_file"] = abspath(cfg["state_file"])
    cfg["tunnel_logs"] = [abspath(p) for p in cfg.get("tunnel_logs") or []]
    plugin = cfg.get("plugin") or {}
    if plugin.get("path"):
        plugin["path"] = abspath(plugin["path"])
    return cfg


class App:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.library = Library(cfg["music_dirs"], cfg.get("extensions") or DEFAULT_CONFIG["extensions"]).scan()
        self.transcoder = Transcoder(
            cfg["transcode_dir"],
            self.library,
            self.public_base,
            ffmpeg=cfg.get("ffmpeg") or None,
        )

        # MusicFree 插件音源（可选）
        self.plugin = None
        self.media = None
        plugin_cfg = cfg.get("plugin") or {}
        if plugin_cfg.get("path") and plugin_cfg.get("enabled", True):
            try:
                from plugin_source import MusicFreePlugin

                self.plugin = MusicFreePlugin(
                    plugin_cfg["path"],
                    user_variables=plugin_cfg.get("userVariables") or {},
                    timeout=float(plugin_cfg.get("timeout") or 12.0),
                )
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[skill] 插件初始化失败: {e}\n")

        try:
            from media_source import MediaSource

            self.media = MediaSource(
                self.library,
                self.plugin,
                prefer_local=bool((cfg.get("source") or {}).get("preferLocal", True)),
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[skill] 音源层初始化失败: {e}\n")

        self.skill = Skill(
            self.library,
            self.public_base,
            state_path=cfg.get("state_file"),
            transcoder=self.transcoder,
            media=self.media,
        )
        self._base_override: str = ""

    def public_base(self) -> str:
        if self._base_override:
            return self._base_override.rstrip("/")
        configured = (self.cfg.get("public_base") or "").rstrip("/")
        if configured:
            return configured
        host = self.cfg.get("host", "0.0.0.0")
        if host in ("0.0.0.0", "::", ""):
            host = _guess_lan_ip()
        return f"http://{host}:{self.cfg.get('port', 8765)}"

    def set_base(self, base: str) -> None:
        self._base_override = (base or "").strip().rstrip("/")


def _guess_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoduMusicSkill/1.1"
    protocol_version = "HTTP/1.1"
    app: App

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[%s] %s %s\n" % (self.log_date_time_string(), self.command, fmt % args))

    # ---------------- 基础 ----------------
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.app.cfg.get("token") or ""
        if not token:
            return True
        return (self.headers.get("X-Bridge-Token") or (query.get("token", [""])[0])) == token

    @staticmethod
    def _route(path: str) -> str:
        try:
            path = urllib.parse.unquote(path)
        except Exception:  # noqa: BLE001
            pass
        p = posixpath.normpath(path).replace("\\", "/")
        return p if p.startswith("/") else "/" + p

    # ---------------- GET ----------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = self._route(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                # 浏览器（含小度 H5 容器）要页面；平台探测/回调要 JSON。
                accept = (self.headers.get("Accept") or "").lower()
                ua = (self.headers.get("User-Agent") or "").lower()
                wants_html = "text/html" in accept or "mozilla" in ua
                if wants_html:
                    return self._serve_web("index.html")
                return self._json(
                    {
                        "ok": True,
                        "service": "xiaodu-musicfree-bridge",
                        "skill_endpoint": "/dueros",
                        "h5_endpoint": "/h5",
                        "tracks": len(self.app.library.tracks),
                    }
                )
            if path == "/api/health":
                return self._json(
                    {
                        "ok": True,
                        "tracks": len(self.app.library.tracks),
                        "base": self.app.public_base(),
                        "uptime": int(time.time() - START_TIME),
                        "transcode": self.app.transcoder.available,
                    }
                )
            if path in ("/h5", "/h5/"):
                return self._serve_web("index.html")
            if path.startswith("/stream/t/"):
                # 兼容：本地转码缓存 和 B站代理转码缓存 都走这里
                raw = path.rsplit("/", 1)[-1]
                raw = raw[:-4] if raw.endswith(".mp3") else raw
                # 先查本地转码缓存
                local = self.app.transcoder.resolve_generated(raw)
                if local:
                    return self._serve_file(local)
                # 再查 B站代理缓存（cache/proxy/ 下，或统一后 cache/ 下）
                return self._serve_proxy_or_stream(raw)
            if path.startswith("/proxy/"):
                # 去掉可能的 .mp3 后缀（小度可能依据后缀判断格式）
                raw = path.rsplit("/", 1)[-1]
                raw = raw[:-4] if raw.endswith(".mp3") else raw
                return self._serve_proxy(raw)
            if path.startswith("/stream/"):
                track = self.app.library.get(path.rsplit("/", 1)[-1])
                if not track:
                    return self._json({"ok": False, "error": "曲目不存在"}, 404)
                return self._serve_file(track.path)
            if not self._authorized(query):
                return self._json({"ok": False, "error": "token 校验失败"}, 401)
            if path == "/api/search":
                return self._json(self._search(query))
            if path == "/api/source":
                return self._json(self._source_status())
            if path == "/api/playlist":
                return self._json(self._playlist(query))
            if path == "/api/resolve":
                return self._json(self._resolve_song(query))
            if path.startswith("/api/playurl/"):
                return self._json(self._playurl(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/lyric/"):
                track = self.app.library.get(path.rsplit("/", 1)[-1])
                if not track:
                    return self._json({"ok": False, "error": "not found"}, 404)
                return self._json({"rawLrc": self.app.library.lyric_for(track) or "", "translation": None})
            if path == "/api/stats":
                stats = self.app.library.stats()
                stats["base"] = self.app.public_base()
                stats["transcode"] = self.app.transcoder.stats()
                stats["sessions"] = self.app.skill.snapshot()
                return self._json(stats)
            if path == "/api/now":
                return self._json({"sessions": self.app.skill.snapshot()})
            return self._json({"ok": False, "error": f"未知路径 {path}"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # ---------------- POST ----------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = self._route(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            # 技能请求可能被投递到 / 或 /dueros，两种都接
            if path in ("/dueros", "/dueros/", "/"):
                body = self._read_json()
                if body.get("request") or body.get("intents"):
                    req = body.get("request") or {}
                    rtype = req.get("type") or ""
                    intents = req.get("intents") or []
                    iname = intents[0].get("name") if isinstance(intents, list) and intents else ""
                    sys.stderr.write(
                        f"[dueros] path={path} type={rtype} intent={iname} "
                        f"device={P.device_id(body)} query={P.raw_query(body)[:60]!r}\n"
                    )
                    _dump_request(body)
                    return self._json(self.app.skill.handle(body))
                # 不是技能请求（如浏览器打开），落到下面的 GET 逻辑不合适，直接返回说明
                return self._json(
                    {"ok": True, "hint": "技能入口就绪，请通过 DuerOS 技能调用", "path": path}
                )
            if not self._authorized(query):
                return self._json({"ok": False, "error": "token 校验失败"}, 401)
            body = self._read_json()
            if path == "/api/set-base":
                self.app.set_base(body.get("base") or "")
                return self._json({"ok": True, "base": self.app.public_base()})
            if path == "/api/volume":
                return self._json(self._set_volume(body))
            return self._json({"ok": False, "error": f"未知路径 {path}"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------- 业务 ----------------
    def _search(self, query: dict[str, list[str]]) -> dict:
        q = query.get("q", [""])[0]
        limit = _int(query.get("limit", ["30"])[0], 30, 1, 200)
        offset = _int(query.get("offset", ["0"])[0], 0, 0, 10**9)
        tracks = self.app.library.search(q, limit=limit, offset=offset)
        base = self.app.public_base()
        data = []
        for t in tracks:
            playable = P.stream_format_for(t.path) is not None
            data.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "duration": t.duration,
                    "url": f"{base}/stream/{t.id}",
                    "needsTranscode": not playable,
                }
            )
        return {"isEnd": len(data) < limit, "total": len(self.app.library.tracks), "data": data}

    def _playurl(self, track_id: str) -> dict:
        """返回浏览器/设备可直接播放的地址；需要时惰性转码。"""
        track = self.app.library.get(track_id)
        if not track:
            return {"ok": False, "error": "曲目不存在"}
        base = self.app.public_base().rstrip("/")
        try:
            if self.app.transcoder.needs_transcode(track_id):
                self.app.transcoder.ensure(track_id)
                key = self.app.transcoder._key(track)
                return {"ok": True, "url": f"{base}/stream/t/{key}", "transcoded": True}
            return {"ok": True, "url": f"{base}/stream/{track_id}", "transcoded": False}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    def _serve_web(self, filename: str) -> None:
        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
        full = os.path.normpath(os.path.join(root, filename))
        if not full.startswith(root) or not os.path.isfile(full):
            return self._json({"ok": False, "error": "页面不存在"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _source_status(self) -> dict:
        """音源层状态：本地曲库 + 插件。"""
        out: dict[str, Any] = {
            "local": {
                "dirs": self.app.library.dirs,
                "tracks": len(self.app.library.tracks),
                "errors": self.app.library.errors,
            }
        }
        media = self.app.media
        if media:
            out["media"] = media.stats()
        plugin = self.app.plugin
        if plugin:
            out["plugin"] = plugin.describe()
        else:
            out["plugin"] = {"available": False, "reason": "未配置插件"}
        return out

    def _playlist(self, query: dict[str, list[str]]) -> dict:
        """搜歌返回播放队列元数据（不含 URL，懒加载）。

        参数：
          q=关键词   —— 搜歌（第一首精确 + 同歌手扩展）
          mode=newsongs —— 网易云新歌榜
          limit=数量（默认20）
        返回：{ok, count, data: [{song_id, title, artist, duration_ms, source}]}
        """
        q = (query.get("q", [""])[0]).strip()
        mode = (query.get("mode", [""])[0]).strip()
        limit = _int(query.get("limit", ["20"])[0], 20, 1, 50)

        media = self.app.media
        if not media:
            return {"ok": False, "error": "音源未就绪"}

        if mode == "newsongs":
            songs = media.new_songs(limit=limit)
        else:
            if not q:
                return {"ok": False, "error": "缺少关键词"}
            songs = media.search_with_related(q, limit=limit)

        data = []
        for s in songs:
            data.append({
                "song_id": s.id,
                "title": s.title,
                "artist": s.artist,
                "duration_ms": (s.duration or 0) * 1000,
                "source": s.source,
                "track_id": s.track_id,
                # 序列化 plugin_item，供 resolve 用
                "plugin_item": s.plugin_item,
            })
        return {"ok": True, "count": len(data), "data": data}

    def _resolve_song(self, query: dict[str, list[str]]) -> dict:
        """单首取流（懒加载），返回可播放 URL。

        参数：song_id, source, track_id, plugin_item(JSON字符串)
        """
        from media_source import Song

        song_id = query.get("song_id", [""])[0]
        source = query.get("source", [""])[0]
        track_id = query.get("track_id", [""])[0]
        plugin_item_raw = query.get("plugin_item", [""])[0]

        media = self.app.media
        if not media:
            return {"ok": False, "error": "音源未就绪"}

        # 重建 Song 对象
        plugin_item = {}
        if plugin_item_raw:
            try:
                plugin_item = json.loads(plugin_item_raw)
            except ValueError:
                pass

        song = Song(
            id=song_id,
            title=query.get("title", [""])[0],
            artist=query.get("artist", [""])[0],
            source=source,
            track_id=track_id,
            plugin_item=plugin_item,
        )

        try:
            resolved = media.resolve(song)
            base = self.app.public_base().rstrip("/")
            if source == "local":
                if self.app.transcoder.needs_transcode(track_id):
                    track = self.app.library.get(track_id)
                    key = self.app.transcoder._key(track) if track else track_id
                    self.app.transcoder.ensure(track_id)
                    url = f"{base}/stream/t/{key}"
                else:
                    url = f"{base}/stream/{track_id}"
            else:
                key = media.proxy_key(song)
                url = f"{base}/stream/t/{key}" if key else resolved.get("url", "")
            if not url:
                return {"ok": False, "error": "无播放地址"}
            return {"ok": True, "url": url, "duration_ms": (query.get("duration_ms", ["0"])[0])}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _set_volume(self, body: dict) -> dict:
        did = (body.get("deviceId") or "").strip()
        level = body.get("volume")
        with self.app.skill.lock:
            if did:
                sess = self.app.skill._session(did)
                sess.volume = level
                self.app.skill.save()
        return {"ok": True, "deviceId": did, "volume": level,
                "note": "记录音量意图；实际音量由设备端执行"}

    def _serve_proxy_or_stream(self, key: str) -> None:
        """B站等插件音源，统一走 /stream/t/ 路径（与本地转码一致）。"""
        self._serve_proxy(key)

    def _serve_proxy(self, key: str) -> None:
        """代理外部音源并转码为 MP3（缓存到本地文件后输出）。

        小度 AudioPlayer 需要 Content-Length 才能正常播放；
        流式转码（无长度）会导致设备 fallback 到内置播放器。
        因此这里先完整转码到缓存文件，再输出文件（带 Content-Length + Range）。
        """
        if not self.app.media:
            return self._json({"ok": False, "error": "代理不可用"}, 503)
        info = self.app.media.proxy_resolve(key)
        if not info:
            return self._json({"ok": False, "error": "代理链接不存在或已过期"}, 404)

        target = info.get("url") or ""
        headers = info.get("headers") or {}
        if not target:
            return self._json({"ok": False, "error": "目标 URL 为空"}, 500)

        ffmpeg = self.app.cfg.get("ffmpeg") or self.app.transcoder.ffmpeg
        if not ffmpeg:
            self._serve_proxy_direct(key, target, headers)
            return

        # 缓存目录：和本地转码一致的 cache/ 目录（key.mp3）
        cache_dir = self.app.cfg.get("transcode_dir") or "cache"
        os.makedirs(cache_dir, exist_ok=True)
        out_path = os.path.join(cache_dir, key + ".mp3")

        # 若缓存已存在且非空，直接输出
        if not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
            tmp_path = out_path + ".part"
            try:
                cmd = [
                    ffmpeg,
                    "-hide_banner", "-loglevel", "error",
                    "-y",
                    "-tls_verify", "0",
                    "-headers", self._headers_str(headers),
                    "-i", target,
                    "-vn",
                    "-codec:a", "libmp3lame",
                    "-q:a", "2",
                    "-ar", "44100",
                    "-ac", "2",
                    "-f", "mp3",
                    tmp_path,
                ]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode != 0 or not os.path.isfile(tmp_path):
                    err = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
                    self._json({"ok": False, "error": f"转码失败: {err or '退出码 ' + str(proc.returncode)}"}, 500)
                    return
                os.replace(tmp_path, out_path)
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "error": "转码超时"}, 504)
                return
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"转码异常: {e}"}, 500)
                return

        # 输出缓存文件（带 Content-Length + Range）
        self._serve_file(out_path)

    def _headers_str(self, headers: dict) -> str:
        parts = []
        for k, v in headers.items():
            if k.lower() in ("host", "content-length", "connection"):
                continue
            parts.append(f"{k}: {v}")
        return "\r\n".join(parts)

    def _serve_proxy_direct(self, key: str, target: str, headers: dict) -> None:
        """无转码的直转兜底。"""
        try:
            req = urllib.request.Request(target)
            for k, v in headers.items():
                if k.lower() in ("host", "content-length", "connection"):
                    continue
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as upstream:
                ctype = upstream.headers.get("Content-Type") or "audio/mpeg"
                clen = upstream.headers.get("Content-Length")
                self.send_response(upstream.status)
                self.send_header("Content-Type", ctype)
                if clen:
                    self.send_header("Content-Length", clen)
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except urllib.error.HTTPError as e:
            self._json({"ok": False, "error": f"上游 {e.code}"}, 502)
        except urllib.error.URLError as e:
            self._json({"ok": False, "error": f"上游不可达: {e.reason}"}, 502)

    def _serve_file(self, path: str | None) -> None:
        if not path or not os.path.isfile(path):
            return self._json({"ok": False, "error": "文件不存在"}, 404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "audio/mpeg"
        start, end = 0, size - 1
        status = HTTPStatus.OK
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _dump_request(body: dict) -> None:
    """把 DuerOS 请求体追加到日志文件，用于排查。"""
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "requests.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("=== " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===\n")
            f.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n\n")
    except OSError:
        pass


def _int(value: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def run(cfg: dict[str, Any], watch_tunnel: bool = False) -> None:
    app = App(cfg)
    Handler.app = app
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True

    print(f"[skill] 曲库 {len(app.library.tracks)} 首")
    print(f"[skill] ffmpeg {app.transcoder.ffmpeg or '未找到'}")
    print(f"[skill] 监听 http://{cfg['host']}:{cfg['port']}")
    print(f"[skill] 公网基址 {app.public_base()}")
    for e in app.library.errors:
        print(f"[skill] 警告: {e}")

    if watch_tunnel:
        from tunnel_watch import AutoBase

        def _on_change(url: str) -> None:
            app.set_base(url)
            print(f"[skill] 隧道地址已更新: {url}")

        AutoBase(cfg.get("tunnel_logs") or [], _on_change).start()
        print("[skill] 已开启隧道地址自动跟踪")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[skill] 退出")
    finally:
        app.skill.save()
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="小度 × MusicFree DuerOS 技能服务")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--watch-tunnel", action="store_true", help="自动跟踪 cloudflared 隧道地址")
    args = ap.parse_args()
    run(load_config(args.config), watch_tunnel=args.watch_tunnel)


if __name__ == "__main__":
    main()
