# YY CEF Probe

这个目录用于验证 YY 客户端内嵌 CEF API 是否可以读取/发送频道公屏消息。

它不接入当前 YYBot 主流程，也不修改原有 UIA/pywinauto 方案。

## 已验证思路

YY 客户端进入频道后，会启动 `yyexternal.exe` CEF 进程，并暴露本地 DevTools 调试端口。频道页面里没有旧开放平台的：

```js
window.yy
window.IYY
window.IYYChannelChat
```

但存在新的内部对象：

```js
window.hdyyapv2
window.MFApiImpl_yyapi_pcV2
```

可通过内部模块：

```js
YY.Channel.ChannelMessage
```

执行：

```js
getCacheMessage()
sendPublicMessage(text)
```

## 依赖

需要 Python 包：

```bash
pip install websocket-client
```

当前环境通常已经有这个包。

## 使用前提

1. 打开 YY 客户端。
2. 登录账号。
3. 进入目标频道。
4. 保持频道窗口存在。

## 命令

在项目根目录运行。

### 1. 列出 CEF 页面

```bash
python yy-cef-probe/yy_cef_probe.py list
```

如果正常，应能看到类似：

```text
https://web.yy.com/pcyy_mainlogicpage/1.0.0/index.html?id=1
https://base.c.yy.com/
```

### 2. 检查内部 API 状态

```bash
python yy-cef-probe/yy_cef_probe.py status
```

重点看：

```text
hasApi: true
hasYY: true
hasChannelMessage: true
CurrentChannelSessId: basechn-...
```

### 3. 读取当前公屏缓存

```bash
python yy-cef-probe/yy_cef_probe.py read --limit 20
```

如果要看字段结构：

```bash
python yy-cef-probe/yy_cef_probe.py read --limit 5 --raw-json
```

只要缓存里有公屏消息，就会输出：

```text
[2] 昵称 uid=xxx imid=xxx: 消息内容
```

`msgType=2` 通常是用户公屏消息。

### 4. 监听新公屏消息

```bash
python yy-cef-probe/yy_cef_probe.py watch
```

默认每 0.5 秒轮询一次内部缓存。

可以改间隔：

```bash
python yy-cef-probe/yy_cef_probe.py watch --interval 0.2
```

### 5. 发送公屏消息

```bash
python yy-cef-probe/yy_cef_probe.py send "〖内部API〗发送测试"
```

这会调用：

```js
hdyyapv2._yy.chat.sendPublicMessage(text)
```

不是模拟键盘输入。

## 测试清单

1. 运行 `list`，确认能发现 `https://base.c.yy.com/`。
2. 运行 `status`，确认 `hasChannelMessage` 为 `true`。
3. 运行 `watch`。
4. 在 YY 公屏手动发：

```text
CEF监听测试123
```

5. 观察终端是否打印该消息。
6. 运行：

```bash
python yy-cef-probe/yy_cef_probe.py send "〖内部API〗发送测试"
```

7. 观察 YY 公屏是否出现该消息。

## 当前限制

- 目前用 `getCacheMessage()` 轮询缓存，不是实时 push 事件。
- 终端中文如果乱码，通常是控制台编码问题，不一定代表 YY 内部数据错误。
- 自动扫描端口范围是 `30000-45000`，如果 YY 使用了其他调试端口，需要后续扩展。
- 这个探针只做验证，不做音乐播放、不做命令解析、不做 TTS。

## 后续接入方向

如果验证稳定，可以再把这一层封装成正式模块：

```text
YYCefApi.py
```

替代原来的 UIA 公屏扫描与剪贴板发送，但本目录当前不会改动主流程。
