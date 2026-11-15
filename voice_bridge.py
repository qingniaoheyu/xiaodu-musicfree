# -*- coding: utf-8 -*-
"""小度语音点歌 完整播放器（懒加载 + 同歌手连播 + 上一首/下一首）

语音口令：
    本地 <歌名>      → 搜歌（第一首精确 + 同歌手连播）
    本地 音乐        → 播网易云新歌榜
    下一首           → 切下一首
    上一首           → 回到上一首
    停止 / 暂停      → 停止

机制：
    1. 队列存元数据（不预取流），播到哪首才调 /api/resolve 取 URL（懒加载）
    2. 预取下一首，隐藏转码延迟
    3. 轮询设备状态判断"播完"，自动下一首
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

import mcp_call

# 设备标识从 device.json 读取（该文件已被 .gitignore 排除，不提交到仓库）
def _load_device():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device.json")
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
            return d.get("cuid", ""), d.get("client_id", "")
    except (OSError, ValueError):
        return "", ""

CUID, CLIENT_ID = _load_device()
LOCAL_SERVICE = "http://127.0.0.1:8765"

SONG_PATTERNS = [r"本地[，,]?(.+)"]
NEWSONG_WORDS = ["音乐", "歌单", "随便", "来点歌", "放首歌", "推荐"]
NEXT_WORDS = ["下一首", "下一曲", "切歌", "换一首", "下一"]
PREV_WORDS = ["上一首", "上一曲", "回到上一首", "上一"]
STOP_WORDS = ["停止", "别放了", "不听了", "停"]


class Player:
    def __init__(self, client):
        self.client = client
        self.queue = []       # 元数据列表 [{song_id,title,artist,duration_ms,source,track_id,plugin_item}]
        self.index = -1
        self.playing = False
        self.current_url = ""
        self.current_duration_ms = 0
        self._prefetch_lock = threading.Lock()
        self._prefetched = {}  # index -> url

    def speak(self, text):
        try:
            self.client.call_tool("xiaodu_speak", {
                "text": text, "cuid": CUID, "client_id": CLIENT_ID,
            })
        except Exception:
            pass

    def push_url(self, url):
        self.client.call_tool("push_resource_to_xiaodu", {
            "resource_type": "audio",
            "audio_url": url,
            "cuid": CUID,
            "client_id": CLIENT_ID,
        })

    def resolve_url(self, item):
        """调 /api/resolve 单首取流（懒加载）。"""
        params = {
            "song_id": item.get("song_id", ""),
            "source": item.get("source", ""),
            "track_id": item.get("track_id", ""),
            "title": item.get("title", ""),
            "artist": item.get("artist", ""),
            "duration_ms": item.get("duration_ms", "0"),
            "plugin_item": json.dumps(item.get("plugin_item", {}), ensure_ascii=False),
        }
        url = f"{LOCAL_SERVICE}/api/resolve?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("url") if data.get("ok") else None

    def play_index(self, i):
        if i < 0 or i >= len(self.queue):
            return False
        self.index = i
        item = self.queue[i]

        # 优先用预取好的 URL
        url = self._prefetched.get(i)
        if not url:
            print(f"  取流中: {item['title']}...")
            url = self.resolve_url(item)
        if not url:
            print(f"  [X] 取流失败: {item['title']}")
            # 不在这里 speak，由调用方（next 静默跳过 / 首曲点歌失败）决定是否提示
            return False

        self.current_url = url
        self.current_duration_ms = int(item.get("duration_ms", 0) or 0)
        self.playing = True
        self.push_url(url)
        print(f"  ▶ 播放[{i+1}/{len(self.queue)}] {item['title']} - {item['artist']}")
        self.speak(f"正在为你播放{item['title']}")
        # 预取下一首
        self.prefetch(i + 1)
        return True

    def prefetch(self, i):
        """后台预取下一首的 URL，隐藏转码延迟。"""
        if i < 0 or i >= len(self.queue) or i in self._prefetched:
            return
        item = self.queue[i]

        def _do():
            try:
                url = self.resolve_url(item)
                if url:
                    self._prefetched[i] = url
                    print(f"  预取好下一首: {item['title']}")
            except Exception as e:
                print(f"  预取失败: {e}")

        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def next(self):
        """切下一首；若当前这首取流失败，自动跳过继续往后找能播的。"""
        start = self.index + 1
        i = start
        while i < len(self.queue):
            if self.play_index(i):
                return True
            # 取流失败，跳过，继续下一首
            print(f"  跳过取流失败的歌，继续下一首")
            i += 1
        # 队列里从 start 往后都没有能播的
        self.speak("后面没有能播放的歌了")
        self.playing = False
        return False

    def prev(self):
        if self.index > 0:
            return self.play_index(self.index - 1)
        return False

    def stop(self):
        self.playing = False
        self.speak("好的，已停止")
        print("  ■ 已停止")

    def load_playlist(self, song=None, mode=None):
        if mode == "newsongs":
            url = f"{LOCAL_SERVICE}/api/playlist?mode=newsongs&limit=20"
        else:
            url = f"{LOCAL_SERVICE}/api/playlist?q={urllib.parse.quote(song)}&limit=20"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data.get("data") or []
        self.queue = items
        self.index = -1
        self._prefetched = {}
        print(f"  队列就绪，{len(items)} 首")
        return len(items) > 0

    def device_audio_state(self):
        try:
            st = self.client.call_tool("xiaodu_get_device_status", {
                "cuid": CUID, "client_id": CLIENT_ID,
            })
            return (st.get("audio") or {})
        except Exception:
            return {}


def load_token():
    with open("mcp_token.json", "r", encoding="utf-8-sig") as f:
        return json.load(f)["access_token"]


def get_history(client, limit=8):
    return client.call_tool("xiaodu_list_device_history", {
        "cuid": CUID,
        "client_id": CLIENT_ID,
        "limit": limit,
    })


def parse_command(query):
    q = (query or "").strip()
    if not q:
        return None, None
    for w in STOP_WORDS:
        if q == w or q.startswith(w):
            return "stop", None
    for w in NEXT_WORDS:
        if q == w or q.startswith(w):
            return "next", None
    for w in PREV_WORDS:
        if q == w or q.startswith(w):
            return "prev", None
    for w in NEWSONG_WORDS:
        if q == w:
            return "newsongs", None
    for pat in SONG_PATTERNS:
        m = re.search(pat, q)
        if m:
            song = m.group(1).strip().strip("。.！!？?，, ")
            if song:
                return "song", song
    return None, None


def main():
    token = load_token()
    client = mcp_call.McpClient(token)
    client.initialize()
    client.notify("notifications/initialized")

    player = Player(client)
    last_ts = 0
    last_progress_check = 0

    print("=" * 60)
    print("小度语音点歌 完整播放器已启动")
    print("口令：本地 <歌名> / 本地 音乐 / 下一首 / 上一首 / 停止")
    print("=" * 60)

    while True:
        try:
            history = get_history(client, limit=8)
            items = (history.get("items") or []) if isinstance(history, dict) else []
            for item in items:
                if item.get("trigger") != "voice":
                    continue
                ts = item.get("timestamp") or 0
                if ts <= last_ts:
                    continue
                last_ts = max(last_ts, ts)
                query = item.get("query") or ""
                cmd, arg = parse_command(query)
                if not cmd:
                    continue

                print(f"\n[指令] {query!r} → {cmd} {arg or ''}")
                if cmd == "song":
                    if player.load_playlist(song=arg):
                        if not player.play_index(0):
                            # 首曲取流失败，尝试跳下一首
                            player.speak(f"{arg}第一首放不了，试试别的")
                            player.next()
                    else:
                        player.speak(f"没有找到{arg}这首歌")
                elif cmd == "newsongs":
                    if player.load_playlist(mode="newsongs"):
                        if not player.play_index(0):
                            player.next()
                    else:
                        player.speak("新歌榜获取失败")
                elif cmd == "next":
                    if not player.next():
                        pass  # next() 内部已提示
                elif cmd == "prev":
                    if not player.prev():
                        player.speak("已经是第一首了")
                elif cmd == "stop":
                    player.stop()

            # 连播检查
            now = time.time()
            if player.playing and (now - last_progress_check) > 5:
                last_progress_check = now
                audio = player.device_audio_state()
                activity = audio.get("activity", "")
                offset = audio.get("offset_ms", 0) or 0
                dur = player.current_duration_ms or 0
                finished = activity in ("FINISHED", "IDLE", "STOPPED")
                if not finished and dur > 0 and offset > 0 and offset >= dur - 5000:
                    finished = True
                if finished:
                    print(f"  ◀ 当前曲播完 (activity={activity})")
                    if not player.next():
                        player.playing = False
                        print("  队列播完")

        except Exception as e:
            print(f"[错误] {e}")

        time.sleep(3)


if __name__ == "__main__":
    main()
