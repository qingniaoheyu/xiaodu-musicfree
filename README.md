# 小度语音点歌服务（MusicFree 音源 + 官方 MCP）

对小度说「**小度小度，本地，孤勇者**」，它就会用我们自己的免费音源播放这首歌。

## 一、最终效果

```
你对小度说「本地，孤勇者」
  → 小度语音识别 → 记进设备历史
  → 守护进程轮询历史，抓到「本地孤勇者」
  → 提取歌名"孤勇者"
  → 本地服务搜索（网易云移动端，免登录）→ 取流（B站回退）→ ffmpeg 转 MP3
  → 官方 MCP push_resource_to_xiaodu 推给小度
  → 小度播放 + 说「正在为你播放孤勇者」
```

**全程不刷机、不买会员、不受 VIP 限制、不需要 miniPC 麦克风。** 语音识别用小度原生，音源用我们自己的。

## 二、为什么是这个方案（踩坑总结）

| 尝试过的方案 | 结果 | 卡点 |
|---|---|---|
| 自定义技能 + AudioPlayer | ❌ | 未发布技能在调试模式下，真机不执行播放 |
| 刷机装第三方 App | ❌ | 小度语音控制不了第三方 App，且刷机变砖风险高 |
| 手机 MusicFree + 蓝牙 | ⚠️ 能用 | 但入口在手机，不是语音 |
| 官方 MCP（推音频） | ✅ **成功** | 个人开发者可申请 MCP 权限（之前误以为要企业认证） |

**关键突破**：小度官方 MCP 的 `push_resource_to_xiaodu` 工具，能把任意公网音频 URL 推给小度播放，且个人开发者可申请权限。

## 三、目录结构

```
xiaodu-musicfree/
├─ daemon.ps1          守护进程（主服务+隧道+语音桥，都自动拉起）
├─ start-service.bat   开机自启入口（启动守护进程）
├─ start.bat           手动启动（前台）
├─ mcp_auth.py         MCP 设备码授权（获取 access_token）
├─ mcp_exchange.py     用 device_code 换 token
├─ mcp_call.py         MCP 客户端（list_devices/speak/push_audio 等）
├─ voice_bridge.py     ★ 语音桥（轮询历史→搜歌→推送播放）
├─ mcp_token.json      access_token（含敏感信息，勿外传）
├─ bridge/             本地音乐服务（搜索/取流/转码/HTTP服务）
│  ├─ server.py        HTTP 服务 + DuerOS 协议
│  ├─ media_source.py  统一音源层
│  ├─ plugin_source.py 网易云插件调用
│  ├─ plugin_runner.js Node 插件运行时
│  ├─ transcoder.py    ffmpeg 转码
│  └─ config.json      配置
├─ plugins/            网易云插件（内置）
├─ vendor/             ffmpeg + node_modules
├─ music/              本地音乐（可选）
├─ cache/              转码缓存
├─ logs/               日志
└─ README.md           本文档
```

## 四、核心机制说明

### 1. 音源链路（bridge/）

- 搜索：网易云**移动端接口** `/api/v1/search/get`（免登录、免 PC 端风控）
- 取流：网易云取流被风控时，自动回退到 B 站同名音频
- 转码：ffmpeg 把 AAC/m4s 转成标准 MP3（小度能播）
- 输出：公网 HTTPS URL（经 Cloudflare Tunnel）

### 2. 语音链路（voice_bridge.py）

- 每 3 秒轮询 `xiaodu_list_device_history`，找 `trigger=voice` 的最新记录
- 匹配触发前缀「本地」+ 歌名
- 调本地服务搜歌拿 URL → `push_resource_to_xiaodu` 推给小度
- 用 `xiaodu_speak` 让小度说「正在为你播放XXX」

### 3. 触发词

当前触发词：**「本地，歌名」**（如「本地，孤勇者」）

设计原则：前缀不含"播放/放/听/音乐"等敏感词，避免触发小度原生意图（否则小度会追问"想听什么"或去自己曲库播）。

## 五、使用

### 日常使用

对小度说：
```
小度小度，本地，孤勇者
```

### 启动 / 停止

- **开机自启**：已配置计划任务 `XiaoduMusicService`，开机自动运行
- **手动启动**：双击 `start-service.bat`
- **停止**：任务管理器结束 python.exe 和 cloudflared.exe

### 查看状态

```
http://127.0.0.1:8765/api/health     服务健康
http://127.0.0.1:8765/api/source     音源状态
```

日志：
```
logs\voice_bridge.log   语音桥日志（看触发情况）
logs\server.err         主服务日志
logs\current_url.txt    当前隧道域名
```

## 六、重要维护事项

### 1. access_token 有效期 30 天

`mcp_token.json` 里的 token 30 天过期。过期后重新授权：

```powershell
cd C:\Users\Administrator\projects\xiaodu-musicfree
python mcp_auth.py --appkey "AppKey" --secret "SecretKey"
```

（AppKey/SecretKey 在 [DuerOS MCP 控制台](https://dueros.baidu.com/dbp/mcp/console) 的应用详情页）

### 2. 隧道域名漂移

Cloudflare quick tunnel 每次重启换域名。守护进程会自动同步，但：
- 如果域名变了，`mcp_call.py` 里推的音频 URL 要保证用**当前域名**（守护进程自动处理）
- 长期稳定建议用命名隧道（需自有域名）

### 3. 设备标识

设备标识（CUID / CLIENT_ID / AppKey）从本地 `device.json` 读取（该文件已被 `.gitignore` 排除，不提交）。换设备或换应用时，重新用 `python mcp_call.py list_devices` 获取，并更新本地 `device.json`。

## 七、已知限制

1. **同音字错误**：小度语音识别可能把"孤勇者"听成"雇佣者"，需要优化（未实现同音字纠错）
2. **小度原生广告**：播放时小度偶尔会插一句"扫码看APP放免费音乐"，无法通过 MCP 关闭
3. **token 30天过期**：需要定期重新授权
4. **隧道域名漂移**：quick tunnel 不稳定
5. **勿扰模式**：设备若开启勿扰，可能影响播放，需在小度设置里检查

## 八、版权声明

音源来自网易云搜索 + B站音频，属于逆向接口使用。**仅限个人学习研究，请勿传播或商用。**
