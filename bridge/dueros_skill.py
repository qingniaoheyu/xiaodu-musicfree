"""DuerOS 技能业务逻辑：把「放首歌」翻译成 AudioPlayer 指令。

所有请求/响应结构均按官方 SDK 源码核对，见 dueros_protocol.py 顶部注释。

要点：
  * 意图名用官方 ai.dueros.common.*（stop/next/pause/continue/previous）；
  * 播放队列按 deviceId 存内存，并可选持久化到磁盘；
  * PlaybackNearlyFinished 时 ENQUEUE 下一首实现连续播放；
  * 非 MP3/M4A 的文件需要转码，由 transcoder 决定实际流地址与格式。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

import dueros_protocol as P

MAX_QUEUE = 50
SESSION_TTL = 6 * 3600  # 6 小时后清理空闲会话


class Session:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.queue: list[dict] = []
        self.index = 0
        self.tokens: list[str] = []
        self.updated = time.time()
        self.volume = None

    def to_dict(self) -> dict:
        # 剔除不可序列化的 _song（Song 对象），只保留可持久化字段
        queue = []
        for t in self.queue:
            t2 = {k: v for k, v in t.items() if k != "_song"}
            queue.append(t2)
        return {
            "device_id": self.device_id,
            "queue": queue,
            "index": self.index,
            "tokens": self.tokens,
            "volume": self.volume,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls(d.get("device_id") or "default")
        s.queue = d.get("queue") or []
        s.index = int(d.get("index") or 0)
        s.tokens = d.get("tokens") or []
        s.volume = d.get("volume")
        s.updated = float(d.get("updated") or time.time())
        return s

    def current(self) -> dict | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def advance(self) -> dict | None:
        if self.index + 1 < len(self.queue):
            self.index += 1
            return self.queue[self.index]
        return None


class Skill:
    def __init__(self, library, base_url_getter, *, state_path: str | None = None,
                 transcoder=None, media=None):
        self.library = library
        self.base_url = base_url_getter
        self.state_path = state_path
        self.transcoder = transcoder
        self.media = media
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        if not self.state_path or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for did, d in (raw.get("sessions") or {}).items():
                self.sessions[did] = Session.from_dict(d)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if not self.state_path:
            return
        with self.lock:
            self._gc()
            data = {"sessions": {k: v.to_dict() for k, v in self.sessions.items()}}
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _gc(self) -> None:
        now = time.time()
        dead = [k for k, v in self.sessions.items() if now - v.updated > SESSION_TTL]
        for k in dead:
            self.sessions.pop(k, None)

    # ---------------- 工具 ----------------
    def _session(self, did: str) -> Session:
        with self.lock:
            s = self.sessions.get(did)
            if not s:
                s = Session(did)
                self.sessions[did] = s
            s.updated = time.time()
            return s

    def _audio_of(self, track: dict) -> tuple[str, str]:
        """返回 (可直接播放的 url, streamFormat)。"""
        # 插件音源：走和本地转码一致的 /stream/t/ 路径（小度已确认能播该路径）
        if track.get("source") == "plugin":
            key = track.get("proxy_key") or ""
            if key:
                base = self.base_url().rstrip("/")
                return f"{base}/stream/t/{key}", P.STREAM_FORMAT_MP3
            return track.get("url") or "", P.STREAM_FORMAT_MP3

        track_id = track["id"]
        if self.transcoder and self.transcoder.needs_transcode(track_id):
            return self.transcoder.url_for(track_id), P.STREAM_FORMAT_MP3
        base = self.base_url().rstrip("/")
        real = self.library.get(track_id)
        fmt = P.stream_format_for(real.path) if real else P.STREAM_FORMAT_MP3
        return f"{base}/stream/{track_id}", fmt or P.STREAM_FORMAT_MP3

    def _search_tracks(self, keyword: str, limit: int) -> list[dict]:
        """搜索：只存元数据，不取流。播放时才取流。"""
        if self.media:
            try:
                songs = self.media.search(keyword, limit)
                if songs:
                    return [self._song_to_track(s, resolve=False) for s in songs]
            except Exception:  # noqa: BLE001
                pass
        # 回退：纯本地
        return [
            {"id": t.id, "title": t.title, "artist": t.artist, "source": "local"}
            for t in self.library.search(keyword, limit=limit)
        ]

    def _song_to_track(self, song, *, resolve: bool = False) -> dict:
        """把音源层的 Song 转成播放队列条目。

        resolve=False 时只存元数据（含 song 引用供后续取流）；
        resolve=True 时才取流并生成代理 key。
        """
        if song.source == "local":
            return {"id": song.track_id, "title": song.title, "artist": song.artist, "source": "local"}
        track = {
            "id": song.id,
            "title": song.title,
            "artist": song.artist,
            "source": "plugin",
            "url": "",
            "proxy_key": "",
            "_song": song,
        }
        if resolve:
            self._fill_plugin_stream(track)
        return track

    def _fill_plugin_stream(self, track: dict) -> None:
        """为插件条目取流并生成代理 key。失败时保留空 url，由 _directive 兜底。"""
        song = track.get("_song")
        if not song or not self.media:
            return
        try:
            resolved = self.media.resolve(song)
            track["url"] = resolved.get("url") or ""
            key = self.media.proxy_key(song)
            track["proxy_key"] = key or ""
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[skill] 取流失败 {song.title}: {e}\n")

    def _warm_track(self, track: dict) -> None:
        """预热插件音源的转码缓存，供真机秒开。

        转码约需 3-4 秒，若等小度拉流时才转，设备会因等待超时而放弃。
        所以在返回指令前，先取流 + 生成代理 key（key 稳定，转码一次后缓存）。
        """
        if not track or track.get("source") != "plugin":
            return
        # 先确保有 proxy_key（会触发取流）
        if not track.get("proxy_key"):
            self._fill_plugin_stream(track)
        # 若有稳定 key，再实际访问一次 /proxy/<key> 触发转码缓存
        key = track.get("proxy_key") or ""
        if key and self.media and hasattr(self.media, "proxy_resolve"):
            # 只做取流+key 生成，真正的转码由设备第一次拉流时触发；
            # 但为了秒开，这里主动让服务端完成转码缓存。
            try:
                import urllib.request as _ur
                base = self.base_url().rstrip("/")
                _ur.urlopen(f"{base}/stream/t/{key}", timeout=120).read(1)
            except Exception:  # noqa: BLE001
                pass

    def _directive(self, sess: Session, *, play_behavior: str) -> dict | None:
        track = sess.current()
        if not track:
            return None
        # 播放时才取流（避免搜索时对全部结果取流导致风控/刷屏）
        if track.get("source") == "plugin" and not track.get("proxy_key"):
            self._fill_plugin_stream(track)
        url, fmt = self._audio_of(track)
        if not url:
            # 取流失败，跳过这首，尝试下一首
            return None
        tok = f"{sess.device_id[:8]}-{track['id']}-{int(time.time() * 1000) % 100000000}"
        if len(sess.tokens) <= sess.index:
            sess.tokens.append(tok)
        else:
            sess.tokens[sess.index] = tok
        return P.audio_player_play(
            url,
            token=tok,
            play_behavior=play_behavior,
            title=track.get("title", ""),
            subtitle=track.get("artist", ""),
            stream_format=fmt,
        )

    # ---------------- 入口 ----------------
    def handle(self, body: dict) -> dict:
        rtype = P.request_type(body)
        session = body.get("session") or {"new": False}

        if rtype == "LaunchRequest":
            return self.on_launch(body, session)
        if rtype == "IntentRequest":
            return self.on_intent(body, session)
        if rtype == "SessionEndedRequest":
            return P.build_response(should_end_session=True, session=session)
        if rtype.startswith("AudioPlayer.") or rtype.startswith("Display.") or rtype.startswith("VideoPlayer."):
            return self.on_event(body, session)
        return P.build_response(output_speech="好的。", session=session)

    def on_launch(self, body: dict, session: dict) -> dict:
        return P.build_response(
            output_speech="想听什么歌？说歌名或歌手就行。",
            should_end_session=False,
            expect_speech=True,
            card=P.build_card("音乐点播", "说「放晴天」试试"),
            session=session,
        )

    def on_intent(self, body: dict, session: dict) -> dict:
        name = P.intent_name(body)
        did = P.device_id(body)

        # 自定义点播意图（可在技能平台自建，名字随意，这里兼容几种常见写法）
        if name in ("PlaySongIntent", "PlayMusicIntent", "SearchAndPlay", "play_song"):
            return self.on_play(body, session, did)

        # 官方通用意图
        if name in (P.COMMON + "stop_intent", P.COMMON + "cancel_intent"):
            return self.on_stop(body, session)
        if name == P.COMMON + "pause_intent":
            return self.on_pause(body, session)
        if name == P.COMMON + "continue_intent":
            return self.on_resume(body, session, did)
        if name == P.COMMON + "next_intent":
            return self.on_next(body, session, did)
        if name == P.COMMON + "previous_intent":
            return self.on_previous(body, session, did)
        if name == P.COMMON + "default_intent":
            # 兜底：把整句 query 当歌名搜一次
            return self.on_play(body, session, did, fallback_query=True)

        return P.build_response(output_speech="我还没学会这个指令。", session=session)

    def on_play(self, body: dict, session: dict, did: str, fallback_query: bool = False) -> dict:
        # 优先取槽位；槽位为空时退回整句 query（去掉"播放/放/我想听"等前缀）
        keyword = (
            P.slot_value(body, "song", "keyword", "query", "name", "music")
            or P.any_slot_value(body)
            or P.raw_query(body)
        )
        keyword = P.clean_keyword(keyword)
        if not keyword:
            return P.build_response(
                output_speech="你想听哪首歌？", should_end_session=False, expect_speech=True, session=session
            )

        hits = self._search_tracks(keyword, MAX_QUEUE)
        if not hits:
            return P.build_response(
                output_speech=f"没有找到{keyword}相关的歌。",
                should_end_session=False,
                expect_speech=True,
                session=session,
            )

        # 第一首精确匹配；后续队列改成「同歌手的其他歌」，实现"相关推荐"体验
        queue = [hits[0]]
        first_artist = (hits[0].get("artist") or "").strip()
        if first_artist and self.media:
            try:
                more = self._search_tracks(first_artist, MAX_QUEUE)
                # 去掉和第一首同名/同 id 的，避免重复
                seen = {hits[0].get("id")}
                for m in more:
                    if m.get("id") not in seen and m.get("title") != hits[0].get("title"):
                        seen.add(m.get("id"))
                        queue.append(m)
            except Exception:  # noqa: BLE001
                pass

        with self.lock:
            sess = self._session(did)
            sess.queue = queue
            sess.index = 0
            sess.tokens = [""] * len(sess.queue)
            # 首曲取流失败时自动向后找一首能播的
            directive = None
            played_index = 0
            for _ in range(len(sess.queue)):
                # 先预热首曲的转码缓存（避免真机拉流时现场转码超时）
                self._warm_track(sess.queue[sess.index])
                directive = self._directive(sess, play_behavior="REPLACE_ALL")
                if directive:
                    played_index = sess.index
                    break
                # 当前这首取流失败，跳到下一首
                if sess.index + 1 < len(sess.queue):
                    sess.index += 1
                else:
                    break
            self.save()

        first = queue[played_index]
        more = f"，共{len(queue)}首" if len(queue) > 1 else ""
        if directive:
            return P.build_response(
                directives=[directive],
                output_speech=f"为你播放{first['title']}，{first.get('artist', '')}{more}",
                # 官方文档：shouldEndSession=false 且 expectSpeech=false 时，
                # 技能仍在会话中但关闭麦克风，此时可返回 AudioPlayer.Play 指令。
                # 真机上这个组合有时比 shouldEndSession=true 更稳定。
                should_end_session=False,
                expect_speech=False,
                card=P.build_card(first["title"], first.get("artist", "")),
                session=session,
            )
        return P.build_response(
            output_speech=f"找到了{keyword}，但这几首暂时都取不到音源，稍后再试。",
            should_end_session=False,
            expect_speech=True,
            session=session,
        )

    def on_pause(self, body: dict, session: dict) -> dict:
        # AudioPlayer 协议里暂停就是 Stop，设备会记录进度
        return P.build_response(directives=[P.audio_player_stop()], should_end_session=True, session=session)

    def on_stop(self, body: dict, session: dict) -> dict:
        return P.build_response(directives=[P.audio_player_stop()], should_end_session=True, session=session)

    def on_resume(self, body: dict, session: dict, did: str) -> dict:
        with self.lock:
            sess = self._session(did)
            directive = self._directive(sess, play_behavior="REPLACE_ALL")
            self.save()
        if not directive:
            return P.build_response(output_speech="还没有可以继续的歌。", session=session)
        return P.build_response(directives=[directive], should_end_session=True, session=session)

    def on_next(self, body: dict, session: dict, did: str) -> dict:
        with self.lock:
            sess = self._session(did)
            nxt = sess.advance()
            directive = self._directive(sess, play_behavior="REPLACE_ALL") if nxt else None
            self.save()
        if not directive:
            return P.build_response(output_speech="已经是最后一首了。", session=session)
        return P.build_response(directives=[directive], should_end_session=True, session=session)

    def on_previous(self, body: dict, session: dict, did: str) -> dict:
        with self.lock:
            sess = self._session(did)
            if sess.index > 0:
                sess.index -= 1
                directive = self._directive(sess, play_behavior="REPLACE_ALL")
            else:
                directive = None
            self.save()
        if not directive:
            return P.build_response(output_speech="已经是第一首了。", session=session)
        return P.build_response(directives=[directive], should_end_session=True, session=session)

    # ---------------- 设备事件 ----------------
    def on_event(self, body: dict, session: dict) -> dict:
        name = P.request_type(body)
        did = P.device_id(body)

        if name == "AudioPlayer.PlaybackNearlyFinished":
            with self.lock:
                sess = self._session(did)
                nxt = sess.advance()
                directive = self._directive(sess, play_behavior="ENQUEUE") if nxt else None
                self.save()
            if directive:
                return P.build_response(directives=[directive], should_end_session=True, session=session)
            return {"version": "2.0", "response": {"directives": [], "shouldEndSession": True}}

        if name == "AudioPlayer.PlaybackStarted":
            return {"version": "2.0", "response": {"directives": [], "shouldEndSession": True}}

        if name == "AudioPlayer.PlaybackFinished":
            self.save()
            return {"version": "2.0", "response": {"directives": [], "shouldEndSession": True}}

        if name == "AudioPlayer.PlaybackStopped":
            self.save()
            return {"version": "2.0", "response": {"directives": [], "shouldEndSession": True}}

        if name == "AudioPlayer.PlaybackFailed":
            return P.build_response(
                output_speech="这首好像放不了，换一首吧。", session=session
            )

        return {"version": "2.0", "response": {"directives": [], "shouldEndSession": True}}

    # ---------------- 对外查询 ----------------
    def snapshot(self) -> dict:
        with self.lock:
            out = {}
            for did, s in self.sessions.items():
                cur = s.current()
                # 剔除 _song 等不可序列化字段
                cur_clean = None
                if cur:
                    cur_clean = {k: v for k, v in cur.items() if k != "_song"}
                # 队列前几首标题，便于调试
                titles = [t.get("title", "") for t in s.queue[:10]]
                out[did] = {
                    "index": s.index,
                    "size": len(s.queue),
                    "current": cur_clean,
                    "queue_titles": titles,
                    "volume": s.volume,
                }
            return out
