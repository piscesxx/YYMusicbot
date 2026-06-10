import os
import webbrowser
from urllib.parse import quote

import requests


BASE_URL = os.environ.get("LX_MUSIC_API_BASE_URL", "http://127.0.0.1:23330").rstrip("/")
TIMEOUT = 3
SUPPORTED_SOURCES = {"kw", "kg", "tx", "wy", "mg", "myList"}
VOLUME_KEYS = {"volume", "volumeSize", "volume_size", "volumePercent", "volume_percent"}
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


def get_current_song_text():
    status = _player_data(get_status())
    name = _pick(status, "name", "songName", "title")
    singer = _pick(status, "singer", "artist", "author")
    volume = get_volume(status)

    if not name:
        return "当前没有播放歌曲。"

    message = f"当前播放：{name}"
    if singer:
        message += f" - {singer}"
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


def get_volume(status=None):
    global _last_known_volume
    volume = _extract_volume_recursive(status or get_status())
    if volume is None:
        volume = _extract_volume_recursive(get_status(filter_fields=["status", "volume", "mute"]))
    if volume is not None:
        _last_known_volume = volume
    return volume


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


def next_song():
    try:
        _request("/skip-next")
    except LxMusicError:
        _open_scheme("lxmusic://player/skipNext")
    return "已切换到下一首。"


def previous_song():
    try:
        _request("/skip-prev")
    except LxMusicError:
        _open_scheme("lxmusic://player/skipPrev")
    return "已切换到上一首。"


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
    return "当前 Lx Music Open API 未确认切换播放模式接口，请在播放器内手动切换。"


def toggle_mic():
    return "当前无法可靠控制 YY 开麦/闭麦，请提供 YY 快捷键或控件信息。"
