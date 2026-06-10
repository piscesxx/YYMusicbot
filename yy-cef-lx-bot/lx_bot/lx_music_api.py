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


class LxMusicError(Exception):
    pass


def _request(path, params=None):
    try:
        response = requests.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LxMusicError("未检测到 Lx Music，请确认播放器已启动并开启开放 API。") from exc

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError:
        return {}


def _open_scheme(url):
    if not webbrowser.open(url):
        raise LxMusicError("无法唤起 Lx Music，请确认已安装并注册 lxmusic 协议。")


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


def get_status(filter_fields=None):
    params = None
    if filter_fields:
        params = {"filter": ",".join(filter_fields)}
    return _request("/status", params=params)


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
        return "当前没有播放歌曲。"

    message = f"当前播放：{song_text}"
    if volume is not None:
        message += f"，当前音量 {volume}"
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
    return "已静音。" if target else "已取消静音。"


def toggle_mute():
    current = get_mute()
    if current is None:
        raise LxMusicError("无法获取当前静音状态，请确认 Lx Music Open API 已返回 mute 字段。")
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
    return "已继续播放。"


def pause():
    _request("/pause")
    return "已暂停播放。"


def toggle_pause():
    if is_playing():
        return pause()
    return play()


def _song_after_switch(previous_signature):
    deadline = time.monotonic() + 3
    last_song = ""
    while time.monotonic() < deadline:
        try:
            status = _player_data(get_status())
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
    if song_text:
        return f"〖已为您切换{direction}，即将播放：{song_text}〗"
    return f"已切换到{direction}。"


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
        raise LxMusicError("音量必须是 0 到 100 的整数。") from exc

    if not 0 <= volume <= 100:
        raise LxMusicError("音量范围是 0 到 100。")

    _set_volume(volume)
    return f"音量已设置为 {volume}。"


def change_volume(delta):
    try:
        delta = int(delta)
    except (TypeError, ValueError) as exc:
        raise LxMusicError("音量增减值必须是整数。") from exc

    current = get_volume()
    if current is None:
        if _last_known_volume is not None:
            current = _last_known_volume
        else:
            raise LxMusicError("无法获取当前音量，请先使用“设置音量20”这类绝对音量命令，或确认 Lx Music Open API 已返回 volume 字段。")

    target = max(0, min(100, current + delta))
    _set_volume(target)

    if delta >= 0:
        return f"音量增加 {delta}，当前音量 {target}。"
    return f"音量减少 {abs(delta)}，当前音量 {target}。"


def _split_song_keyword(keyword):
    for separator in ("-", "－", "—", "–"):
        if separator in keyword:
            song_name, singer = keyword.split(separator, 1)
            return song_name.strip(), singer.strip()
    return keyword.strip(), ""


def search_play(keyword):
    keyword = keyword.strip()
    if not keyword:
        raise LxMusicError("点歌内容不能为空。")

    _open_scheme(f"lxmusic://music/searchPlay/{quote(keyword)}")
    song_name, singer = _split_song_keyword(keyword)
    song_text = f"{singer} - {song_name}" if singer else song_name
    return f"〖🐟〗点歌成功：{song_text} (即将为您播放)"


def search(keyword):
    keyword = keyword.strip()
    if not keyword:
        raise LxMusicError("搜索内容不能为空。")

    _open_scheme(f"lxmusic://music/search/{quote(keyword)}")
    return f"已打开 Lx Music 搜索：{keyword}。"


def _parse_songlist(text):
    text = text.strip().replace("\\", "/")
    if "/" not in text:
        raise LxMusicError("歌单格式应为 来源/歌单ID，例如 tx/123456。")

    source, songlist_id = [part.strip() for part in text.split("/", 1)]
    if source not in SUPPORTED_SOURCES:
        raise LxMusicError("歌单来源仅支持 kw、kg、tx、wy、mg、myList。")
    if not songlist_id:
        raise LxMusicError("歌单ID不能为空。")
    return source, songlist_id


def play_songlist(text):
    source, songlist_id = _parse_songlist(text)
    _open_scheme(f"lxmusic://songlist/play/{quote(source)}/{quote(songlist_id)}")
    return f"已发送播放歌单请求：{source}/{songlist_id}。"


def open_songlist(text):
    source, songlist_id = _parse_songlist(text)
    _open_scheme(f"lxmusic://songlist/open/{quote(source)}/{quote(songlist_id)}")
    return f"已发送打开歌单请求：{source}/{songlist_id}。"


def toggle_play_mode():
    return "当前 Lx Music Open API 暂无随机/顺序/单曲循环播放模式切换接口，请在播放器内手动切换。"
