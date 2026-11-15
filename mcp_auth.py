# -*- coding: utf-8 -*-
"""小度 MCP 设备码授权流程（自动获取 access_token）

用法：
    python mcp_auth.py --appkey <AppKey> --secret <SecretKey>

流程：
    1. 用 AppKey 获取 device_code + user_code + 授权链接
    2. 打印授权链接（或二维码），你用小度App/百度App扫码或输码授权
    3. 授权后，脚本轮询换取 access_token + refresh_token
    4. 保存到 mcp_token.json

token 有效期 30 天，可刷新；refresh_token 只能用一次。
"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEVICE_CODE_URL = "https://openapi.baidu.com/oauth/2.0/device/code"
TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"


def _get(url, timeout=15):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "dueros.baidu.com")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, data, timeout=15):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def step1_get_device_code(appkey):
    url = (
        f"{DEVICE_CODE_URL}?response_type=device_code"
        f"&client_id={urllib.parse.quote(appkey)}"
        f"&scope=basic,dueros"
    )
    data = _get(url)
    if "device_code" not in data:
        raise RuntimeError(f"获取设备码失败: {data}")
    return data


def step2_show_auth(data):
    print("=" * 60)
    print("请完成授权（二选一）：")
    print()
    print(f"  方式1 输入用户码授权：")
    print(f"    授权网址: {data.get('verification_url')}")
    print(f"    用户码  : {data.get('user_code')}")
    print()
    print(f"  方式2 扫码授权（用百度App/小度App/微信扫）:")
    print(f"    二维码链接: {data.get('qrcode_url')}")
    print()
    print(f"  注意：设备码 {data.get('expires_in', 300)} 秒内有效")
    print("=" * 60)


def step3_poll_token(appkey, secret, device_code, interval=5, max_wait=300):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        url = (
            f"{TOKEN_URL}?grant_type=device_token"
            f"&code={urllib.parse.quote(device_code)}"
            f"&client_id={urllib.parse.quote(appkey)}"
            f"&client_secret={urllib.parse.quote(secret)}"
        )
        try:
            data = _get(url)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            data = json.loads(body) if body else {"error": f"HTTP {e.code}"}

        if "access_token" in data:
            return data
        err = data.get("error", "")
        desc = data.get("error_description", "")
        # authorization_pending / slow_down 是正常的"还没授权"
        if err in ("authorization_pending", "authorization_declined", "slow_down"):
            if err == "authorization_declined":
                raise RuntimeError("用户拒绝了授权")
            time.sleep(max(interval, 5))
            continue
        # 其他错误直接抛
        raise RuntimeError(f"换取 token 失败: {err} {desc}")
    raise RuntimeError("授权超时")


def main():
    ap = argparse.ArgumentParser(description="小度 MCP 设备码授权")
    ap.add_argument("--appkey", required=True, help="应用的 AppKey")
    ap.add_argument("--secret", required=True, help="应用的 SecretKey")
    args = ap.parse_args()

    print("[1/3] 获取设备码...")
    dc = step1_get_device_code(args.appkey)
    print(f"  device_code 已获取，{dc.get('expires_in')} 秒内有效")

    print()
    print("[2/3] 引导授权...")
    step2_show_auth(dc)

    print()
    print("[3/3] 等待你授权后自动轮询换取 token...")
    input("  >>> 请先完成上面的授权，完成后按回车开始轮询 <<< ")

    token = step3_poll_token(args.appkey, args.secret, dc["device_code"], dc.get("interval", 5))

    print()
    print("=" * 60)
    print("✅ 授权成功！")
    print(f"  access_token 前20字符: {token.get('access_token','')[:20]}...")
    print(f"  expires_in: {token.get('expires_in')} 秒")
    print(f"  refresh_token 前20字符: {token.get('refresh_token','')[:20]}...")
    print("=" * 60)

    # 保存
    out = {
        "appkey": args.appkey,
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "expires_in": token.get("expires_in"),
        "created_at": time.time(),
    }
    with open("mcp_token.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print()
    print("✅ token 已保存到 mcp_token.json")


if __name__ == "__main__":
    main()
