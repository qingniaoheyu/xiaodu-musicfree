# -*- coding: utf-8 -*-
"""小度 MCP 客户端（StreamableHTTP + JSON-RPC）

用法：
    python mcp_call.py list_devices          # 列出设备
    python mcp_call.py speak "文本" cuid client_id   # 让小度朗读
    python mcp_call.py push_audio <audio_url> cuid client_id  # 推音频给小度播放
    python mcp_call.py play_music "歌名" cuid client_id      # 让小度按歌名播放

token 从 mcp_token.json 读取。
"""

import json
import sys
import urllib.request

MCP_URL = "https://xiaodu.baidu.com/dueros_mcp_server/mcp/"


def load_token():
    with open("mcp_token.json", "r", encoding="utf-8-sig") as f:
        return json.load(f)["access_token"]


class McpClient:
    def __init__(self, token):
        self.token = token
        self.session_id = None
        self.rpc_id = 0

    def _post(self, payload, timeout=30):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(MCP_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("ACCESS_TOKEN", self.token)
        if self.session_id:
            req.add_header("Mcp-Session-Id", self.session_id)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            return r.read().decode("utf-8", "ignore")

    def _parse(self, text):
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        # SSE 格式：找 data: {...}
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except ValueError:
                        continue
        return None

    def rpc(self, method, params=None):
        self.rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self.rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        text = self._post(payload)
        data = self._parse(text)
        if data is None:
            raise RuntimeError(f"响应无法解析: {text[:300]}")
        if "error" in data:
            raise RuntimeError(f"MCP 错误: {data['error']}")
        return data.get("result")

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    def initialize(self):
        return self.rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xiaodu-musicfree", "version": "1.0.0"},
        })

    def call_tool(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        # 提取 text 内容
        texts = []
        for chunk in result.get("content", []) or []:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                texts.append(chunk.get("text", ""))
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined

    def list_devices(self):
        return self.call_tool("list_user_devices", {})


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python mcp_call.py list_devices")
        print("  python mcp_call.py speak <文本> <cuid> <client_id>")
        print("  python mcp_call.py push_audio <audio_url> <cuid> <client_id>")
        print("  python mcp_call.py play_music <歌名> <cuid> <client_id>")
        sys.exit(2)

    token = load_token()
    client = McpClient(token)
    client.initialize()
    client.notify("notifications/initialized")

    cmd = sys.argv[1]

    if cmd == "list_devices":
        result = client.list_devices()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "speak":
        text, cuid, cid = sys.argv[2], sys.argv[3], sys.argv[4]
        result = client.call_tool("xiaodu_speak", {"text": text, "cuid": cuid, "client_id": cid})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "push_audio":
        url, cuid, cid = sys.argv[2], sys.argv[3], sys.argv[4]
        result = client.call_tool("push_resource_to_xiaodu", {
            "resource_type": "audio", "audio_url": url, "cuid": cuid, "client_id": cid,
        })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "play_music":
        song, cuid, cid = sys.argv[2], sys.argv[3], sys.argv[4]
        result = client.call_tool("xiaodu_play_music", {
            "song": song, "cuid": cuid, "client_id": cid,
        })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"未知命令: {cmd}")
        sys.exit(2)


if __name__ == "__main__":
    main()
