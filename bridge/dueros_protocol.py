"""DuerOS 自定义技能协议层（零依赖）。

全部结构对照官方 SDK 源码核实，非猜测：
  jokenwang/bot-sdk-python → dueros/Request.py、Response.py、Nlu.py、directive/AudioPlayer/

真实请求结构：
    {
      "request": {"type": "IntentRequest",
                  "intents": [{"name": "...", "slots": {"song": {"name":..., "value":...}}}],
                  "query": {"original": "..."},
                  "dialogState": "COMPLETED"},
      "session": {...},
      "context": {"System": {"device": {"deviceId":..., "supportedInterfaces": {...}},
                             "user": {...}, "application": {"applicationId":...}}}
    }

真实响应结构（注意是嵌套在 "response" 下，不是平铺在顶层）：
    {
      "version": "2.0",
      "context": {},
      "session": {...},
      "response": {"directives": [], "shouldEndSession": true,
                   "card": null, "outputSpeech": {"type":"PlainText","text":"..."},
                   "reprompt": ...}
    }

官方意图名（不是自造的 XxxIntent）：
    ai.dueros.common.default_intent / stop_intent / next_intent /
    cancel_intent / confirm_intent / pause_intent / continue_intent / previous_intent
"""

from __future__ import annotations

import uuid
from typing import Any

PLAY_BEHAVIOR = ("REPLACE_ALL", "ENQUEUE", "REPLACE_ENQUEUED")

# 小度端支持的流格式，取值来自 AudioPlayer/StreamFormatEnum.py
# 注意：只有这三种，WAV / FLAC / OGG 均不被支持，必须转成 MP3 或 M4A。
STREAM_FORMAT_MP3 = "AUDIO_MP3"
STREAM_FORMAT_M3U8 = "AUDIO_M3U8"
STREAM_FORMAT_M4A = "AUDIO_M4A"

# 按扩展名映射到小度能播的格式；不在表内的需要转码
EXT_STREAM_FORMAT = {
    ".mp3": STREAM_FORMAT_MP3,
    ".m4a": STREAM_FORMAT_M4A,
    ".aac": STREAM_FORMAT_M4A,
    ".m3u8": STREAM_FORMAT_M3U8,
}


def stream_format_for(path: str) -> str | None:
    """返回该文件可直接播放的格式；None 表示需要转码。"""
    import os

    return EXT_STREAM_FORMAT.get(os.path.splitext(path)[1].lower())

COMMON = "ai.dueros.common."


def new_token() -> str:
    return uuid.uuid4().hex[:20]


# ---------------------------------------------------------------- 指令

def audio_player_play(
    url: str,
    *,
    token: str | None = None,
    play_behavior: str = "REPLACE_ALL",
    offset_ms: int = 0,
    title: str = "",
    subtitle: str = "",
    artwork: str = "",
    stream_format: str = STREAM_FORMAT_MP3,
) -> dict:
    """AudioPlayer.Play 指令。url 必须是 HTTPS 公网可达。"""
    stream = {
        "token": token or new_token(),
        "url": url,
        "offsetInMilliSeconds": int(offset_ms),
        "streamFormat": stream_format,
    }
    item: dict[str, Any] = {"stream": stream}
    if title or subtitle or artwork:
        info: dict[str, Any] = {"title": title or "未知曲目", "type": "MUSIC"}
        if subtitle:
            info["titleSubtext1"] = subtitle
        if artwork:
            info["art"] = {"sources": [{"url": artwork, "size": "LARGE"}]}
        item["playerInfo"] = info
    return {
        "type": "AudioPlayer.Play",
        "playBehavior": play_behavior if play_behavior in PLAY_BEHAVIOR else "REPLACE_ALL",
        "audioItem": item,
    }


def audio_player_stop() -> dict:
    return {"type": "AudioPlayer.Stop"}


def audio_player_clear_queue() -> dict:
    return {"type": "AudioPlayer.ClearQueue", "clearBehavior": "CLEAR_ALL"}


# ---------------------------------------------------------------- 响应

def _speech(text: str | None) -> dict | None:
    if not text:
        return None
    if "<speak>" in text:
        return {"type": "SSML", "ssml": text}
    return {"type": "PlainText", "text": text}


def build_response(
    *,
    directives: list[dict] | None = None,
    output_speech: str | None = None,
    should_end_session: bool = True,
    card: dict | None = None,
    reprompt: str | None = None,
    expect_speech: bool | None = None,
    session: dict | None = None,
) -> dict:
    """按官方 Response.build() 的结构拼装。"""
    body: dict[str, Any] = {
        "directives": directives or [],
        "shouldEndSession": should_end_session,
        "card": card,
        "outputSpeech": _speech(output_speech),
        "reprompt": {"outputSpeech": _speech(reprompt)} if reprompt else None,
    }
    if isinstance(expect_speech, bool):
        body["expectSpeech"] = expect_speech

    out: dict[str, Any] = {
        "version": "2.0",
        "context": {},
        "session": session or {"new": False},
        "response": body,
    }
    return out


def build_card(title: str, content: str = "", image_url: str = "") -> dict:
    card: dict[str, Any] = {"type": "standard", "title": title, "content": content}
    if image_url:
        card["image"] = {"url": image_url}
    return card


# ---------------------------------------------------------------- 请求解析

def request_type(body: dict) -> str:
    return ((body.get("request") or {}).get("type") or "").strip()


def first_intent(body: dict) -> dict:
    """意图在 request.intents（数组）里，取第一个。"""
    req = body.get("request") or {}
    intents = req.get("intents") or []
    if isinstance(intents, list) and intents:
        return intents[0] or {}
    if isinstance(intents, dict):
        return intents
    return {}


def intent_name(body: dict) -> str:
    return (first_intent(body).get("name") or "").strip()


def slot_value(body: dict, *names: str) -> str:
    """槽位在 intent.slots 里，是 dict：{槽位名: {"name":..., "value":...}}。

    通配槽位（歌名、任意词）的 value 往往是 JSON 字符串，
    如 {"song":"晴天","origin":"晴天"}，这里一并解出。
    """
    slots = first_intent(body).get("slots") or {}
    if not isinstance(slots, dict):
        return ""
    for n in names:
        v = slots.get(n)
        if isinstance(v, dict):
            val = v.get("value")
            if isinstance(val, list):
                val = val[0] if val else ""
            if val:
                return _unwrap(str(val).strip())
        elif isinstance(v, str) and v.strip():
            return _unwrap(v.strip())
    return ""


def _unwrap(val: str) -> str:
    """若槽位值是 JSON 字符串，取 song/origin/value/query 等字段。"""
    s = val.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            import json as _json

            obj = _json.loads(s)
            if isinstance(obj, dict):
                for key in ("song", "keyword", "query", "origin", "value", "name", "music"):
                    if obj.get(key):
                        return str(obj[key]).strip()
        except (ValueError, TypeError):
            pass
    return s


def any_slot_value(body: dict) -> str:
    """取任意一个非空槽位值。

    DuerOS 的「通配槽位」标识是 sys.wildcard-slot，
    而开发者也可能自定义成 song / keyword 等，这里不挑名字统一兜底。
    """
    slots = first_intent(body).get("slots") or {}
    if not isinstance(slots, dict):
        return ""
    for _k, v in slots.items():
        if isinstance(v, dict):
            val = v.get("value")
            if isinstance(val, list):
                val = val[0] if val else ""
            if val:
                got = _unwrap(str(val).strip())
                if got:
                    return got
        elif isinstance(v, str) and v.strip():
            got = _unwrap(v.strip())
            if got:
                return got
    return ""


def clean_keyword(text: str) -> str:
    """清洗用户口语，提取真正的歌名。

    处理三类噪音：
      1. 动词前缀：「播放晴天」→「晴天」
      2. 书名号/引号：「《晴天》」→「晴天」
      3. 首尾标点与「的」：「晴天的」→「晴天」
    """
    s = (text or "").strip()

    # 先剥动词前缀（可能叠加，如「帮我播放」）
    changed = True
    while changed:
        changed = False
        for pre in (
            "帮我", "给我", "请", "麻烦", "我想听", "我要听", "我想", "我要",
            "来一首", "来首", "放一首", "放首", "播放一下", "播放", "放",
            "听一下", "听一首", "听首", "听", "搜索", "搜一下", "搜",
        ):
            if s.startswith(pre) and len(s) > len(pre):
                s = s[len(pre):].strip()
                changed = True
                break

    # 先去掉首尾标点（否则「《晴天》。」结尾是句号，括号不闭合）
    s = s.strip("，,。.！!？?、;；:： ")

    # 剥离开头/结尾的书名号、引号、括号（不要求配对：ASR 常吞掉右括号）
    openers = "《「『【〈“‘(（["
    closers = "》」』】〉”’)]）"
    s = s.strip(openers + closers + "，,。.！!？?、;；:： ")
    # 再剥一层（可能嵌套，如《「晴天」》）
    s = s.strip(openers + closers + "，,。.！!？?、;；:： ")

    stripped = True
    while stripped:
        stripped = False
        for left, right in (("(", ")"), ("[", "]")):
            if s.startswith(left) and s.endswith(right) and len(s) > len(left) + len(right):
                s = s[len(left):-len(right)].strip()
                stripped = True
                break

    # 括号剥掉后可能又露出标点，再清一次
    s = s.strip("，,。.！!？?、;；:： ")

    # 末尾的「的歌」「的」「歌曲」属于口语尾巴，去掉
    for tail in ("这首歌", "歌曲", "的歌", "的"):
        if s.endswith(tail) and len(s) > len(tail):
            s = s[: -len(tail)].strip()
            break

    return s.strip("，,。.！!？?、;；:： ")


def raw_query(body: dict) -> str:
    return (((body.get("request") or {}).get("query") or {}).get("original") or "").strip()


def device_id(body: dict) -> str:
    ctx = body.get("context") or {}
    dev = ((ctx.get("System") or {}).get("device") or {})
    return (dev.get("deviceId") or body.get("cuid") or "default").strip() or "default"


def supported_interfaces(body: dict) -> dict:
    ctx = body.get("context") or {}
    dev = ((ctx.get("System") or {}).get("device") or {})
    return dev.get("supportedInterfaces") or {}


def audio_player_context(body: dict) -> dict:
    return ((body.get("context") or {}).get("AudioPlayer") or {})


def event_token(body: dict) -> str:
    return (((body.get("request") or {}).get("token")) or "").strip()
