# YYBot

当前主线是 **YY CEF + Lx Music Desktop** 方案：

- 通过 YY 客户端 CEF 页面内部 API 读取公屏消息
- 通过 YY 客户端 CEF 页面内部 API 发送公屏反馈
- 通过 Lx Music Desktop Open API / Scheme 控制播放器
- 通过 `yy://pd-[sid=...]` 切换频道，并在切换后重新接管新的频道页继续监听

## 当前主入口

在项目根目录运行：

```bash
python yy-cef-lx-bot/mini_bot.py
```

更细的启动说明、命令说明和调试说明见：

- `yy-cef-lx-bot/README.md`
- `docs/architecture.md`
- `docs/legacy-notes.md`

## 技术路线

### 1. YY 公屏读写

当前方案不是浏览器公开 SDK，也不是 OCR，也不是 UI 自动化主链路。

实际使用的是：

- Chrome DevTools Protocol 连接 YY 内部 CEF 页面
- `YY.Channel.ChannelMessage.getCacheMessage()` 读取公屏缓存
- `yy.chat.sendPublicMessage()` 发送公屏反馈

### 2. Lx Music 控制

播放器控制分成两类：

- **Open API**：状态、暂停/继续、上一首、下一首、音量
- **Scheme**：点歌、播放歌单、打开歌单等

### 3. 频道切换

当前主方案是：

- `yy://pd-[sid=<asid>]`

切换后会重新扫描频道页并重绑 DevTools websocket，继续监听新频道公屏。

## 环境要求

- Python 3.10 - 3.13
- Windows 桌面环境
- YY 客户端
- Lx Music Desktop，并在设置中开启开放 API 服务
- Python 依赖：

```bash
pip install websocket-client requests edge-tts pygame
```

如果你只使用纯文本反馈而不需要 TTS，`edge-tts` 和 `pygame` 不是硬性必需。

## 当前功能

- 公屏命令监听
- 公屏反馈发送
- 当前歌曲查询
- 暂停 / 继续
- 上一首 / 下一首
- 绝对音量设置
- 相对音量增减
- 点歌
- 歌单打开 / 播放
- 频道切换
- 切换后持续监听新频道
- 可选 TTS 反馈

## 历史内容说明

仓库中仍保留部分历史试验与旧路线内容，方便后续排障或回顾：

- `yy-sdk-probe/`
- `yy-ocr-benchmark/`
- `yy-cef-probe/`
- `legacy_uia/`

这些目录都**不是当前主线实现**。

其中：

- `legacy_uia/` 是旧 UI 自动化 fallback 方案
- 其余 probe / benchmark 目录是历史验证材料

## 运行建议

本项目适合运行在 **Windows 可交互桌面环境** 中，不适合普通无界面 Linux 服务器，也不建议把完整机器人直接作为 Docker 化主部署方案。

原因：

- YY 是 Windows 桌面应用
- Lx Music Desktop 也是桌面应用
- 频道切换和 CEF 页面接管依赖本机桌面客户端状态

## License

本项目基于 MIT License 许可证发行。
