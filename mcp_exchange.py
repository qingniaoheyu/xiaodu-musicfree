# -*- coding: utf-8 -*-
"""用 device_code 换 access_token（SecretKey 只在本地填写，不外传）

用法：
    python mcp_exchange.py <SecretKey>

会读取同目录的 device_code.json，换取 token 并保存 mcp_token.json
"""

import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"

# AppKey 从 device.json 读取（该文件已被 .gitignore 排除，不提交）
def _load_appkey():
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device.json")
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            return json.load(f).get("appkey", "")
    except (OSError, ValueError):
        return ""

APPKEY = _load_appkey()


def main():
    if len(sys.argv) < 2:
        print("用法: python mcp_exchange.py <SecretKey>")
        sys.exit(2)
    secret = sys.argv[1]

    # 读 device_code
    with open("device_code.json", "r", encoding="utf-8-sig") as f:
        dc = json.load(f)
    device_code = dc["device_code"]

    url = (
        f"{TOKEN_URL}?grant_type=device_token"
        f"&code={urllib.parse.quote(device_code)}"
        f"&client_id={urllib.parse.quote(APPKEY)}"
        f"&client_secret={urllib.parse.quote(secret)}"
    )
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "dueros.baidu.com")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"HTTP {e.code}: {body}")
        sys.exit(1)

    if "access_token" not in data:
        print(f"换取失败: {data}")
        sys.exit(1)

    print("✅ 成功！")
    print(f"  access_token 前20: {data['access_token'][:20]}...")
    print(f"  refresh_token 前20: {data.get('refresh_token','')[:20]}...")
    print(f"  expires_in: {data.get('expires_in')} 秒")

    out = {
        "appkey": APPKEY,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
    }
    with open("mcp_token.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("✅ 已保存到 mcp_token.json")


if __name__ == "__main__":
    main()
