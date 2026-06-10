# mini_bot.py 改动记录

需要应用改动的文件：`yy-cef-lx-bot/mini_bot.py`

---

## 改动 1：HELP_TEXT（替换已有常量）

**位置**：常量区（~HELP_TEXT = 那行）

**旧值**：
```python
HELP_TEXT = 'YY音乐机器人菜单\n当前歌曲名|发送:1\n暂停／继续|发送:2\n上一首|发送:4\n播放下一首|发送:5\n静音／取消静音|发送:6\n设置音量|发送：-10、+10、设置音量20\n点歌|发送：点歌歌名-歌手\n播放歌单|发送：播放歌单 tx/歌单ID\n帮助|发送：0、帮助、菜单'
```

**新值**：
```python
HELP_TEXT = """【播放控制】
1=当前歌曲  2=暂停/继续  4=上一首  5/切歌=下一首  6=静音
音量: -10 / +10 / 设置音量20
【点歌/歌单】
点歌: 点歌 歌名-歌手
播放: 播放歌单 tx/歌单ID
切换: 切换歌单 热歌榜
【查询】
当前歌单 / 歌单列表 / 缓存状态
0 / 帮助 / 菜单 = 本帮助"""
```

---

## 改动 2：import 区末尾添加 audio_cache 条件导入

**位置**：所有 import 语句最后

**添加代码**：
```python
try:
    from audio_cache import (
        CacheScheduler, PRESET_PLAYLISTS, PRESET_NAMES,
        QQ_LEADERBOARDS, QQ_LEADERBOARDS_NORM,
        fetch_qq_leaderboard, read_playback_state,
        read_all_playlists, switch_to_local_playlist,
    )
    HAS_AUDIO_CACHE = True
except ImportError:
    CacheScheduler = None
    PRESET_PLAYLISTS = {}
    PRESET_NAMES = {}
    QQ_LEADERBOARDS = {}
    QQ_LEADERBOARDS_NORM = {}
    fetch_qq_leaderboard = None
    read_playback_state = lambda: {}
    read_all_playlists = lambda: []
    switch_to_local_playlist = None
    HAS_AUDIO_CACHE = False
```

---

## 改动 3：新增 _switch_board_playlist() 函数

**位置**：放在 `play_next_request` / `update_song_queue` 附近，或任意公共函数区

**添加代码**：
```python
def _switch_board_playlist(source: str, source_list_id: str, display_name: str) -> str:
    """切换 Lx Music 内置 board__ 歌单（通过修改 data.json）。"""
    if switch_to_local_playlist is None:
        return "音频缓存模块不可用"
    try:
        local_pls = read_all_playlists()
    except Exception as exc:
        return f"读取本地歌单失败: {exc}"
    for pl in local_pls:
        if pl.get("source") == source and pl.get("sourceListId") == source_list_id:
            try:
                info = switch_to_local_playlist(pl["id"])
                time.sleep(0.5)
                LxMusicApi.play()
                return f"已切换歌单：{info.get('name', display_name)}"
            except Exception as exc:
                return f"切换歌单失败: {exc}"
    return f"歌单「{display_name}」的本地数据未找到，请先在 Lx Music 中添加该歌单。"
```

---

## 改动 4：新增 _play_qq_leaderboard() 函数

**位置**：紧跟在 `_switch_board_playlist` 之后

**添加代码**：
```python
def _play_qq_leaderboard(queue: SongQueue, display_name: str, topid: int, row: dict) -> str:
    """通过 QQ 音乐 API 拉取排行榜并加入播放队列。"""
    if not HAS_AUDIO_CACHE or fetch_qq_leaderboard is None:
        return "音频缓存模块不可用"
    try:
        songs = fetch_qq_leaderboard(topid)
    except Exception as exc:
        return f"获取「{display_name}」失败: {exc}"
    if not songs:
        return f"「{display_name}」没有歌曲数据"
    queue.items.clear()
    queue.clear_current()
    queue.history.clear()
    for song in songs:
        keyword = f"{song['songName']} - {song['singer']}"
        queue.enqueue(keyword, row)
    request = queue.pop_next()
    if request:
        result = start_song_request(request)
        return f"〖已切换至「{display_name}」〗共 {len(songs)} 首，{result}"
    return f"已加载「{display_name}」，共 {len(songs)} 首"
```

---

## 改动 5：handle_command() 末尾添加新命令

**位置**：`handle_command` 函数末尾，`导入歌单` 判断之后、`return (None, False)` 之前

**添加代码**：
```python
    # ==== 以下为新增功能 ====

    # 切换歌单（支持 QQ 排行榜、预置歌单、本地歌单）
    if HAS_AUDIO_CACHE:
        switch_match = re.fullmatch(r"切换歌单\s*[：:]?\s*(.+)", content)
        if switch_match:
            name = switch_match.group(1).strip()
            key = name.lower().replace(" ", "")
            # QQ 音乐排行榜优先（直接从 API 拉取实时榜单）
            if key in QQ_LEADERBOARDS_NORM:
                display_name, topid = QQ_LEADERBOARDS_NORM[key]
                return _play_qq_leaderboard(queue, display_name, topid, row), True
            # 预置歌单
            if key in PRESET_NAMES:
                source, list_id = PRESET_NAMES[key]
                if "board__" in list_id:
                    return _switch_board_playlist(source, list_id, name), True
                try:
                    return LxMusicApi.play_songlist(f"{source}/{list_id}"), True
                except LxMusicApi.LxMusicError as exc:
                    return str(exc), True
            # 直接输入 source/listId 格式
            if "/" in name:
                try:
                    return LxMusicApi.play_songlist(name), True
                except LxMusicApi.LxMusicError as exc:
                    return str(exc), True
            # 模糊匹配本地歌单
            try:
                local_pls = read_all_playlists()
                for pl in local_pls:
                    pl_name = pl.get("name", "")
                    if name.lower() in pl_name.lower():
                        pl_source = pl.get("source", "")
                        pl_list_id = pl.get("sourceListId", "")
                        if "board__" in pl_list_id:
                            return _switch_board_playlist(pl_source, pl_list_id, pl_name), True
                        try:
                            return LxMusicApi.play_songlist(f"{pl_source}/{pl_list_id}"), True
                        except LxMusicApi.LxMusicError as exc:
                            return str(exc), True
            except Exception:
                pass
            # 未找到
            available = "、".join(list(PRESET_PLAYLISTS.keys()))
            qq_names = "、".join(list(QQ_LEADERBOARDS)[:8])
            return f"未找到歌单「{name}」。可用预置: {available}，QQ 排行榜: {qq_names}...", True

    # 歌单列表
    if content in {"歌单列表", "可用歌单"} and HAS_AUDIO_CACHE:
        lines = []
        hot = ["热歌榜", "抖音热歌榜", "新歌榜", "飙升榜", "流行指数榜"]
        hot_valid = [n for n in hot if n in QQ_LEADERBOARDS]
        if hot_valid:
            lines.append("【QQ热门】" + " / ".join(hot_valid))
        genre = [
            "内地榜", "欧美榜", "说唱榜", "韩国榜", "日本榜",
            "香港地区榜", "台湾地区榜", "影视金曲榜", "DJ舞曲榜",
            "国风热歌榜", "综艺新歌榜", "动漫音乐榜", "游戏音乐榜",
            "网络歌曲榜", "喜力电音榜", "校园音乐人排行榜",
            "腾讯音乐人原创榜", "听歌识曲榜", "K歌金曲榜", "有声榜",
        ]
        genre_valid = [n for n in genre if n in QQ_LEADERBOARDS]
        if genre_valid:
            lines.append("【QQ分类】" + " / ".join(genre_valid))
        if PRESET_PLAYLISTS:
            lines.append("【预置】" + " / ".join(PRESET_PLAYLISTS.keys()))
        try:
            local_pls = read_all_playlists()
            if local_pls:
                local_names = [pl["name"] for pl in local_pls[:6]]
                line = "【本地】" + " / ".join(local_names)
                if len(local_pls) > 6:
                    line += f" ...共{len(local_pls)}个"
                lines.append(line)
        except Exception:
            pass
        if not lines:
            lines.append("暂无可用歌单。")
        return "\n".join(lines), True

    # 当前歌单信息
    if content == "当前歌单" and HAS_AUDIO_CACHE:
        try:
            state = read_playback_state()
            pl_name = state.get("playlist_name") or "未知"
            cur = state.get("current_song") or {}
            if cur:
                song_text = f"{cur.get('singer', '')} - {cur.get('name', '')}"
            else:
                song_text = "无"
            return f"当前歌单：{pl_name}\n当前歌曲：{song_text}\n位置：{state.get('index', 0)}", True
        except Exception as exc:
            return f"读取状态失败: {exc}", True

    # 缓存状态
    if content == "缓存状态" and HAS_AUDIO_CACHE:
        try:
            from audio_cache import CACHE_DIR
            if CACHE_DIR.exists():
                files = list(CACHE_DIR.iterdir())
                file_count = len(files)
                total_size = sum(f.stat().st_size for f in files) / (1024 * 1024)
                return f"缓存状态：{file_count} 个文件 ({total_size:.1f} MB)", True
            return "缓存目录不存在", True
        except Exception as exc:
            return f"缓存状态读取失败: {exc}", True

    return None, False
```

---

## 改动 6：pick_channel_page() 多候选修复

**位置**：函数 `pick_channel_page`

**替换为**：
```python
def pick_channel_page(args, strict_channel: bool = False,
                      baseline: dict | None = None) -> dict:
    """从扫描结果中选出合适的频道页面。"""
    candidates = []
    for page in scan_pages():
        if "yy.com" not in str(page.get("url", "")).lower():
            continue
        enrich_page(page)
        if not page_matches_args(page, args, strict_channel):
            continue
        state = read_channel_state(page)
        if state and (not baseline or page_state_changed(page, state, baseline)):
            candidates.append((page, state))
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        # 优先选可发公屏消息的页面，其次选有频道 ID 的页面
        candidates.sort(key=lambda ps: (
            not bool(ps[1].get("hasChannelMessage")),
            not bool(ps[1].get("channelInfo", {}).get("sid")),
            len(ps[1].get("CurrentChannelSessId", "") or ""),
        ))
        return candidates[0][0]
    for page in scan_pages():
        if "yy.com" in str(page.get("url", "")).lower():
            enrich_page(page)
            return page
    raise RuntimeError("未找到 YY CEF 频道页面。")
```

---

## 注意事项

1. `audio_cache.py` 必须是独立的完整文件（QQ_LEADERBOARDS 等常量都在里面）
2. `run_bot` 函数中的 `print(f"已连接 YY CEF 页面:...")` 必须在 `api = YYCefApi(args)` 之后
3. `LxMusicApi._split_song_keyword` 和 `LxMusicApi.LxMusicError` 需要 `lx_music_api.py` 中已定义
4. 如果昨天代码的 `audio_cache.py` 没有 `QQ_LEADERBOARDS`、`QQ_LEADERBOARDS_NORM`、`read_playback_state`、`read_all_playlists` 等，需要先更新 `audio_cache.py`
