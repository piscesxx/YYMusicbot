# YY CEF 能力探测记录

本文记录当前 YY 9.55 CEF 页面中已验证或可继续验证的能力，方便后续维护机器人功能时查阅。

## 当前页面

已验证页面：

```text
url=https://base.c.yy.com/
title=47797166-听歌练枪
session=basechn-...-15879723-47797166
window.__yycefchannelinner__.isJoinedChannel=true
window.__yycefchannelinner__.uid=15879723
```

当前 YY 9.55 页面没有旧版 JS API：

```text
window.hdyyapv2 不存在
window.MFApiImpl_yyapi_pcV2 不存在
YY.Channel.ChannelMessage 不存在
```

因此机器人需要优先兼容新版 DOM fallback。

## 稳定可用能力

### 公屏读取

新版页面可通过 DOM 读取公屏：

```text
#public_message_list
#public_message_list [role="listitem"]
.publicContent[custom-userid][custom-fulltext]
.nick span[style*="unicode-bidi"]
```

可读取字段：

- `data-msgid`：消息 ID
- `custom-userid`：发送用户 UID
- `custom-fulltext`：用户消息正文
- 昵称：`.nick span[style*="unicode-bidi"]`
- 系统通知：无 `.publicContent` 的 listitem，文本来自 `innerText`

典型用户消息结构：

```text
<div data-msgid="5" role="listitem">
  ...
  <div class="publicContent" custom-userid="15879723" custom-fulltext="bot测试">
    ...
  </div>
</div>
```

### 公屏发送

新版页面可通过输入框和发送按钮发送公屏消息：

```text
.content_input[contenteditable="true"]
#sendButton
```

发送流程：

1. 聚焦 `.content_input[contenteditable="true"]`
2. 设置 `textContent`
3. 派发 `InputEvent('input')`
4. 点击 `#sendButton`

已验证可发送 `bot测试` 并被公屏读取。

### 当前频道状态

可读：

```text
document.title
window.CurrentChannelSessId
window.__yycefchannelinner__.uid
window.__yycefchannelinner__.isJoinedChannel
```

用途：

- 判断是否进入频道
- 识别当前登录 UID
- 识别频道号
- 频道切换后重新绑定

### 频道树

频道树 DOM：

```text
#channel_tree_id
#channel_tree_id [id^="treeitem_"]
#channel_<id>_2
#channelName_<id>
```

已读到示例：

```text
treeitem_47797166   听歌练枪 (1)
treeitem_15879723   罗密欧与猪过夜 cpdd
treeitem_2764005252 选人厅
treeitem_2787207142 情缘广场
```

可实现：

- 列出当前频道树
- 根据子频道名定位节点
- 点击频道树节点尝试切换子频道
- 判断用户是否在频道树中

## 可继续验证能力

### 用户进入欢迎

公屏系统通知格式：

```text
通知： [昵称] 进入 [频道名] 频道。(HH:MM:SS)
```

可用正则：

```regex
通知：\s*\[(.+?)\]\s*进入\s*\[(.+?)\]\s*频道
```

适合实现：

- 用户进入频道后 TTS 欢迎
- 进场日志
- 特定用户进入提醒

### 页面按钮自动化

已枚举到的按钮包括：

- 收藏
- 复制飞机票
- 邀请好友一起玩
- 举报
- 开播
- 首页
- 游戏大厅
- 隐藏到主窗口
- 最小化
- 最大化
- 退出频道
- 管理员
- 发言记录
- 定位自己
- 收起所有频道
- 找人
- 模式选择
- 放麦
- 开麦
- 抢麦
- 选择表情、字体和颜色
- 私聊
- 赠送鲜花
- 扬声器
- 麦克风
- 调音台
- 按住 F2 说话
- 播放伴奏
- 录音
- 频道模板
- 组件广场

适合先验证低风险按钮：

- 定位自己
- 收起/展开频道树
- 找人
- 发言记录

高风险按钮不建议默认自动化：

- 举报
- 开播
- 赠送鲜花
- 商城/游戏商城
- 退出频道

### 窗口控制

`CefWindow` 暴露了窗口控制方法：

```text
minimize
maximize
restore
hide
show
focusBrowser
getGeometry
move
resize
setTopMost
closeWindow
leaveChannel
```

可实现但需谨慎：

- 最小化/还原频道窗口
- 激活频道窗口
- 移动/调整窗口大小

### YY 内置音频播放器

`YYAudioPlayer` 暴露了：

```text
addPlayList
insertPlayList
clearList
play
pause
resume
stop
playNext
playPre
switchListIndex
getPlayId
getPlayTime
getTotalTime
getVolume
setVolume
getMute
setMute
getPlayList
getPlaying
checkDownload
```

已验证可用的播放流程：

```javascript
const song = {
  id: 111029,
  title: '一万个舍不得',
  singer: '庄心妍',
  filePath: 'C:/Users/sobey/AppData/Roaming/duowan/yy/business/yymusiclib/b813bff4037d9fb45d5edeeced4797bf/music.mp3',
  fileUrl: 'http://yybgmusic.yystatic.com/bgmusic/68d0bac849d0dd31525c3f3c.zip',
  fileMd5: '8e94ce470321975e5196d7ed63db27a9',
  fileSize: 8301498,
  totalTime: 0,
  accompaniment: 1,
};

await YYAudioPlayer.clearList();
await YYAudioPlayer.addPlayList(song);
await YYAudioPlayer.play(song.id);
```

注意事项：

- `addPlayList(songObject)` 可正确加入歌曲。
- `addPlayList([songObject])`、`addPlayList(JSON.stringify(...))` 会加入空字段歌曲，不要使用。
- `play()` 和 `switchListIndex(0)` 不会直接播放刚加入的歌；需要调用 `play(song.id)`。
- `insertPlayList(...)` 在当前测试中未成功加入歌曲。
- `YYAudioPlayer.webInit(logic)` 是真实播放初始化关键步骤；未初始化时可能出现 `playing=true` 但 `playTime=0` 的假播放状态。
- 已验证 YY 官方伴奏 zip 对象在 `webInit` 后可真实播放：`playTime` 会推进，`sig_playTime` 会持续触发。
- 外部网络音源直链目前只验证到可加入播放列表并返回 `playing=true`，但 `playTime` 不推进，不能视作真实播放成功。
- 把外部 MP3 打包成含 `music.mp3` 的 zip 并通过本地 HTTP 提供时，YY 能下载并解压到 `yymusiclib`，但仍然 `playTime=0`；说明问题不只是下载格式。
- 自定义本地 `filePath` 在测试中容易被 YY 归一化为空字符串；直接传本地 MP3 路径还可能导致 CEF DevTools 连接被重置，不建议作为主方案。

这是 YY 内置音频能力，不是当前主线使用的 Lx Music。外部音源接入仍在验证阶段，不应直接替换主线。

## 建议后续功能优先级

1. 管理员权限：基于 `custom-userid` 限制高风险命令。
2. 点歌队列管理：队列查询、删除我的歌、清空队列。
3. 子频道切换：读取频道树并点击目标子频道。
4. 进场欢迎：根据系统通知触发 TTS。
5. 关键词自动回复：基于公屏文本触发。
6. 找人/在线检测：基于频道树和搜索框。

## 当前代码接入点

主文件：`mini_bot.py`

- 页面筛选：`page_has_channel_message()`
- 公屏读取：`YYCefApi.read_messages()`
- 公屏发送：`YYCefApi.send_message()`
- 消息去重：`message_id_of()` / `seen`
- 主循环：`run_bot()`
- TTS：`speak_async()` -> `lx_bot.tts.Read()`

新增功能应优先复用这些入口，避免直接散落新的 DevTools 调用。