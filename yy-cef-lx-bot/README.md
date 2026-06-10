# YY CEF + Lx Music 机器人

这个文件夹现在就是一个**可独立拷贝、可独立部署**的完整项目目录。

拷贝到新环境后，只需要：

1. 安装 Python 依赖
2. 启动 YY 客户端
3. 启动 Lx Music Desktop 并开启 Open API
4. 运行 `mini_bot.py`

即可直接使用。

## 目录说明

```text
yy-cef-lx-bot/
├─ mini_bot.py                 主入口
├─ cef_probe.py                CEF 探测 / 排障脚本
├─ requirements.txt            Python 依赖
├─ lx_bot/
│  ├─ __init__.py
│  ├─ lx_music_api.py          Lx Music 控制逻辑
│  └─ tts.py                   可选 TTS 播报逻辑
```

## 环境要求

- Windows 10 / 11
- Python 3.10 - 3.13
- YY 客户端
- Lx Music Desktop
- Lx Music Desktop 中已开启 Open API

## 安装依赖

在当前目录执行：

```bash
pip install -r requirements.txt
```

说明：

- `websocket-client`：连接 YY CEF DevTools
- `requests`：调用 Lx Music Open API
- `edge-tts`、`pygame`：用于本地语音播报，包括 `读...` 命令和 `--tts` 反馈播报

如果你完全不需要 TTS，理论上只装：

```bash
pip install websocket-client requests
```

也能运行主线机器人。

## 启动方式

在当前目录运行：

```bash
python mini_bot.py
```

### 开启语音播报

```bash
python mini_bot.py --tts
```

### 自己账号发消息也处理

```bash
python mini_bot.py --process-self
```

### 多账号时指定 UID

```bash
python mini_bot.py --uid 15879723
```

### 多频道时指定起始频道

```bash
python mini_bot.py --uid 15879723 --channel 47797166
```

## 支持命令

```text
0 / 帮助 / 菜单      发送帮助菜单
1                    当前歌曲
2                    暂停/继续
4 / 上一首           有点歌历史时回放上一首点歌，否则播放器上一首
播放器上一首 / 歌单上一首  强制调用播放器上一首
5 / 下一首 / 切歌    下一首
6                    静音/取消静音
7                    切播放模式（当前 Open API 暂无接口）
+10 / 音量+10        音量增加
-10 / 音量-10        音量减少
设置音量20           设置音量到 20
点歌晴天             通过 Lx Music 点歌
点歌晴天-周杰伦      通过 Lx Music 点歌
读厉害               本地 TTS 播报“厉害”（不需要 --tts）
播放歌单 tx/123      播放歌单
导入歌单 tx/123      打开歌单
切换频道391936       切换 YY 频道
```

## 当前实现方案

### YY 公屏读写

当前主线使用 YY 客户端内部 CEF 页面能力：

- `YY.Channel.ChannelMessage.getCacheMessage()` 读取公屏缓存
- `yy.chat.sendPublicMessage()` 发送反馈

### Lx Music 控制

当前项目内置了：

- `lx_bot/lx_music_api.py`

它负责：

- 当前歌曲
- 暂停 / 继续
- 上一首 / 下一首
- 音量设置
- 音量增减
- 点歌
- 歌单相关操作

### 频道切换

当前主方案是：

- `yy://pd-[sid=<asid>]`

机器人收到：

```text
切换频道391936
```

后会：

1. 发送“正在切换频道 ...”
2. 打开协议
3. 等待新频道页恢复
4. 重绑新的 CEF 页面
5. 在新频道继续监听公屏

### TTS

如果开启 `--tts`，反馈消息会通过：

- `lx_bot/tts.py`

转语音并在本机播放。

## 部署到新环境时最重要的事

除了 Python 依赖外，你还必须保证：

1. YY 客户端能正常登录并进入频道
2. Lx Music Desktop 已启动
3. Lx Music Open API 已开启
4. 机器是 Windows 可交互桌面环境

这个项目不适合普通 Linux 服务器或 Docker 作为完整运行环境。

## 项目基本完成的验证标准

把整个 `yy-cef-lx-bot/` 文件夹拷到新环境后，做到以下几点，就表示该目录已经可以独立交付：

1. `python mini_bot.py --help` 能正常运行
2. `python mini_bot.py` 能正常启动
3. `帮助` 命令能收到反馈
4. `1` / `2` / `+10` / `设置音量20` 正常工作
5. `点歌...` 正常工作
6. `切换频道...` 后仍能继续监听新频道
7. `python mini_bot.py --tts` 时本地播报正常

## 排障脚本

当前目录保留以下排障 / 验证脚本：

- `cef_probe.py`：用于排障和探测 YY 客户端行为变化，不是日常运行机器人的默认入口。
- `test_yy_audio_source.py`：用于单独验证“JS 音源脚本 -> YY 内置播放器”链路，可在关闭 Lx Music 后运行：

```bash
python test_yy_audio_source.py
```

也可以指定歌曲信息：

```bash
python test_yy_audio_source.py 晴天 --singer 周杰伦 --source tx --song-id 0039MnYb0qxYhV
```

历史上的频道切换验证脚本已移除，相关技术结论记录在仓库根目录的 `docs/legacy-notes.md`。
