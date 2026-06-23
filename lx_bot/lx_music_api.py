import os
import time
import webbrowser
from urllib.parse import quote

import requests


BASE_URL = os.environ.get("LX_MUSIC_API_BASE_URL", "http://127.0.0.1:23330").rstrip("/")
TIMEOUT = 3
SUPPORTED_SOURCES = {"kw", "kg", "tx", "wy", "mg", "myList"}
VOLUME_KEYS = {"volume", "volumeSize", "volume_size", "volumePercent", "volume_percent"}
MUTE_KEYS = {"mute", "muted", "isMute", "isMuted", "is_mute", "is_muted"}
DEFAULT_VOLUME = int(os.environ.get("LX_MUSIC_DEFAULT_VOLUME", "50"))
_last_known_volume = None
_status_cache = {"data": None, "time": 0.0}
_STATUS_CACHE_TTL = 0.4


class LxMusicError(Exception):
    pass


def _request(path, params=None):
    try:
        response = requests.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LxMusicError("🎵 没有检测到 Lx Music 呢，先打开播放器并开启 API 吧～") from exc

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError:
        return {}


def _open_scheme(url):
    if not webbrowser.open(url):
        raise LxMusicError("🎵 唤不起 Lx Music 呢，确认已安装并注册了 lxmusic 协议哦～")


def _pick(data, *keys):
    for key in keys:
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


def _player_data(status):
    data = _pick(status, "data", "player")
    if isinstance(data, dict):
        return data
    return status if isinstance(status, dict) else {}


def check_status():
    try:
        get_status()
        return True
    except LxMusicError:
        return False


def get_status(filter_fields=None, force=False):
    global _status_cache
    now = time.monotonic()
    if not force and not filter_fields and now - _status_cache["time"] < _STATUS_CACHE_TTL:
        return _status_cache["data"]
    params = None
    if filter_fields:
        params = {"filter": ",".join(filter_fields)}
    data = _request("/status", params=params)
    if not filter_fields:
        _status_cache = {"data": data, "time": now}
    return data


def _current_song_display(status=None):
    data = _player_data(status or get_status())
    name = _pick(data, "name", "songName", "title")
    singer = _pick(data, "singer", "artist", "author")
    song_id = _pick(data, "id", "songmid", "mid", "hash")

    if not name:
        return ""

    song_text = f"{singer} - {name}" if singer else str(name)
    if song_id:
        song_text = f"({song_id}) {song_text}"
    return song_text


def get_current_song_text():
    status = _player_data(get_status())
    song_text = _current_song_display(status)
    volume = get_volume(status)

    if not song_text:
        return "🔇 现在没在放歌呢，点一首吧～"

    message = f"🎵 正在唱：{song_text}"
    if volume is not None:
        message += f"，音量 {volume} 哦"
    return message


def _normalize_volume(value):
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return None

    if 0 <= volume < 1:
        volume *= 100
    if not 0 <= volume <= 100:
        return None
    return max(0, min(100, round(volume)))


def _extract_volume_recursive(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key in VOLUME_KEYS:
                volume = _normalize_volume(value)
                if volume is not None:
                    return volume
        for value in data.values():
            volume = _extract_volume_recursive(value)
            if volume is not None:
                return volume
    elif isinstance(data, list):
        for item in data:
            volume = _extract_volume_recursive(item)
            if volume is not None:
                return volume
    return None


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes", "on", "muted"}:
            return True
        if lowered in {"false", "0", "no", "off", "unmuted"}:
            return False
    return None


def _extract_mute_recursive(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key in MUTE_KEYS:
                mute = _normalize_bool(value)
                if mute is not None:
                    return mute
        for value in data.values():
            mute = _extract_mute_recursive(value)
            if mute is not None:
                return mute
    elif isinstance(data, list):
        for item in data:
            mute = _extract_mute_recursive(item)
            if mute is not None:
                return mute
    return None


def get_volume(status=None):
    global _last_known_volume
    volume = _extract_volume_recursive(status or get_status())
    if volume is None:
        volume = _extract_volume_recursive(get_status(filter_fields=["status", "volume", "mute"]))
    if volume is not None:
        _last_known_volume = volume
    return volume


def get_mute(status=None):
    mute = _extract_mute_recursive(status or get_status())
    if mute is None:
        mute = _extract_mute_recursive(get_status(filter_fields=["status", "volume", "mute"]))
    return mute


def set_mute(mute):
    target = bool(mute)
    _request("/mute", params={"mute": "true" if target else "false"})
    return "🔇 已静音，安静一下～" if target else "🔊 取消静音，大声放吧～"


def toggle_mute():
    current = get_mute()
    if current is None:
        raise LxMusicError("😅 不知道当前静音状态呢，再试一次看看吧～")
    return set_mute(not current)


def is_playing(status=None):
    data = _player_data(status or get_status())
    value = _pick(data, "status", "playStatus", "playing", "isPlaying")

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.lower() in {"playing", "play", "running", "true", "1"}
    return False


def play():
    _request("/play")
    return "▶️ 继续播放，嗨起来～"


def pause():
    _request("/pause")
    return "⏸ 暂停一下，想听了叫我哦～"


def toggle_pause():
    if is_playing():
        return pause()
    return play()


def _song_after_switch(previous_signature):
    deadline = time.monotonic() + 3
    last_song = ""
    while time.monotonic() < deadline:
        try:
            status = _player_data(get_status(force=True))
        except LxMusicError:
            return last_song
        song_text = _current_song_display(status)
        signature = _song_signature(status)
        if song_text:
            last_song = song_text
        if song_text and signature != previous_signature:
            return song_text
        time.sleep(0.3)
    return last_song


def _song_signature(status):
    name = _pick(status, "name", "songName", "title") or ""
    singer = _pick(status, "singer", "artist", "author") or ""
    song_id = _pick(status, "id", "songmid", "mid", "hash") or ""
    return f"{song_id}|{name}|{singer}"


def _switch_song(path, scheme_url, direction):
    try:
        previous_status = _player_data(get_status())
        previous_signature = _song_signature(previous_status)
    except LxMusicError:
        previous_signature = ""

    try:
        _request(path)
    except LxMusicError:
        _open_scheme(scheme_url)

    song_text = _song_after_switch(previous_signature)
    prefix = "⏭" if "下一首" in direction else "⏮"
    if song_text:
        return f"{prefix} 已切{direction}：{song_text}"
    return f"{prefix} 已切{direction}～"


def next_song():
    return _switch_song("/skip-next", "lxmusic://player/skipNext", "下一首")


def previous_song():
    return _switch_song("/skip-prev", "lxmusic://player/skipPrev", "上一首")


def _set_volume(volume):
    global _last_known_volume
    _request("/volume", params={"volume": volume})
    _last_known_volume = volume


def set_volume(value):
    try:
        volume = int(value)
    except (TypeError, ValueError) as exc:
        raise LxMusicError("🔢 音量要输 0 到 100 之间的整数哦～") from exc

    if not 0 <= volume <= 100:
        raise LxMusicError("🔢 音量只能在 0 到 100 之间呢～")

    _set_volume(volume)
    return f"🔊 音量已设为 {volume}～"


def change_volume(delta):
    try:
        delta = int(delta)
    except (TypeError, ValueError) as exc:
        raise LxMusicError("🔢 音量增减要输整数哦～") from exc

    current = get_volume()
    if current is None:
        if _last_known_volume is not None:
            current = _last_known_volume
        else:
            raise LxMusicError("😅 不知道当前音量呢，先试试「设置音量50」这样的命令吧～")

    target = max(0, min(100, current + delta))
    _set_volume(target)

    if delta >= 0:
        return f"🔊 音量调高了 {delta}，现在 {target} 啦～"
    return f"🔉 音量调低了 {abs(delta)}，现在 {target} 啦～"


def _split_song_keyword(keyword):
    for separator in ("-", "－", "—", "–"):
        if separator in keyword:
            song_name, singer = keyword.split(separator, 1)
            return song_name.strip(), singer.strip()
    return keyword.strip(), ""


def search_play(keyword):
    keyword = keyword.strip()
    if not keyword:
        raise LxMusicError("📝 点歌内容不能为空哦，试试「点歌 歌名-歌手」吧～")

    _open_scheme(f"lxmusic://music/searchPlay/{quote(keyword)}")
    song_name, singer = _split_song_keyword(keyword)
    song_text = f"{singer} - {song_name}" if singer else song_name
    return f"🎵 正在搜索播放：{song_text}"


def search(keyword):
    keyword = keyword.strip()
    if not keyword:
        raise LxMusicError("📝 搜索内容不能为空哦～")

    _open_scheme(f"lxmusic://music/search/{quote(keyword)}")
    return f"🔍 已打开 Lx Music 搜索：{keyword}～"


def _parse_songlist(text):
    text = text.strip().replace("\\", "/")
    if "/" not in text:
        raise LxMusicError("📋 歌单格式是 来源/歌单ID，比如 tx/123456 哦～")

    source, songlist_id = [part.strip() for part in text.split("/", 1)]
    if source not in SUPPORTED_SOURCES:
        raise LxMusicError("📋 歌单来源只支持 kw、kg、tx、wy、mg、myList 哦～")
    if not songlist_id:
        raise LxMusicError("📋 歌单 ID 不能为空哦～")
    return source, songlist_id


def play_songlist(text):
    source, songlist_id = _parse_songlist(text)
    _open_scheme(f"lxmusic://songlist/play/{quote(source)}/{quote(songlist_id)}")
    return f"📦 歌单请求已发送：{source}/{songlist_id}，马上就来～"


def open_songlist(text):
    source, songlist_id = _parse_songlist(text)
    _open_scheme(f"lxmusic://songlist/open/{quote(source)}/{quote(songlist_id)}")
    return f"📂 正在打开歌单：{source}/{songlist_id}，稍等哦～"


def toggle_play_mode():
    return "😅 切换播放模式的功能还在路上呢，麻烦去 Lx Music 里手动切换一下吧～"
