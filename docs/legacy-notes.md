# 历史路线与保留说明

## 当前主线

当前推荐实现只有一条：

- `yy-cef-lx-bot/mini_bot.py`

技术组合：

- YY CEF 内部 API
- Lx Music Open API / Scheme
- 可选 TTS

## 旧 UI 自动化路线

以下文件已经不再作为主入口：

- `legacy_uia/MyYYBot.py`
- `legacy_uia/GetYYChatRecords.py`
- `legacy_uia/SendYYMessages.py`

它们被保留的原因是：

- 作为历史 fallback 参考
- 在极端情况下可用于回顾早期 UI 自动化方案

但它们不是当前推荐方案，也不应再作为主 README 的默认启动方式。

## QQMusic 路线

以下文件属于旧 QQMusic 路线：

- `QQMusicApi.py`
- `Login.py`
- `MusicPlayer.py`
- `CredentialData.json`

这一路线已经退役，原因包括：

- 当前播放器主线已经切换到 Lx Music Desktop
- QQMusic 登录与凭据链路更重
- 与当前项目目标不再一致

## 历史试验目录

以下目录保留在仓库中，但不是当前主线：

- `yy-sdk-probe/`
- `yy-ocr-benchmark/`
- `yy-cef-probe/`

含义：

- `yy-sdk-probe/`：验证过 SDK 方向
- `yy-ocr-benchmark/`：验证过 OCR 方向
- `yy-cef-probe/`：早期 CEF 探测验证

这些目录保留是为了后续回顾和排障，不代表它们仍然参与当前主流程。

## 已移除的频道切换验证脚本

`yy-cef-lx-bot/channel_switch_test.py` 曾用于验证页面内部接口 `yy.channel.joinChannel(sid, asid, ssid, entrance)`。这条路线证明了 YY CEF 页面内存在频道切换能力，但需要同时掌握 `sid/asid/ssid/entrance` 等参数，最终没有作为主线方案。

`yy-cef-lx-bot/yy_scheme_switch_test.py` 曾用于验证 `yy://pd-[sid=<asid>]` 协议跳转。验证结论是：只传 `sid=<asid>` 即可唤起 YY 并切换频道；切换后需要重新扫描 DevTools target，等待新的原生频道页恢复，再用 `YY.Channel.ChannelMessage.getCacheMessage()` 和 `yy.chat.sendPublicMessage()` 继续接管公屏。

这两个脚本的有效逻辑已经并入 `yy-cef-lx-bot/mini_bot.py`，所以代码文件不再保留。当前只保留 `yy-cef-lx-bot/cef_probe.py` 作为 YY CEF 排障和行为探测工具。
