# YYBot 架构说明

## 当前主线

当前最终实现以 `yy-cef-lx-bot/mini_bot.py` 为中心。

主链路：

```text
YY 客户端频道页
  -> DevTools websocket
  -> 读取公屏缓存
  -> Python 解析命令
  -> 调用 Lx Music Open API / Scheme
  -> 发送公屏反馈
```

## 核心文件

- `yy-cef-lx-bot/mini_bot.py`
- `LxMusicApi.py`
- `Read.py`
- `yy-cef-lx-bot/cef_probe.py`

## 1. YY 公屏读取

机器人会扫描 YY 暴露的 DevTools 页面，找到可用频道页，然后连接其 websocket。

核心实现：

- 扫描页面：`scan_pages()`
- 选择频道页：`find_channel_page()`
- 读取消息：`YYCefApi.read_messages()`

实际读取依赖：

- `YY.Channel.ChannelMessage.getCacheMessage()`

说明：

- 当前是轮询缓存，不是 push 事件
- 启动时会先读取当前缓存并建立 `seen`，避免把旧消息全部当成新消息处理

## 2. YY 公屏发送

反馈消息通过频道页内的：

- `yy.chat.sendPublicMessage(text)`

因此主线已经不再依赖旧 UI 自动化输入框发消息。

## 3. Lx Music 控制

`LxMusicApi.py` 分成两类控制方式：

### Open API

用于：

- `/status`
- `/play`
- `/pause`
- `/skip-next`
- `/skip-prev`
- `/volume`
- `/mute`

典型命令：

- 当前歌曲
- 暂停 / 继续
- 上一首 / 下一首
- 设置音量
- 音量增减
- 静音 / 取消静音

当前官方 Open API 文档未提供随机播放、顺序播放、单曲循环等播放模式切换接口，所以命令 `7` 只返回提示，不伪造不可验证的控制逻辑。

### Scheme

用于：

- `lxmusic://music/searchPlay/...`
- `lxmusic://songlist/play/...`
- `lxmusic://songlist/open/...`

典型命令：

- 点歌
- 播放歌单
- 导入 / 打开歌单

## 4. 音量逻辑

相对音量命令（如 `+10`、`-10`）不能依赖默认值。

当前逻辑是：

1. 先尝试从普通 `/status` 中提取 volume
2. 如果普通状态里没有 volume，再额外请求：
   - `/status?filter=status,volume,mute`
3. 读取不到真实音量时，不再盲目从 50 起算

这样可以避免播放器先启动、机器人后启动时，音量突然跳大。

## 5. 点歌队列

`mini_bot.py` 内维护 `SongQueue`。

行为：

- 第一首立即播放
- 后续歌曲入队
- 当前歌曲结束后自动播放下一首

结束判断依赖 Lx 当前播放状态和歌曲签名变化。

## 6. 频道切换方案

当前主方案是：

- `yy://pd-[sid=<asid>]`

原因：

- 比直接从页面里调用 `joinChannel(...)` 更稳定
- 已经通过专项测试验证可以切到目标频道

切换流程：

1. 收到 `切换频道123456`
2. 发送“正在切换频道 ...”
3. 打开 `yy://pd-[sid=...]`
4. 等待新的频道页出现或当前页状态变化
5. 重新绑定新的 CEF 页面
6. 重建 `seen`
7. 继续监听新频道公屏

## 7. 切换后的重绑

切频道后旧 websocket 可能会断开，或页面短暂消失。

当前机器人会：

- 检测 `read_messages()` / `send_message()` 失败
- 或检测频道页状态变化
- 重新扫描频道页
- 重绑新的 `DevToolsClient`
- 继续主循环

这一步是当前主线相对早期验证脚本的关键增强。

## 8. TTS 反馈

如果启动时传 `--tts`，反馈消息会通过 `Read.py` 异步播报。

依赖：

- `edge_tts`
- `pygame`

## 9. 为什么不是其他路线

### 不是 SDK 路线

历史上做过 `yy-sdk-probe/`，但当前主线没有走官方 SDK 页面方案。

### 不是 OCR 路线

`yy-ocr-benchmark/` 只是历史实验，最终没有采用 OCR 识别公屏。

### 不是旧 UI 自动化主线

旧方案依赖 `pywinauto` 控制窗口和输入框，当前主线已经由 CEF 内部 API 取代。

旧方案只作为 fallback 历史保留在 `legacy_uia/`。
