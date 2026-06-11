from pathlib import Path

import argparse
import asyncio
import json
import re
import socket
import subprocess
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, asdict
from itertools import count
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from lx_bot import lx_music_api as LxMusicApi

try:
    import websocket
except ImportError as exc:
    raise SystemExit("缺少 websocket-client，请先安装：pip install websocket-client") from exc

try:
    from audio_cache import (
        CacheScheduler, PRESET_PLAYLISTS, PRESET_NAMES,
        QQ_LEADERBOARDS, QQ_LEADERBOARDS_NORM,
        fetch_qq_leaderboard, read_playback_state,
        read_all_playlists, switch_to_local_playlist,
        create_qq_leaderboard_playlist, delete_temp_playlist,
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
    create_qq_leaderboard_playlist = None
    delete_temp_playlist = None
    HAS_AUDIO_CACHE = False


HTTP_TIMEOUT = 0.25
WS_TIMEOUT = 5
DEFAULT_PORTS = (33395, 38980, 30796, 39007)
CHANNEL_STATE_EXPRESSION = r"""
(async () => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  let info = yy && yy.channel && yy.channel.channelInfo || {};
  if (yy && yy.channel && typeof yy.channel.getChannelInfo === 'function') {
    try {
      const fresh = await yy.channel.getChannelInfo(true);
      if (fresh) info = fresh;
    } catch (e) {}
  }
  return {
    href: location.href,
    title: document.title,
    CurrentChannelSessId: String(window.CurrentChannelSessId || ''),
    hasYY: !!yy,
    hasJoinChannel: !!(yy && yy.channel && typeof yy.channel.joinChannel === 'function'),
    hasChannelMessage: !!(yy && yy.chat && yy.chat.cef && typeof yy.chat.cef.getModule === 'function'),
    channelInfo: {
      sid: info.sid || 0,
      asid: info.asid || 0,
      ssid: info.ssid || 0,
      channelName: info.channelName || '',
      subChannelName: info.subChannelName || '',
      entrance: info.entrance || ''
    }
  };
})()
"""
START_SIGNATURE_DELAY = 3
MIN_SONG_SECONDS = 15
SONG_FEEDBACK_PREFIX = "〖🐟〗"
QUEUE_STATE_FILE = Path(__file__).resolve().parent / "song_queue.json"
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


@dataclass
class CefPage:
    port: int
    title: str
    url: str
    websocket_url: str
    uid: str = ""
    channel_id: str = ""
    session_id: str = ""


@dataclass
class SongRequest:
    keyword: str
    user_id: str
    display_text: str


class ChannelSwitchRequest:
    def __init__(self, sid: int):
        self.sid = sid


class ReadAloudRequest:
    def __init__(self, text: str):
        self.text = text


class SongQueue:
    def __init__(self):
        self.items: deque[SongRequest] = deque()
        self.current: SongRequest | None = None
        self.history: list[SongRequest] = []
        self.current_signature: str | None = None
        self.current_started_at = 0.0
        self.signature_ready_at = 0.0

    # ---- 持久化 ----

    def save_state(self):
        """将排队的点歌写入文件，重启后恢复。"""
        data = {
            "items": [asdict(r) for r in self.items],
            "history": [asdict(r) for r in self.history],
        }
        try:
            QUEUE_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"保存点歌队列失败: {exc}")

    def load_state(self):
        """从文件恢复排队点歌。"""
        try:
            text = QUEUE_STATE_FILE.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        for key in ("items", "history"):
            for item in data.get(key, []):
                req = SongRequest(**item)
                if key == "items":
                    self.items.append(req)
                else:
                    self.history.append(req)
        if self.items:
            print(f"已恢复点歌队列，共 {len(self.items)} 首待播放，{len(self.history)} 首历史")

    # ---- 操作 ----

    def enqueue(self, keyword: str, row: dict[str, Any]) -> tuple[SongRequest, int]:
        song_name, singer = LxMusicApi._split_song_keyword(keyword)
        display_text = f"{singer} - {song_name}" if singer else song_name
        user_id = str(row.get("imid") or row.get("uid") or "")
        request = SongRequest(keyword=keyword, user_id=user_id, display_text=display_text)
        self.items.append(request)
        self.save_state()
        position = len(self.items) + (1 if self.current else 0)
        return request, position

    def has_pending(self) -> bool:
        return bool(self.items)

    def start_request(self, request: SongRequest, remember_current: bool = True):
        if remember_current and self.current:
            self.history.append(self.current)
        self.current = request
        self.current_signature = None
        self.current_started_at = time.monotonic()
        self.signature_ready_at = self.current_started_at + START_SIGNATURE_DELAY

    def pop_next(self) -> SongRequest | None:
        if not self.items:
            return None
        request = self.items.popleft()
        self.start_request(request)
        self.save_state()
        return request

    def replay_previous(self) -> SongRequest | None:
        if not self.history:
            return None
        if self.current:
            self.items.appendleft(self.current)
        request = self.history.pop()
        self.start_request(request, remember_current=False)
        self.save_state()
        return request

    def update_current_signature(self):
        if self.current and not self.current_signature and time.monotonic() >= self.signature_ready_at:
            self.current_signature = get_song_signature()

    def current_finished(self) -> bool:
        if not self.current:
            return False
        if time.monotonic() - self.current_started_at < MIN_SONG_SECONDS:
            return False
        self.update_current_signature()
        if not is_lx_playing():
            return True
        signature = get_song_signature()
        return bool(self.current_signature and signature and signature != self.current_signature)

    def clear_current(self):
        if self.current:
            self.history.append(self.current)
        self.current = None
        self.current_signature = None
        self.current_started_at = 0.0
        self.signature_ready_at = 0.0
        self.save_state()


class DevToolsClient:
    def __init__(self, websocket_url: str):
        self.ws = websocket.create_connection(websocket_url, timeout=WS_TIMEOUT)
        self.ids = count(1)

    def close(self):
        self.ws.close()

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        message_id = next(self.ids)
        self.ws.send(json.dumps({
            "id": message_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        }))

        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != message_id:
                continue
            if "exceptionDetails" in message.get("result", {}):
                details = message["result"]["exceptionDetails"]
                raise RuntimeError(json.dumps(details, ensure_ascii=False))
            return message.get("result", {}).get("result", {}).get("value")


def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_candidate_ports() -> list[int]:
    ports: set[int] = set(DEFAULT_PORTS)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match 'YY.exe|yyexternal' } | "
                "Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
        for match in re.finditer(r"--remote-debugging-port=(\d+)", completed.stdout):
            ports.add(int(match.group(1)))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(ports)


def enrich_page(page: CefPage) -> CefPage:
    client = None
    try:
        client = DevToolsClient(page.websocket_url)
        info = client.evaluate(r"""
(() => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  const session = String(window.CurrentChannelSessId || '');
  const userAgent = navigator.userAgent || '';
  const uidMatch = userAgent.match(/UID\/(\d+)/);
  const sessionParts = session.split('-');
  const sessionUid = sessionParts.length >= 2 ? sessionParts[sessionParts.length - 2] : '';
  return {
    uid: String((yy && yy.loginUid) || (uidMatch ? uidMatch[1] : '') || sessionUid),
    sessionId: session,
    channelId: sessionParts.length ? sessionParts[sessionParts.length - 1] : '',
    hasApi: !!api,
    hasYY: !!yy
  };
})()
""") or {}
        page.uid = str(info.get("uid") or "")
        page.channel_id = str(info.get("channelId") or "")
        page.session_id = str(info.get("sessionId") or "")
    except Exception:
        pass
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
    return page


def pages_from_port(port: int) -> list[CefPage]:
    try:
        targets = fetch_json(f"http://127.0.0.1:{port}/json/list")
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return []

    if not isinstance(targets, list):
        return []

    pages = []
    for target in targets:
        websocket_url = target.get("webSocketDebuggerUrl")
        if websocket_url:
            pages.append(enrich_page(CefPage(port, target.get("title", ""), target.get("url", ""), websocket_url)))
    return pages


def scan_pages() -> list[CefPage]:
    pages = []
    seen: set[tuple[int, str]] = set()
    for port in get_candidate_ports():
        if not is_port_open(port):
            continue
        for page in pages_from_port(port):
            key = (page.port, page.websocket_url)
            if key in seen:
                continue
            seen.add(key)
            pages.append(page)
    return pages


def page_matches_args(page: CefPage, args, strict_channel: bool = True) -> bool:
    if args.uid and page.uid != str(args.uid):
        return False
    if strict_channel and args.channel and page.channel_id != str(args.channel):
        return False
    return True


def page_has_channel_message(page: CefPage) -> bool:
    client = None
    try:
        client = DevToolsClient(page.websocket_url)
        result = client.evaluate(r"""
(() => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  const module = yy && yy.chat && yy.chat.cef && yy.chat.cef.getModule('YY.Channel.ChannelMessage');
  return !!module && !module.isNull && typeof module.getCacheMessage === 'function' && typeof yy.chat.sendPublicMessage === 'function';
})()
""")
        return bool(result)
    except Exception:
        return False
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def read_channel_state(page: CefPage) -> dict[str, Any] | None:
    client = None
    try:
        client = DevToolsClient(page.websocket_url)
        return client.evaluate(CHANNEL_STATE_EXPRESSION, await_promise=True)
    except Exception:
        return None
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def capture_page_state(page: CefPage, state: dict[str, Any] | None) -> dict[str, Any]:
    info = (state or {}).get("channelInfo") or {}
    return {
        "websocket_url": page.websocket_url,
        "title": page.title,
        "page_channel_id": page.channel_id,
        "session": str((state or {}).get("CurrentChannelSessId") or page.session_id or ""),
        "sid": int(info.get("sid") or 0),
        "asid": int(info.get("asid") or 0),
        "ssid": int(info.get("ssid") or 0),
        "hasYY": bool((state or {}).get("hasYY")),
        "hasChannelMessage": bool((state or {}).get("hasChannelMessage")),
    }


def page_state_changed(page: CefPage, state: dict[str, Any] | None, baseline: dict[str, Any] | None) -> bool:
    if not baseline:
        return True
    current = capture_page_state(page, state)
    keys = ("title", "page_channel_id", "session", "sid", "asid", "ssid", "hasYY", "hasChannelMessage")
    return any(current[key] != baseline.get(key) for key in keys)


def is_target_channel(page: CefPage, state: dict[str, Any] | None, expected_sid: int) -> bool:
    info = (state or {}).get("channelInfo") or {}
    page_channel_id = str(page.channel_id or "")
    channel_sid = str(info.get("sid") or "")
    channel_asid = str(info.get("asid") or "")
    channel_ssid = str(info.get("ssid") or "")
    text_blob = " ".join([
        str(page.title or ""),
        str(page.url or ""),
        str((state or {}).get("CurrentChannelSessId") or page.session_id or ""),
    ])
    target_sid = str(expected_sid)
    return target_sid in {channel_asid, channel_sid, channel_ssid, page_channel_id} or target_sid in text_blob


def build_scheme(sid: int) -> str:
    return f"yy://pd-[sid={sid}]"


def open_scheme(url: str):
    if not webbrowser.open(url):
        raise RuntimeError("无法唤起 YY，请确认已安装并注册 yy:// 协议。")


def wait_for_switched_page(args, expected_sid: int, timeout_seconds: float, baseline: dict[str, Any]):
    # 切频道时旧页面会短暂失效，这里等待新频道页恢复后再重绑。
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        page, error = pick_channel_page(args, strict_channel=False, baseline=baseline)
        if not page:
            last_error = error or ""
            time.sleep(0.5)
            continue
        state = read_channel_state(page)
        if state and is_target_channel(page, state, expected_sid):
            return page, state
        if state and page_state_changed(page, state, baseline):
            return page, state
        time.sleep(0.5)
    if last_error:
        raise RuntimeError(last_error)
    raise RuntimeError("等待目标频道页超时，未观察到匹配频道，也未观察到新的频道页状态变化。")


def describe_page(page: CefPage) -> str:
    return f"port={page.port} uid={page.uid or '?'} channel={page.channel_id or '?'} title={normalize_text(page.title)} url={page.url}"


def pick_channel_page(args, strict_channel: bool = False,
                      baseline: dict[str, Any] | None = None) -> tuple[CefPage | None, str | None]:
    """从扫描结果中选出合适的频道页面。"""
    candidates: list[tuple[CefPage, dict[str, Any]]] = []
    for page in scan_pages():
        if "yy.com" not in page.url.lower():
            continue
        enrich_page(page)
        if not page_matches_args(page, args, strict_channel):
            continue
        state = read_channel_state(page)
        if state and (not baseline or page_state_changed(page, state, baseline)):
            candidates.append((page, state))
    if len(candidates) == 1:
        return candidates[0][0], None
    if len(candidates) > 1:
        # 优先选可发公屏消息的页面，其次选有频道 ID 的页面
        candidates.sort(key=lambda ps: (
            not bool(ps[1].get("hasChannelMessage")),
            not bool(ps[1].get("channelInfo", {}).get("sid")),
            len(ps[1].get("CurrentChannelSessId", "") or ""),
        ))
        return candidates[0][0], None
    for page in scan_pages():
        if "yy.com" in page.url.lower():
            enrich_page(page)
            return page, None
    return None, "未找到 YY CEF 频道页面，请确认 YY 客户端已登录并进入频道，或检查 --uid/--channel 是否正确。"


def wait_for_channel_page(args, timeout_seconds: float, strict_channel: bool = True, baseline: dict[str, Any] | None = None) -> CefPage:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        page, error = pick_channel_page(args, strict_channel=strict_channel, baseline=baseline)
        if page:
            return page
        last_error = error or ""
        time.sleep(0.5)
    raise RuntimeError(last_error or "等待 YY CEF 频道页面超时。")


def find_channel_page(args, strict_channel: bool = True, baseline: dict[str, Any] | None = None, fatal: bool = True) -> CefPage:
    page, error = pick_channel_page(args, strict_channel=strict_channel, baseline=baseline)
    if page:
        return page
    if fatal:
        raise SystemExit(error)
    raise RuntimeError(error)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if "�" in text:
        return text
    try:
        repaired = text.encode("latin1").decode("gbk")
    except UnicodeError:
        return text
    return repaired if repaired.count("�") <= text.count("�") else text


def message_to_row(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("senderProp") or {}
    composite = message.get("compositeMsg") or []
    composite_text = "".join(normalize_text(item.get("data", "")) for item in composite if isinstance(item, dict))
    text = normalize_text(message.get("fullText") or message.get("textMsg") or composite_text)

    return {
        "msgType": message.get("msgType"),
        "uid": message.get("uid") or sender.get("uid"),
        "imid": sender.get("imid"),
        "nick": normalize_text(sender.get("nick")),
        "text": text.strip(),
        "textUUID": sender.get("textUUID"),
        "timestamp": sender.get("textMICROSECOND_TIMESTAMP"),
        "isSelfSend": bool(sender.get("Send")),
    }


class YYCefApi:
    def __init__(self, args):
        self.args = args
        self.page: CefPage | None = None
        self.client: DevToolsClient | None = None
        self.rebind(find_channel_page(args, strict_channel=True))

    def rebind(self, page: CefPage):
        self.close()
        self.page = page
        self.client = DevToolsClient(page.websocket_url)

    def reconnect(self, baseline: dict[str, Any] | None = None, timeout_seconds: float = 12):
        page = wait_for_channel_page(self.args, timeout_seconds, strict_channel=False, baseline=baseline)
        self.rebind(page)
        return page

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def status(self) -> dict[str, Any]:
        return self.client.evaluate(CHANNEL_STATE_EXPRESSION, await_promise=True)

    def read_messages(self) -> list[dict[str, Any]]:
        messages = self.client.evaluate(r"""
(async () => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  const module = yy && yy.chat && yy.chat.cef && yy.chat.cef.getModule('YY.Channel.ChannelMessage');
  if (!module || module.isNull || typeof module.getCacheMessage !== 'function') {
    throw new Error('YY.Channel.ChannelMessage.getCacheMessage 不可用');
  }
  return await module.getCacheMessage();
})()
""", await_promise=True)
        if not isinstance(messages, list):
            return []
        return [message_to_row(message) for message in messages if isinstance(message, dict)]

    def send_message(self, text: str):
        expression = """
((text) => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  if (!yy || !yy.chat || typeof yy.chat.sendPublicMessage !== 'function') {
    throw new Error('yy.chat.sendPublicMessage 不可用');
  }
  yy.chat.sendPublicMessage(text);
  return true;
})(%s)
""" % json.dumps(text, ensure_ascii=False)
        return self.client.evaluate(expression)


def speak_async(text: str, enabled: bool):
    if not enabled:
        return

    def worker():
        try:
            from lx_bot.tts import Read
            asyncio.run(Read(text))
        except ImportError as exc:
            print(f"TTS不可用，请安装 edge-tts 和 pygame: {exc}")
        except Exception as exc:
            print(f"TTS失败: {exc}")

    threading.Thread(target=worker, daemon=True).start()


def send_feedback(api: YYCefApi, text: str):
    print(f"反馈: {text}")
    api.send_message(text)


def get_song_signature() -> str:
    try:
        status = LxMusicApi._player_data(LxMusicApi.get_status())
    except LxMusicApi.LxMusicError:
        return ""

    name = LxMusicApi._pick(status, "name", "songName", "title") or ""
    singer = LxMusicApi._pick(status, "singer", "artist", "author") or ""
    song_id = LxMusicApi._pick(status, "id", "songmid", "mid", "hash") or ""
    return f"{song_id}|{name}|{singer}"


def is_lx_playing() -> bool:
    try:
        return LxMusicApi.is_playing()
    except LxMusicApi.LxMusicError:
        return False


def start_song_request(request: SongRequest) -> str:
    LxMusicApi.search_play(request.keyword)
    return f"〖即将为您播放：{request.display_text}〗"


def play_previous_request(queue: SongQueue) -> str:
    request = queue.replay_previous()
    if not request:
        return LxMusicApi.previous_song()
    LxMusicApi.search_play(request.keyword)
    return f"〖已为您切换上一首，即将播放：{request.display_text}〗"


def update_song_queue(api: YYCefApi, queue: SongQueue):
    if queue.current and not queue.current_finished():
        return

    if queue.current and queue.current_finished():
        queue.clear_current()

    # 点歌队列已空 → 恢复之前保存的歌单（如有）
    if not queue.has_pending():
        feedback = _restore_saved_playlist()
        if feedback:
            send_feedback(api, feedback)
        return

    # 有点歌待播放 — 首次时保存当前歌单
    _save_current_playlist()

    request = queue.pop_next()
    if not request:
        return

    feedback = start_song_request(request)
    send_feedback(api, feedback)


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


def _play_qq_leaderboard(display_name: str, topid: int) -> str:
    """通过 QQ 音乐 API 拉取排行榜，写入 SQLite 临时歌单并切换到 Lx Music 播放。"""
    if not HAS_AUDIO_CACHE or fetch_qq_leaderboard is None or create_qq_leaderboard_playlist is None:
        return "音频缓存模块不可用"
    try:
        songs = fetch_qq_leaderboard(topid)
    except Exception as exc:
        return f"获取「{display_name}」失败: {exc}"
    if not songs:
        return f"「{display_name}」没有歌曲数据"

    # 写入 SQLite 创建临时歌单
    try:
        list_id = create_qq_leaderboard_playlist(topid, songs)
    except Exception as exc:
        return f"创建临时歌单失败: {exc}"

    # 切换到该歌单
    try:
        switch_to_local_playlist(list_id)
        time.sleep(0.5)
        LxMusicApi.play()
    except Exception as exc:
        return f"切换歌单失败: {exc}"

    return f"〖已切换至「{display_name}」〗共 {len(songs)} 首"


def handle_volume(content: str) -> str | None:
    if content.startswith("设置音量"):
        return LxMusicApi.set_volume(content[4:].strip())
    if content.startswith("音量+"):
        return LxMusicApi.change_volume(content[3:].strip())
    if content.startswith("音量-"):
        return LxMusicApi.change_volume(f"-{content[3:].strip()}")
    if content.startswith("+") and content[1:].strip().isdigit():
        return LxMusicApi.change_volume(content[1:].strip())
    if content.startswith("-") and content[1:].strip().isdigit():
        return LxMusicApi.change_volume(content)
    return None


def handle_command(content: str, row: dict[str, Any], queue: SongQueue) -> tuple[str | ChannelSwitchRequest | ReadAloudRequest | None, bool]:
    content = content.strip()
    if not content:
        return None, False

    volume_result = handle_volume(content)
    if volume_result:
        return volume_result, True

    switch_match = re.fullmatch(r"切换频道\s*[：:]?\s*(\d+)", content)
    if switch_match:
        return ChannelSwitchRequest(int(switch_match.group(1))), True

    if content.startswith("读"):
        read_text = content[1:].strip()
        if read_text:
            return ReadAloudRequest(read_text), True
        return None, False

    if content in {"0", "帮助", "菜单"}:
        return HELP_TEXT, False
    if content == "1":
        return LxMusicApi.get_current_song_text(), True
    if content == "2":
        return LxMusicApi.toggle_pause(), True
    if content in {"4", "上一首"}:
        return play_previous_request(queue), True
    if content in {"播放器上一首", "歌单上一首"}:
        return LxMusicApi.previous_song(), True
    if content in {"5", "下一首", "切歌"}:
        if queue.has_pending():
            queue.clear_current()
            request = queue.pop_next()
            if request:
                return start_song_request(request), True
        # 没有排队的点歌了 → 恢复歌单（如有）
        if _saved_playlist:
            queue.current = None
            queue.current_signature = None
            queue.save_state()
            feedback = _restore_saved_playlist()
            if feedback:
                return feedback, True
        return LxMusicApi.next_song(), True
    if content == "6":
        return LxMusicApi.toggle_mute(), True
    if content == "7":
        return LxMusicApi.toggle_play_mode(), True
    if content.startswith("点歌"):
        song = content[2:].strip()
        if not song:
            raise LxMusicApi.LxMusicError("点歌内容不能为空。")
        request, position = queue.enqueue(song, row)
        if queue.current:
            return f"{SONG_FEEDBACK_PREFIX}点歌成功：{request.display_text} (您的歌曲当前位于第{position}位) - {request.user_id}", True
        return f"{SONG_FEEDBACK_PREFIX}点歌成功：{request.display_text} (即将为您播放) - {request.user_id}", True
    if content.startswith("播放歌单"):
        return LxMusicApi.play_songlist(content[4:].strip()), True
    if content.startswith("导入歌单"):
        return LxMusicApi.open_songlist(content[4:].strip()), True

    # 查看点歌队列
    if content in {"点歌队列", "歌曲队列", "排队"}:
        if not queue.items and not queue.current:
            return "点歌队列为空", True
        lines = []
        if queue.current:
            lines.append(f"正在播放：{queue.current.display_text}")
        if queue.items:
            for i, item in enumerate(queue.items, 1):
                lines.append(f"{i}. {item.display_text}")
        return "\n".join(lines), True

    # ==== 新增功能：切换歌单 / 歌单列表 / 当前歌单 / 缓存状态 ====

    if HAS_AUDIO_CACHE:
        switch_match = re.fullmatch(r"切换歌单\s*[：:]?\s*(.+)", content)
        if switch_match:
            # 用户主动切换歌单，清空保存状态避免冲突
            _saved_playlist.clear()
            name = switch_match.group(1).strip()
            key = name.lower().replace(" ", "")
            # QQ 音乐排行榜优先（直接从 API 拉取实时榜单）
            if key in QQ_LEADERBOARDS_NORM:
                display_name, topid = QQ_LEADERBOARDS_NORM[key]
                return _play_qq_leaderboard(display_name, topid), True
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


def format_chat(row: dict[str, Any]) -> str:
    nick = row.get("nick") or ""
    uid = row.get("uid") or ""
    imid = row.get("imid") or ""
    text = row.get("text") or ""
    return f"{nick} uid={uid} imid={imid}: {text}"


def message_id_of(row: dict[str, Any]) -> str:
    return str(row.get("textUUID") or f"{row.get('uid')}:{row.get('timestamp')}:{row.get('text')}")


def prime_seen(api: YYCefApi) -> set[str]:
    seen: set[str] = set()
    for row in api.read_messages():
        message_id = message_id_of(row)
        if message_id:
            seen.add(message_id)
    return seen


def current_page_baseline(api: YYCefApi) -> dict[str, Any]:
    state = read_channel_state(api.page) if api.page else None
    return capture_page_state(api.page, state) if api.page else {}


def reconnect_api(api: YYCefApi, baseline: dict[str, Any], timeout_seconds: float = 12) -> tuple[set[str], dict[str, Any]]:
    page = api.reconnect(baseline=baseline, timeout_seconds=timeout_seconds)
    state = read_channel_state(page)
    new_baseline = capture_page_state(page, state)
    print("已重新绑定 YY CEF 页面:")
    print(describe_page(page))
    print(json.dumps(state or {}, ensure_ascii=False, indent=2))
    return prime_seen(api), new_baseline


def switch_channel(api: YYCefApi, args, sid: int) -> tuple[str, set[str], dict[str, Any]]:
    # 先用 yy:// 完成跳转，再重新接管跳转后的原生频道页。
    baseline = current_page_baseline(api)
    scheme = build_scheme(sid)
    print(f"打开协议: {scheme}")
    open_scheme(scheme)
    page, state = wait_for_switched_page(args, sid, 12, baseline)
    api.rebind(page)
    seen = prime_seen(api)
    new_baseline = capture_page_state(page, state)
    feedback = f"已切换到频道 sid={sid}"
    print("切换后的 YY CEF 页面:")
    print(describe_page(page))
    print(json.dumps(state or {}, ensure_ascii=False, indent=2))
    return feedback, seen, new_baseline


# 用于追踪已欢迎过的用户
_welcomed_uids: set[str] = set()

# 点歌 playlist save/restore：点歌会通过 search_play 替换 Lx Music 上下文，
# 当点歌队列清空后需要恢复原来的歌单。
_saved_playlist: dict[str, Any] = {}


def _save_current_playlist():
    """保存当前 Lx Music 歌单状态，用于点歌结束后恢复。"""
    global _saved_playlist
    if _saved_playlist:
        return
    if not HAS_AUDIO_CACHE:
        return
    try:
        state = read_playback_state()
        list_id = state.get("listId") or ""
        if list_id:
            _saved_playlist = {
                "listId": list_id,
                "playlist_name": state.get("playlist_name", "未知"),
                "index": state.get("index", 0),
            }
    except Exception:
        pass


def _restore_saved_playlist() -> str | None:
    """恢复之前保存的歌单。返回反馈文本或 None。"""
    global _saved_playlist
    if not _saved_playlist or not HAS_AUDIO_CACHE:
        _saved_playlist = {}
        return None
    list_id = _saved_playlist.get("listId", "")
    pl_name = _saved_playlist.get("playlist_name", "未知")
    _saved_playlist = {}
    if not list_id:
        return None
    try:
        switch_to_local_playlist(list_id)
        time.sleep(0.3)
        LxMusicApi.play()
        return f"已恢复歌单「{pl_name}」"
    except Exception as exc:
        return f"恢复歌单失败: {exc}"


def run_bot(args):
    api = YYCefApi(args)
    queue = SongQueue()
    queue.load_state()

    # 启动缓存调度器
    cache_scheduler: CacheScheduler | None = None
    if HAS_AUDIO_CACHE and CacheScheduler is not None and not args.no_cache:
        try:
            cache_scheduler = CacheScheduler(quality=args.cache_quality)
            cache_scheduler.start()
            print(f"音频缓存调度器已启动（音质: {args.cache_quality}）")
        except Exception as exc:
            print(f"音频缓存调度器启动失败: {exc}")

    try:
        status = api.status()
        baseline = capture_page_state(api.page, status)
        print("已连接 YY CEF 页面:")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        print("开始监听公屏命令，按 Ctrl+C 退出。")

        seen = prime_seen(api)

        while True:
            try:
                # 1. 确认当前 CEF 页面仍然是有效频道页。
                latest_status = api.status()
                latest_baseline = capture_page_state(api.page, latest_status)
                if page_state_changed(api.page, latest_status, baseline):
                    print("检测到频道页状态变化，正在重新绑定。")
                    seen, baseline = reconnect_api(api, baseline)
                    continue
                baseline = latest_baseline

                # 2. 推进点歌队列，再读取公屏缓存。
                update_song_queue(api, queue)
                rows = api.read_messages()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"频道页读取失败，正在尝试重新绑定: {exc}")
                try:
                    seen, baseline = reconnect_api(api, baseline)
                    continue
                except Exception as reconnect_exc:
                    print(f"重新绑定失败，将继续重试: {reconnect_exc}")
                    time.sleep(args.interval)
                    continue

            # 3. 对新增公屏消息去重并分发命令。
            for row in rows:
                message_id = message_id_of(row)
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)

                # 检测用户进入频道，自动发送欢迎语音
                text = row.get("text") or ""
                # 格式: 通知： [[U]开心[/U]] 进入 [听歌练枪] 频道。(10:02:20)
                # 格式: 通知： [鱼摆摆] 进入 [听歌练枪] 频道。(10:03:02)
                if " 进入 [" in text:
                    # 提取昵称: 从 "通知： [" 之后到 "] 进入" 之前
                    nick_match = re.search(r"通知：\s*\[(.+?)\]\s*进入\s*\[", text)
                    nick = nick_match.group(1) if nick_match else "朋友"
                    # 去掉可能的 [U][/U] 格式标签
                    nick = re.sub(r"\[/?U\]", "", nick)
                    print(f"[进入频道] nick={nick} text={text}")
                    dedup_key = f"enter:{nick}:{text[-20:]}"
                    if dedup_key not in _welcomed_uids:
                        _welcomed_uids.add(dedup_key)
                        welcome_text = f"欢迎 {nick} 进入频道"
                        print(f"→ 发送欢迎: {welcome_text}")
                        speak_async(welcome_text, True)

                if row.get("msgType") != 2:
                    continue
                if row.get("isSelfSend") and not args.process_self:
                    print(f"忽略自身消息: {row.get('text')}")
                    continue

                print(format_chat(row))
                try:
                    feedback, speak = handle_command(row.get("text") or "", row, queue)
                except LxMusicApi.LxMusicError as exc:
                    feedback, speak = str(exc), True
                except Exception as exc:
                    feedback, speak = f"命令处理失败：{exc}", True

                if isinstance(feedback, ChannelSwitchRequest):
                    pending_text = f"正在切换频道 {feedback.sid}"
                    try:
                        send_feedback(api, pending_text)
                    except Exception as exc:
                        print(f"发送切换提示失败，正在尝试重新绑定: {exc}")
                        try:
                            seen, baseline = reconnect_api(api, baseline)
                            send_feedback(api, pending_text)
                        except Exception as reconnect_exc:
                            print(f"发送切换提示失败，稍后继续重试: {reconnect_exc}")
                            continue
                    try:
                        feedback_text, seen, baseline = switch_channel(api, args, feedback.sid)
                        send_feedback(api, feedback_text)
                    except Exception as exc:
                        error_text = f"切换频道失败：{exc}"
                        print(error_text)
                        try:
                            send_feedback(api, error_text)
                        except Exception:
                            try:
                                seen, baseline = reconnect_api(api, baseline)
                                send_feedback(api, error_text)
                            except Exception as reconnect_exc:
                                print(f"切换失败后的反馈发送也失败，稍后继续重试: {reconnect_exc}")
                    continue

                if isinstance(feedback, ReadAloudRequest):
                    speak_async(feedback.text, True)
                    continue

                if feedback:
                    try:
                        send_feedback(api, feedback)
                    except Exception as exc:
                        print(f"发送反馈失败，正在尝试重新绑定: {exc}")
                        try:
                            seen, baseline = reconnect_api(api, baseline)
                            send_feedback(api, feedback)
                        except Exception as reconnect_exc:
                            print(f"发送反馈失败，稍后继续重试: {reconnect_exc}")
                            continue
                    update_song_queue(api, queue)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止机器人。")
    finally:
        api.close()
        if cache_scheduler:
            try:
                cache_scheduler.stop()
                print("音频缓存调度器已停止。")
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="YY CEF + Lx Music 机器人。")
    parser.add_argument("--interval", type=float, default=0.5, help="公屏轮询间隔秒数，默认 0.5")
    parser.add_argument("--process-self", action="store_true", help="处理自己账号发出的公屏消息")
    parser.add_argument("--uid", help="指定 YY 登录 UID，用于多账号时选择发送反馈的账号")
    parser.add_argument("--channel", help="指定 YY 原始频道号，用于多频道时选择频道页面")
    parser.add_argument("--cache-quality", default="320k", help="缓存音质，默认 320k")
    parser.add_argument("--no-cache", action="store_true", help="禁用音频缓存")
    args = parser.parse_args()
    run_bot(args)


if __name__ == "__main__":
    main()
