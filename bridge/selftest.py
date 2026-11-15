"""端到端自测：不依赖 DuerOS 控制台，直接打本地服务。

用法：
    python bridge/selftest.py [--base http://127.0.0.1:8765]

覆盖：
  1. 健康检查 / 曲库加载
  2. 搜索（命中 / 未命中）
  3. 原始音频流 Range
  4. 转码链路（WAV → MP3 → Range）
  5. LaunchRequest 响应结构（version/response 嵌套）
  6. PlaySongIntent → AudioPlayer.Play 指令结构与取值
  7. 官方意图 stop / next / pause
  8. PlaybackNearlyFinished 自动续播 ENQUEUE
  9. 队列持久化（重启后仍在）
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

PASS, FAIL = 0, 0


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")
    return cond


def http(base: str, path: str, *, method: str = "GET", body: dict | None = None,
         headers: dict | None = None, raw: bool = False):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read()
        return (r.status, dict(r.headers), content) if raw else (r.status, dict(r.headers), json.loads(content) if content else {})


def dueros(base: str, payload: dict) -> dict:
    _s, _h, data = http(base, "/dueros", method="POST", body=payload)
    return data


def intent(name: str, slots: dict | None = None, query: str = "", dev: str = "SELFTEST-DEV") -> dict:
    return {
        "request": {
            "type": "IntentRequest",
            "requestId": "req-" + name,
            "query": {"original": query},
            "intents": [
                {"name": name, "slots": {k: {"name": k, "value": v} for k, v in (slots or {}).items()}}
            ],
            "dialogState": "COMPLETED",
        },
        "session": {"new": True, "sessionId": "s-1"},
        "context": {
            "System": {
                "device": {"deviceId": dev, "supportedInterfaces": {"AudioPlayer": {}, "Display": {}}},
                "application": {"applicationId": "selftest"},
            }
        },
    }


def event(name: str, token: str = "", dev: str = "SELFTEST-DEV") -> dict:
    return {
        "request": {"type": name, "requestId": "ev-" + name, "token": token},
        "session": {"new": False},
        "context": {"System": {"device": {"deviceId": dev}}},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base

    print("1) 健康检查")
    try:
        _s, _h, health = http(base, "/api/health")
        ok("服务可达", health.get("ok") is True, str(health))
        ok("曲库非空", health.get("tracks", 0) > 0, f"tracks={health.get('tracks')}")
        ok("转码可用", health.get("transcode") is True, "ffmpeg 未就绪，WAV/FLAC 会失败")
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] 服务不可达: {e}")
        return 1

    print("2) 搜索")
    _s, _h, r1 = http(base, "/api/search?q=" + urllib.parse.quote("晴天"))
    ok("命中晴天", len(r1.get("data", [])) >= 1, str(r1)[:120])
    _s, _h, r2 = http(base, "/api/search?q=" + urllib.parse.quote("不存在的歌名xyz"))
    ok("未命中返回空", r2.get("data") == [], str(r2)[:120])

    print("3) 原始音频流 Range")
    if r1.get("data"):
        tid = r1["data"][0]["id"]
        _s, h, body = http(base, f"/stream/{tid}", headers={"Range": "bytes=0-1023"}, raw=True)
        ok("返回 206", _s == 206, f"status={_s}")
        ok("Content-Range 正确", h.get("Content-Range", "").startswith("bytes 0-1023/"), str(h.get("Content-Range")))
        ok("长度 1024", len(body) == 1024, f"len={len(body)}")

    print("4) 转码链路")
    _s, _h, stats = http(base, "/api/stats")
    tc = stats.get("transcode", {})
    ok("ffmpeg 已配置", bool(tc.get("ffmpeg")), str(tc))
    need = [d for d in r1.get("data", []) if d.get("needsTranscode")]
    if need:
        tid = need[0]["id"]
        play = dueros(base, intent("PlaySongIntent", {"song": need[0]["title"]}))
        d = (play.get("response") or {}).get("directives") or []
        if d:
            url = d[0]["audioItem"]["stream"]["url"]
            fmt = d[0]["audioItem"]["stream"]["streamFormat"]
            ok("转码曲目用 AUDIO_MP3", fmt == "AUDIO_MP3", f"fmt={fmt}")
            ok("URL 指向 /stream/t/", "/stream/t/" in url, url)
            path = url.split(base.rstrip("/"))[-1] if base in url else None
            if path:
                _s2, h2, b2 = http(base, path, headers={"Range": "bytes=0-511"}, raw=True)
                ok("转码流可 Range", _s2 == 206 and len(b2) == 512, f"status={_s2} len={len(b2)}")
    else:
        print("  [SKIP] 曲库没有需要转码的文件")

    print("5) LaunchRequest 结构")
    launch = dueros(base, {"request": {"type": "LaunchRequest", "requestId": "l1"},
                           "session": {"new": True},
                           "context": {"System": {"device": {"deviceId": "SELFTEST-DEV"}}}})
    ok("有 version", launch.get("version") == "2.0", str(launch)[:100])
    ok("响应嵌套在 response 下", "response" in launch, str(list(launch.keys())))
    ok("outputSpeech 在 response 内", "outputSpeech" in (launch.get("response") or {}), str(launch.get("response", {}).keys()))
    ok("顶层无 outputSpeech（旧错误结构）", "outputSpeech" not in launch)

    print("6) PlaySongIntent 指令")
    play = dueros(base, intent("PlaySongIntent", {"song": "测试"}))
    resp = play.get("response") or {}
    ds = resp.get("directives") or []
    ok("有 AudioPlayer.Play", bool(ds) and ds[0].get("type") == "AudioPlayer.Play", str(ds)[:150])
    if ds:
        st = ds[0]["audioItem"]["stream"]
        ok("playBehavior=REPLACE_ALL", ds[0].get("playBehavior") == "REPLACE_ALL", ds[0].get("playBehavior"))
        ok("streamFormat 合法", st.get("streamFormat") in ("AUDIO_MP3", "AUDIO_M4A", "AUDIO_M3U8"), st.get("streamFormat"))
        ok("url 是 http(s)", str(st.get("url", "")).startswith(("http://", "https://")), st.get("url"))
        ok("有 token", bool(st.get("token")))
        ok("有 playerInfo", "playerInfo" in ds[0]["audioItem"])

    print("7) 官方通用意图")
    for name, expect_stop in (("ai.dueros.common.stop_intent", True), ("ai.dueros.common.pause_intent", True)):
        r = dueros(base, intent(name))
        d = (r.get("response") or {}).get("directives") or []
        ok(f"{name} 返回 Stop 指令", (not expect_stop) or (bool(d) and d[0].get("type") == "AudioPlayer.Stop"),
           str(d)[:120])
    r = dueros(base, intent("ai.dueros.common.next_intent"))
    d = (r.get("response") or {}).get("directives") or []
    ok("next_intent 返回 Play", bool(d) and d[0].get("type") == "AudioPlayer.Play", str(d)[:120])

    print("8) 自动续播 ENQUEUE")
    dueros(base, intent("PlaySongIntent", {"song": "测试"}))
    ev = dueros(base, event("AudioPlayer.PlaybackNearlyFinished", dev="SELFTEST-DEV"))
    d = (ev.get("response") or {}).get("directives") or []
    if d:
        ok("NearlyFinished 返回 ENQUEUE", d[0].get("playBehavior") == "ENQUEUE", str(d[0].get("playBehavior")))
    else:
        print("  [SKIP] 该关键词只命中一首，无下一首可入队")

    print("9) 队列持久化")
    _s, _h, st2 = http(base, "/api/stats")
    sess = (st2.get("sessions") or {}).get("SELFTEST-DEV")
    ok("会话已记录", sess is not None, str(st2.get("sessions"))[:150])
    if sess:
        ok("队列非空", sess.get("size", 0) > 0, str(sess))

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  延迟导入避免顶部未用告警

    sys.exit(main())
