"""
Lx Music 预缓存模块

功能:
  1. 读取 Lx Music SQLite 数据库，获取当前歌单和即将播放的歌曲
  2. 通过 JS 音源脚本解析音频 URL，下载到本地缓存
  3. 启动本地 HTTP 服务器提供缓存文件
  4. 将 music_url 替换为本地地址，实现零卡顿播放

依赖:
  - Node.js (音源脚本用)
  - Python 标准库 http.server (可选 pip install requests)
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

# ---------- 路径常量 ----------

LX_DATAS_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "lx-music-desktop" / "LxDatas",       # Roaming
    Path(os.environ.get("LOCALAPPDATA", "")) / "lx-music-desktop" / "LxDatas",  # Local fallback
]

# ---------- 预定义歌单 ----------
# 来源固定 ID，无需查数据库
# 格式: "显示名": ("source", "sourceListId")

PRESET_PLAYLISTS: dict[str, tuple[str, str]] = {
    # 网易云音乐 — 真实歌单 ID
    "云音乐热歌榜": ("wy", "board__wy__1"),
    "云音乐新歌榜": ("wy", "board__wy__2"),
    "云音乐飙升榜": ("wy", "board__wy__3"),
    "云音乐原创榜": ("wy", "board__wy__4"),
}

# 预定义名称 → source/sourceListId 反向索引
PRESET_NAMES = {k.lower().replace(" ", ""): v for k, v in PRESET_PLAYLISTS.items()}

# ---------- 缓存配置 ----------

CACHE_DIR = Path(__file__).resolve().parent / "audio_cache"
CACHE_HTTP_PORT = 18908
CACHE_MAX_AGE = 86400  # 缓存文件保留秒数 (24小时)
PRE_CACHE_COUNT = 5    # 提前缓存几首歌
RESOLVE_TIMEOUT = 30   # 音源脚本超时秒数
POLL_INTERVAL = 3      # 轮询间隔秒数

# 音源脚本路径 (从 test_yy_audio_source.py 沿用)
ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_SCRIPT = ROOT_DIR / "sources" / "huibq.js"


# ================================================================
#  Node.js 音源脚本调用（移植自 test_yy_audio_source.py）
# ================================================================

class AudioResolveError(Exception):
    pass


def _find_all_source_scripts() -> list[Path]:
    """返回所有可用音源脚本列表，按优先级排序"""
    seen: set[Path] = set()
    scripts: list[Path] = []

    candidates = [
        DEFAULT_SOURCE_SCRIPT,
        ROOT_DIR / "sources" / "huibq.js",
        Path("sources/huibq.js"),
        ROOT_DIR / "全豆要-聚合音源 v3.0.0.js",
        Path("全豆要-聚合音源 v3.0.0.js"),
        *sorted(Path("sources").glob("*聚合音源*.js")),
        *sorted(Path("sources").glob("*音源*.js")),
        *sorted(Path("sources").glob("HYWmusic*.js")),
        *sorted(Path("sources").glob("*.js")),
        *sorted(Path(".").glob("*聚合音源*.js")),
        *sorted(Path(".").glob("*音源*.js")),
    ]
    for p in candidates:
        resolved = p.resolve()
        if resolved.exists() and resolved not in seen:
            seen.add(resolved)
            scripts.append(resolved)
    if not scripts:
        raise AudioResolveError("未找到音源脚本 (sources/*.js)")
    return scripts


def _find_source_script() -> Path:
    """查找优先级最高的音源脚本"""
    return _find_all_source_scripts()[0]


def resolve_audio_url(source: str, song_id: str, name: str, singer: str,
                      quality: str = "128k",
                      script_path: Path | None = None) -> str:
    """通过 JS 音源脚本解析歌曲的音频下载 URL。"""
    script = script_path or _find_source_script()

    node_code = r'''
const fs = require('fs')
const vm = require('vm')
const payload = JSON.parse(process.argv[1])
let handler = null
const EVENT_NAMES = { request: 'request', inited: 'inited' }
const request = async (url, options, cb) => {
  try {
    const resp = await fetch(url, {
      method: options?.method || 'GET',
      headers: options?.headers || {},
      body: options?.body,
    })
    const text = await resp.text()
    cb(null, { statusCode: resp.status, headers: Object.fromEntries(resp.headers.entries()), body: text })
  } catch (err) { cb(err) }
}
const lx = { EVENT_NAMES, request, on: (name, fn) => { if (name === EVENT_NAMES.request) handler = fn }, send: () => {}, env: 'desktop', version: '2.12.2' }
const sandbox = { console: { log: () => {}, error: () => {}, warn: () => {} }, globalThis: { lx }, setTimeout, clearTimeout, URL, URLSearchParams, Buffer }
sandbox.globalThis.globalThis = sandbox.globalThis
vm.createContext(sandbox)
vm.runInContext(fs.readFileSync(payload.scriptPath, 'utf8'), sandbox, { filename: payload.scriptPath })
if (!handler) throw new Error('音源脚本未注册 request handler')
;(async () => {
  const info = {
    musicInfo: { songmid: payload.songId, songId: payload.songId, id: payload.songId, hash: payload.songId, name: payload.name, singer: payload.singer },
    type: payload.quality,
  }
  const url = await Promise.race([
    handler({ source: payload.source, action: 'musicUrl', info }),
    new Promise((_, reject) => setTimeout(() => reject(new Error('解析播放 URL 超时')), 15000)),
  ])
  process.stdout.write(JSON.stringify({ url }))
})().catch(err => { process.stderr.write(err.message || String(err)); process.exit(1) })
'''
    payload = {
        "scriptPath": str(script),
        "source": source,
        "songId": song_id,
        "name": name,
        "singer": singer,
        "quality": quality,
    }
    try:
        completed = subprocess.run(
            ["node", "-e", node_code, json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=RESOLVE_TIMEOUT,
        )
    except FileNotFoundError:
        raise AudioResolveError("未找到 Node.js，请确认已安装")
    except subprocess.TimeoutExpired:
        raise AudioResolveError(f"音源脚本解析超时 ({RESOLVE_TIMEOUT}s): {name}")

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "未知错误"
        raise AudioResolveError(f"音源脚本执行失败: {stderr}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise AudioResolveError(f"音源脚本输出解析失败: {completed.stdout[:100]}")

    url = data.get("url")
    if not url:
        raise AudioResolveError("音源脚本没有返回播放 URL")
    return url


# 换源搜索解析 — 尝试从 keyword 搜索歌曲并解析音频 URL
FALLBACK_SOURCES = ["wy", "kg", "kw", "mg"]


def search_and_resolve(source: str, keyword: str, quality: str = "128k",
                       script_path: Path | None = None) -> str:
    """通过 JS 音源脚本的 search 接口搜索歌曲，再解析音频下载 URL。

    先用 keyword 搜索，取第一个结果重新 musicUrl 解析。用于换源降级。
    """
    script = script_path or _find_source_script()

    node_code = r'''
const fs = require('fs')
const vm = require('vm')
const payload = JSON.parse(process.argv[1])
let handler = null
const EVENT_NAMES = { request: 'request', inited: 'inited' }
const request = async (url, options, cb) => {
  try {
    const resp = await fetch(url, {
      method: options?.method || 'GET',
      headers: options?.headers || {},
      body: options?.body,
    })
    const text = await resp.text()
    cb(null, { statusCode: resp.status, headers: Object.fromEntries(resp.headers.entries()), body: text })
  } catch (err) { cb(err) }
}
const lx = { EVENT_NAMES, request, on: (name, fn) => { if (name === EVENT_NAMES.request) handler = fn }, send: () => {}, env: 'desktop', version: '2.12.2' }
const sandbox = { console: { log: () => {}, error: () => {}, warn: () => {} }, globalThis: { lx }, setTimeout, clearTimeout, URL, URLSearchParams, Buffer }
sandbox.globalThis.globalThis = sandbox.globalThis
vm.createContext(sandbox)
vm.runInContext(fs.readFileSync(payload.scriptPath, 'utf8'), sandbox, { filename: payload.scriptPath })
if (!handler) throw new Error('音源脚本未注册 request handler')
;(async () => {
  let url = null
  // 1. search
  let searchResp
  try {
    searchResp = await Promise.race([
      handler({ source: payload.source, action: 'search', info: { keyword: payload.keyword, page: 1, type: 'music' } }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('search timeout')), 15000)),
    ])
  } catch (e) { searchResp = null }
  // 2. 从搜索结果中提取第一个
  let first = null
  if (searchResp) {
    const raw = searchResp.data || searchResp
    const list = Array.isArray(raw) ? raw : (raw.list || [])
    if (list.length > 0) first = list[0]
  }
  if (!first) throw new Error('search returned no results')
  // 3. resolve
  const info = {
    musicInfo: {
      songmid: first.songmid || first.id || '',
      songId: first.songId || first.id || '',
      id: first.id || '',
      hash: first.hash || '',
      name: first.name || payload.keyword,
      singer: first.singer || '',
    },
    type: payload.quality,
  }
  try {
    const result = await Promise.race([
      handler({ source: payload.source, action: 'musicUrl', info }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('resolve timeout')), 15000)),
    ])
    url = result && result.url
  } catch (e) {}
  if (!url) throw new Error('resolve failed after search')
  process.stdout.write(JSON.stringify({ url }))
})().catch(err => { process.stderr.write(err.message || String(err)); process.exit(1) })
'''
    payload = {
        "scriptPath": str(script),
        "source": source,
        "keyword": keyword,
        "quality": quality,
    }
    try:
        completed = subprocess.run(
            ["node", "-e", node_code, json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=RESOLVE_TIMEOUT,
        )
    except FileNotFoundError:
        raise AudioResolveError("未找到 Node.js，请确认已安装")
    except subprocess.TimeoutExpired:
        raise AudioResolveError(f"音源脚本搜索解析超时 ({RESOLVE_TIMEOUT}s): {keyword}")

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "未知错误"
        raise AudioResolveError(f"换源搜索解析失败: {stderr}")

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise AudioResolveError(f"换源输出解析失败: {completed.stdout[:100]}")

    url = data.get("url")
    if not url:
        raise AudioResolveError("换源解析没有返回播放 URL")
    return url


# ================================================================
#  Lx Music 数据库读取
# ================================================================

def _find_lx_datas_dir() -> Path:
    """查找 Lx Music 数据目录。"""
    for d in LX_DATAS_DIRS:
        if d.exists():
            return d
    raise FileNotFoundError("未找到 Lx Music 数据目录 (LxDatas)")


def _db_connect() -> sqlite3.Connection:
    """连接到 Lx Music 的 SQLite 数据库。"""
    datas_dir = _find_lx_datas_dir()
    db_path = datas_dir / "lx.data.db"
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def read_playback_state() -> dict[str, Any]:
    """读取 Lx Music 当前播放状态。

    返回:
        {
            "listId": str  (歌单 ID，如 tx_xxx),
            "index": int   (当前播放位置),
            "playlist_name": str,
            "source": str,
            "sourceListId": str,
            "current_song": {"id": str, "name": str, "singer": str},
        }
    """
    datas_dir = _find_lx_datas_dir()
    data_path = datas_dir / "data.json"
    state: dict[str, Any] = {}

    # 1. 从 data.json 读 playInfo
    try:
        with open(data_path, encoding="utf-8") as f:
            data = json.load(f)
        pi = data.get("playInfo") or {}
        state["listId"] = pi.get("listId") or ""
        state["index"] = int(pi.get("index", 0))
        state["progress"] = pi.get("time", 0)
        state["maxTime"] = pi.get("maxTime", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        state["listId"] = ""
        state["index"] = 0

    # 2. 从数据库查歌单和当前歌曲
    if state.get("listId"):
        try:
            conn = _db_connect()
            # 歌单信息
            row = conn.execute("SELECT name, source, sourceListId FROM my_list WHERE id=?",
                               (state["listId"],)).fetchone()
            if row:
                state["playlist_name"] = row["name"]
                state["source"] = row["source"]
                state["sourceListId"] = row["sourceListId"]

            # 当前歌曲
            row = conn.execute("""
                SELECT m.name, m.singer, m.id
                FROM my_list_music_info m
                JOIN my_list_music_info_order o ON m.id = o.musicInfoId AND m.listId = o.listId
                WHERE o.listId = ? AND o."order" = ?
            """, (state["listId"], state["index"])).fetchone()
            if row:
                state["current_song"] = {
                    "id": row["id"],
                    "name": row["name"],
                    "singer": row["singer"],
                }

            conn.close()
        except FileNotFoundError:
            pass

    return state


def get_upcoming_songs(list_id: str, current_index: int,
                       count: int = PRE_CACHE_COUNT) -> list[dict[str, Any]]:
    """从 Lx Music 数据库获取即将播放的歌曲列表。

    返回:
        [{"id": str, "name": str, "singer": str, "source": str, "order": int}, ...]
    """
    try:
        conn = _db_connect()
        # 先查歌单的 source
        list_row = conn.execute("SELECT source FROM my_list WHERE id=?", (list_id,)).fetchone()
        source = list_row["source"] if list_row else "tx"

        rows = conn.execute("""
            SELECT m.name, m.singer, m.id, o."order"
            FROM my_list_music_info m
            JOIN my_list_music_info_order o ON m.id = o.musicInfoId AND m.listId = o.listId
            WHERE o.listId = ? AND o."order" > ?
            ORDER BY o."order" ASC
            LIMIT ?
        """, (list_id, current_index, count)).fetchall()

        conn.close()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "singer": r["singer"],
                "source": source,
                "order": r["order"],
            }
            for r in rows
        ]
    except FileNotFoundError:
        return []


# ================================================================
#  QQ 音乐排行榜 API（公开接口，无需 key）
# ================================================================

# 网易云音乐排行榜映射 (可直接通过 songlist/play 播放)
# 格式: "显示名": bangid
WY_LEADERBOARDS: dict[str, str] = {
    "热歌榜": "3778678",
    "抖音热歌榜": "2250011882",  # 网易云抖音榜
    "新歌榜": "3779629",
    "飙升榜": "19723756",
    "原创榜": "2884035",
    "说唱榜": "991319590",
    "古典榜": "71384707",
    "电音榜": "1978921795",
    "ACG榜": "71385702",
    "韩语榜": "745956260",
    "网络热歌榜": "6723173524",
    "民谣榜": "5059661515",
    "摇滚榜": "5059633707",
    "国风榜": "5059642708",
    "日语榜": "5059644681",
    "欧美热歌榜": "2809513713",
    "欧美新歌榜": "2809577409",
}

# 标准化查询映射 (全小写无空格 → (显示名, bangid))
WY_LEADERBOARDS_NORM: dict[str, tuple[str, str]] = {
    k.lower().replace(" ", ""): (k, v) for k, v in WY_LEADERBOARDS.items()
}


def fetch_qq_leaderboard(topid: int, num: int = 100) -> list[dict[str, Any]]:
    """从 QQ 音乐公开 API 获取排行榜歌曲列表。

    Args:
        topid: 排行榜 ID (26=热歌榜, 27=新歌榜, 62=飙升榜, ...)
        num: 获取歌曲数量

    Returns:
        [{
            "songName": str, "singer": str,
            "mid": str,              # QQ songmid
            "id": int,               # QQ 数字 ID
            "interval": int,         # 时长（秒）
            "albumName": str,
            "albumMid": str,
            "mediaMid": str,         # file.media_mid
            "fileSizes": dict,       # {"128k": bytes, "320k": bytes, "flac": bytes}
            "picUrl": str,           # 专辑封面 URL
        }, ...]

    Raises:
        RuntimeError: API 请求失败或数据异常
    """
    url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)",
        "Content-Type": "application/json",
    }
    body = json.dumps({
        "toplist": {
            "module": "musicToplist.ToplistInfoServer",
            "method": "GetDetail",
            "param": {"topid": topid, "num": num, "period": ""},
        },
        "comm": {"uin": 0, "format": "json", "ct": 20, "cv": 1859},
    }).encode()

    req = Request(url, data=body, headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"请求 QQ 音乐排行榜失败: {e}")

    code = result.get("toplist", {}).get("code")
    if code != 0:
        raise RuntimeError(f"QQ 音乐 API 返回错误码: {code}")

    songs = result.get("toplist", {}).get("data", {}).get("songInfoList", [])
    if not songs:
        raise RuntimeError("排行榜没有歌曲数据")

    song_list = []
    for s in songs:
        name = s.get("title") or s.get("name") or ""
        singers = s.get("singer") or []
        singer = "、".join(
            ns.get("name", "") for ns in singers if isinstance(ns, dict)
        )
        if not name or not singer:
            continue

        mid = s.get("mid", "")
        sid = s.get("id", 0)
        interval_sec = s.get("interval", 0)
        album = s.get("album") or {}
        album_name = album.get("name", "")
        album_mid = album.get("mid", "")
        pic_url = f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{album_mid}.jpg" if album_mid else ""
        file_info = s.get("file") or {}
        media_mid = file_info.get("media_mid", "")
        file_sizes = {}
        if file_info.get("size_128mp3"):
            file_sizes["128k"] = file_info["size_128mp3"]
        if file_info.get("size_320mp3"):
            file_sizes["320k"] = file_info["size_320mp3"]
        if file_info.get("size_flac"):
            file_sizes["flac"] = file_info["size_flac"]

        song_list.append({
            "songName": name,
            "singer": singer,
            "mid": mid,
            "id": sid,
            "interval": interval_sec,
            "albumName": album_name,
            "albumMid": album_mid,
            "mediaMid": media_mid,
            "fileSizes": file_sizes,
            "picUrl": pic_url,
        })

    return song_list


def read_all_playlists() -> list[dict[str, Any]]:
    """读取 Lx Music 中所有已保存的歌单。"""
    try:
        conn = _db_connect()
        rows = conn.execute("SELECT id, name, source, sourceListId, position FROM my_list ORDER BY position").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except FileNotFoundError:
        return []


def switch_to_local_playlist(playlist_id: str) -> dict[str, Any]:
    """通过直接修改 data.json 切换到本地已保存的歌单。

    适用于 board__ 等无法通过 songlist/play 方案播放的内置歌单（如排行榜）。
    修改 data.json 后 Lx Music 会自动加载新歌单。

    Args:
        playlist_id: my_list 表中的内部 id

    Returns:
        包含 name, source, sourceListId 的歌单信息字典

    Raises:
        ValueError: 歌单不存在或数据库/文件错误
    """
    datas_dir = _find_lx_datas_dir()
    data_path = datas_dir / "data.json"

    # 验证歌单存在
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT name, source, sourceListId FROM my_list WHERE id=?",
            (playlist_id,)
        ).fetchone()
        conn.close()
    except FileNotFoundError as e:
        raise ValueError(f"无法连接 Lx Music 数据库: {e}")

    if not row:
        raise ValueError(f"歌单不存在: {playlist_id}")

    info = {"name": row["name"], "source": row["source"], "sourceListId": row["sourceListId"]}

    # 修改 data.json playInfo
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("playInfo", {})
    data["playInfo"]["listId"] = playlist_id
    data["playInfo"]["index"] = 0
    data["playInfo"]["time"] = 0
    data["playInfo"]["maxTime"] = 0

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return info


# ================================================================
#  排行榜 → SQLite 临时歌单
# ================================================================

TEMP_PLAYLIST_PREFIX = "_temp_qq_toplist_"


def _interval_str(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _human_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.2f} MB"


def _build_qq_meta(song: dict[str, Any]) -> str:
    """构建 QQ 歌曲的 meta JSON。"""
    file_sizes = song.get("fileSizes") or {}
    album_mid = song.get("albumMid", "")
    pic_url = song.get("picUrl", "")
    if not pic_url and album_mid:
        pic_url = f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{album_mid}.jpg"

    qualitys = []
    _qualitys = {}
    q_keys = [("128k", "128k"), ("320k", "320k"), ("flac", "flac")]
    for q_key, q_label in q_keys:
        size_bytes = file_sizes.get(q_key, 0)
        if size_bytes > 0:
            size_str = _human_size(size_bytes)
            qualitys.append({"type": q_label, "size": size_str})
            _qualitys[q_label] = {"size": size_str}

    meta = {
        "songId": song.get("mid", ""),
        "albumName": song.get("albumName", ""),
        "picUrl": pic_url,
        "qualitys": qualitys,
        "_qualitys": _qualitys,
        "albumId": album_mid,
        "strMediaMid": song.get("mediaMid", ""),
        "id": song.get("id", 0),
        "albumMid": album_mid,
    }
    return json.dumps(meta, ensure_ascii=False)


def delete_temp_playlist(list_id: str) -> None:
    """删除临时歌单及其所有歌曲记录。"""
    try:
        conn = _db_connect()
        conn.execute("DELETE FROM my_list_music_info_order WHERE listId=?", (list_id,))
        conn.execute("DELETE FROM my_list_music_info WHERE listId=?", (list_id,))
        conn.execute("DELETE FROM my_list WHERE id=?", (list_id,))
        conn.commit()
        conn.close()
    except (FileNotFoundError, sqlite3.Error):
        import traceback
        traceback.print_exc()


def create_qq_leaderboard_playlist(topid: int, songs: list[dict[str, Any]]) -> str:
    """在 SQLite 中创建临时排行榜歌单，返回 playlist_id。

    会先清理同 topid 的旧临时歌单，再创建新的。
    """
    list_id = f"{TEMP_PLAYLIST_PREFIX}{topid}"
    # 反向查找 topid 对应的排行榜名称
    display_name = next((name for name, tid in QQ_LEADERBOARDS.items() if tid == topid), f"QQ排行榜_{topid}")

    conn = _db_connect()
    try:
        # 在同一连接和事务中清理旧数据，避免并发连接导致的残留
        conn.execute("DELETE FROM my_list_music_info_order WHERE listId=?", (list_id,))
        conn.execute("DELETE FROM my_list_music_info WHERE listId=?", (list_id,))
        conn.execute("DELETE FROM my_list WHERE id=?", (list_id,))

        # 创建歌单
        conn.execute(
            "INSERT INTO my_list (id, name, source, sourceListId, position) VALUES (?, ?, ?, ?, ?)",
            (list_id, display_name, "tx", f"board__tx__{topid}", 999),
        )

        # 插入歌曲
        for order, song in enumerate(songs):
            mid = song.get("mid", "")
            music_id = f"tx_{mid}"
            interval_str = _interval_str(song.get("interval", 0))
            meta_str = _build_qq_meta(song)

            conn.execute(
                "INSERT OR REPLACE INTO my_list_music_info (id, listId, name, singer, source, interval, meta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (music_id, list_id, song["songName"], song["singer"], "tx", interval_str, meta_str),
            )
            conn.execute(
                "INSERT OR REPLACE INTO my_list_music_info_order (listId, musicInfoId, \"order\") VALUES (?, ?, ?)",
                (list_id, music_id, order),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return list_id


# ================================================================
#  缓存管理器
# ================================================================


def _safe_filename(song_id: str, ext: str = ".mp3") -> str:
    """根据 song_id 生成安全的缓存文件名。"""
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', song_id)
    return f"{safe}{ext}"


def _guess_ext(url: str) -> str:
    """从 URL 猜测文件扩展名。"""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in (".mp3", ".flac", ".wav", ".ogg", ".aac", ".m4a"):
        return ext
    return ".mp3"


def download_audio(url: str, dest: Path, timeout: int = 60) -> Path:
    """下载音频文件到指定路径。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return dest


def inject_local_url(song_id: str, source: str, local_url: str, quality: str = "128k"):
    """将本地缓存 URL 写入 Lx Music 的 music_url 表。

    这样 Lx Music 下一首播放时会直接从本地加载，而不是去在线源拉取。
    """
    try:
        conn = _db_connect()
        key = f"{source}_{song_id}_{quality}" if not song_id.startswith(f"{source}_") else f"{song_id}_{quality}"
        # 先看是否已有记录
        existing = conn.execute("SELECT url FROM music_url WHERE id=?", (key,)).fetchone()
        if existing:
            # 只替换尚未缓存的 URL (已经指向本地的跳过)
            old_url = existing["url"]
            if old_url and ("127.0.0.1" in old_url or "localhost" in old_url):
                conn.close()
                return
        # 插入或替换
        conn.execute(
            "INSERT OR REPLACE INTO music_url (id, url) VALUES (?, ?)",
            (key, local_url),
        )
        conn.commit()
        conn.close()
    except FileNotFoundError:
        pass


# ================================================================
#  本地 HTTP 服务器
# ================================================================

class RangeHTTPServer(HTTPServer):
    """支持 Range 请求的本地 HTTP 服务器。"""
    allow_reuse_address = True


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """为缓存目录提供 HTTP 服务，支持 Range 请求。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CACHE_DIR), **kwargs)

    def send_head(self):
        """重写 send_head，优先处理 Range 请求。"""
        path = self.translate_path(self.path)
        if not os.path.exists(path) or os.path.isdir(path):
            return super().send_head()

        file_size = os.path.getsize(path)
        content_type = self.guess_type(path)
        range_header = self.headers.get("Range")

        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end_str = match.group(2)
                end = int(end_str) if end_str else file_size - 1
                if start >= file_size or end >= file_size:
                    self.send_error(416, "Range Not Satisfiable")
                    return None
                length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                f = open(path, "rb")
                f.seek(start)
                return f

        return super().send_head()

    def log_message(self, format, *args):
        if "404" not in format % args:
            return
        sys.stderr.write(f"[HTTP] {format % args}\n")


class LocalCacheServer:
    """本地缓存 HTTP 服务器。"""

    def __init__(self, port: int = CACHE_HTTP_PORT):
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        if self.server:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.server = RangeHTTPServer(("127.0.0.1", self.port), RangeRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server = None
            self.thread = None

    def url_for(self, filename: str) -> str:
        return f"{self.base_url}/{quote(filename)}"


# ================================================================
#  缓存调度器
# ================================================================

class CacheScheduler:
    """后台缓存调度器，轮询 Lx Music 状态并自动预缓存即将播放的歌曲。

    特性:
      - 提前缓存即将播放的歌曲到本地
      - 播完后自动删除缓存文件，不占硬盘
      - 歌单切换时自动清理旧缓存
    """

    def __init__(self, source_script: Path | None = None,
                 pre_cache_count: int = PRE_CACHE_COUNT,
                 quality: str = "320k"):
        self.source_script = source_script
        self.pre_cache_count = pre_cache_count
        self.quality = quality
        self.http_server = LocalCacheServer()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_list_id = ""
        self._last_index = -1
        self._cached_orders: set[int] = set()   # 已缓存的 order
        self._order_to_file: dict[int, Path] = {}  # order → 缓存文件路径

    def start(self):
        """启动后台缓存线程。启动时自动清理旧缓存文件。"""
        if self._running:
            return
        self.clean_orphaned()
        self._running = True
        self.http_server.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.http_server.stop()

    def _run_loop(self):
        while self._running:
            try:
                self._tick()
            except Exception as exc:
                sys.stderr.write(f"[Cache] 缓存调度出错: {exc}\n")
            time.sleep(POLL_INTERVAL)

    def _tick(self):
        state = read_playback_state()
        list_id = state.get("listId", "")
        cur_index = state.get("index", 0)

        # 歌单切换 → 清除旧缓存文件和记录
        if self._last_list_id and list_id != self._last_list_id:
            self._clean_all_cache()
            self._last_list_id = list_id
            self._last_index = -1

        # 歌曲推进 → 清理状态跟踪（文件保留，避免 Lx Music 重读时 404）
        if cur_index > self._last_index:
            for order in list(self._order_to_file):
                if order <= cur_index:
                    self._order_to_file.pop(order, None)
            self._cached_orders = {o for o in self._cached_orders if o > cur_index}
            self._last_index = cur_index

        if not list_id:
            return

        # 缓存当前正在播的歌（它的 URL 已在 music_url 表中）
        cur_song = state.get("current_song")
        if cur_song and cur_song.get("id"):
            cur_order = cur_index
            if cur_order not in self._cached_orders:
                song = {
                    "id": cur_song["id"],
                    "name": cur_song.get("name", ""),
                    "singer": cur_song.get("singer", ""),
                    "source": cur_song.get("source", "tx"),
                    "order": cur_order,
                }
                try:
                    fpath = self._cache_one(song)
                    if fpath:
                        self._cached_orders.add(cur_order)
                        self._order_to_file[cur_order] = fpath
                except Exception as exc:
                    sys.stderr.write(f"[Cache] current song cache failed: {exc}\n")


        upcoming = get_upcoming_songs(list_id, cur_index, self.pre_cache_count)
        if not upcoming:
            return

        to_cache = [s for s in upcoming if s["order"] not in self._cached_orders]
        if not to_cache:
            return

        for song in to_cache:
            try:
                fpath = self._cache_one(song)
                self._cached_orders.add(song["order"])
                if fpath:
                    self._order_to_file[song["order"]] = fpath
            except Exception as exc:
                sys.stderr.write(f"[Cache] 缓存失败 [{song['order']}] {song['singer']} - {song['name']}: {exc}\n")

    def _clean_all_cache(self):
        """删除所有已跟踪的缓存文件并清空记录。"""
        for fpath in self._order_to_file.values():
            if fpath and fpath.exists():
                try:
                    fpath.unlink()
                except OSError:
                    pass
        self.clean_orphaned()
        self._order_to_file.clear()
        self._cached_orders.clear()

    @staticmethod
    def clean_orphaned():
        """删除缓存目录中所有文件并清理 stale music_url 条目（启动时清理）。"""
        # 删文件
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
        # 清理指向本地缓存的 music_url 记录，避免 Lx Music 请求已删除的文件
        try:
            conn = _db_connect()
            conn.execute("DELETE FROM music_url WHERE url LIKE ?", ("%127.0.0.1%",))
            conn.execute("DELETE FROM music_url WHERE url LIKE ?", ("%localhost%",))
            conn.commit()
            conn.close()
        except FileNotFoundError:
            pass
        except Exception as exc:
            sys.stderr.write(f"[Cache] 清理 music_url 失败: {exc}\n")

    def status_text(self) -> str:
        """返回缓存状态文本。"""
        if not CACHE_DIR.exists():
            return "缓存目录不存在"
        files = list(CACHE_DIR.iterdir())
        file_count = len(files)
        total_size = sum(f.stat().st_size for f in files) / (1024 * 1024)
        cached = sorted(self._order_to_file.items())
        lines = [
            f"缓存状态: {file_count} 个文件 ({total_size:.1f} MB)",
            f"音质: {self.quality}",
            f"预缓存进度: {len(self._cached_orders)} 首已就绪",
        ]
        if cached:
            lines.append(f"当前缓存队列:")
            for order, fpath in cached[:5]:
                sz = fpath.stat().st_size / (1024 * 1024) if fpath.exists() else 0
                lines.append(f"  [{order}] {fpath.name} ({sz:.1f} MB)")
        return "\n".join(lines)

    def _cache_one(self, song: dict[str, Any]) -> Path | None:
        """解析单个歌曲的 URL，下载到缓存，注入本地 URL。

        先以歌曲原生 source 尝试解析并缓存；
        若失败则以歌名+歌手为 keyword 对其他源进行搜索降级。
        若当前音源脚本全部失败，自动换下一个可用脚本重试。
        返回缓存文件路径，完全失败返回 None。
        """
        name = song["name"]
        singer = song["singer"]
        source = song.get("source", "tx")
        song_id = song["id"]

        raw_song_id = song_id
        for prefix in ("tx_", "wy_", "kw_", "kg_", "mg_"):
            if song_id.startswith(prefix):
                raw_song_id = song_id[len(prefix):]
                break

        keyword = f"{name} - {singer}"

        # 优先目标音质，解析或下载失败后降级
        qualities_to_try = [self.quality]
        fallbacks = {"flac24bit": ["flac", "320k", "128k"],
                     "flac": ["320k", "128k"],
                     "320k": ["128k"],
                     "128k": []}
        if self.quality in fallbacks:
            qualities_to_try.extend(f for f in fallbacks[self.quality] if f not in qualities_to_try)

        # 优先查 music_url 表（Lx 内置 SDK 已解析过的 URL）
        try:
            conn = _db_connect()
            for q in qualities_to_try:
                music_key = f"{source}_{raw_song_id}_{q}"
                row = conn.execute("SELECT url FROM music_url WHERE id=?", (music_key,)).fetchone()
                if row:
                    url = row["url"]
                    if url and "127.0.0.1" not in url and "localhost" not in url:
                        ext = _guess_ext(url)
                        filename = _safe_filename(song_id, ext)
                        dest = CACHE_DIR / filename
                        if not dest.exists():
                            download_audio(url, dest)
                        local_url = self.http_server.url_for(filename)
                        inject_local_url(song_id, source, local_url, self.quality)
                        conn.close()
                        return dest
            conn.close()
        except (FileNotFoundError, Exception):
            pass

        # music_url 表没有 → 用 JS 音源脚本解析 URL
        for q in qualities_to_try:
            try:
                url = resolve_audio_url(source, raw_song_id, name, singer, q, self.source_script)
                if url:
                    ext = _guess_ext(url)
                    filename = _safe_filename(song_id, ext)
                    dest = CACHE_DIR / filename
                    if not dest.exists():
                        download_audio(url, dest)
                    local_url = self.http_server.url_for(filename)
                    inject_local_url(song_id, source, local_url, self.quality)
                    return dest
            except Exception:
                continue

        return None


# ================================================================
#  命令行入口 (测试用)
# ================================================================

def cmd_status():
    """显示当前播放状态和即将播放的歌曲。"""
    state = read_playback_state()
    print(f"当前歌单: {state.get('playlist_name', '?') or '?'}")
    print(f"  listId={state.get('listId')}")
    print(f"  index={state.get('index')}  progress={state.get('progress',0):.0f}s/{state.get('maxTime',0):.0f}s")
    cur = state.get("current_song")
    if cur:
        print(f"  current: {cur['singer']} - {cur['name']} ({cur['id']})")

    upcoming = get_upcoming_songs(state.get("listId", ""), state.get("index", 0))
    if upcoming:
        print(f"\n即将播放 (接下来 {len(upcoming)} 首):")
        for s in upcoming:
            print(f"  [{s['order']}] {s['singer']} - {s['name']} ({s['id']})")

    print(f"\n已保存的歌单:")
    for pl in read_all_playlists():
        print(f"  {pl['name']}  source={pl['source']}  listId={pl['sourceListId']}")


def cmd_playlist(name: str):
    """输出指定预定义歌单的 source/listId，用于 play_songlist。"""
    key = name.lower().replace(" ", "")
    if key in PRESET_NAMES:
        source, list_id = PRESET_NAMES[key]
        print(f"playlist: {source}/{list_id}")
    else:
        print(f"未找到歌单: {name}，可用:")
        for pname in PRESET_PLAYLISTS:
            print(f"  {pname}")


def cmd_cache(count: int = PRE_CACHE_COUNT):
    """立即触发一次预缓存。"""
    scheduler = CacheScheduler(pre_cache_count=count)
    scheduler.http_server.start()
    scheduler._tick()
    scheduler.http_server.stop()
    print(f"缓存完成，目录: {CACHE_DIR}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Lx Music 音频预缓存工具")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="查看当前播放状态")
    pl_parser = sub.add_parser("playlist", help="查看预定义歌单")
    pl_parser.add_argument("name", nargs="?", help="歌单名称")

    cache_parser = sub.add_parser("cache", help="立即预缓存")
    cache_parser.add_argument("--count", type=int, default=PRE_CACHE_COUNT, help=f"缓存数量，默认{PRE_CACHE_COUNT}")

    daemon_parser = sub.add_parser("daemon", help="启动后台缓存守护")
    daemon_parser.add_argument("--quality", default="128k", help="音质")
    daemon_parser.add_argument("--count", type=int, default=PRE_CACHE_COUNT)

    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "playlist":
        if args.name:
            cmd_playlist(args.name)
        else:
            print("可用歌单:")
            for name in PRESET_PLAYLISTS:
                print(f"  {name}")
    elif args.cmd == "cache":
        cmd_cache(args.count)
    elif args.cmd == "daemon":
        scheduler = CacheScheduler(quality=args.quality, pre_cache_count=args.count)
        scheduler.start()
        print(f"缓存守护已启动 (quality={args.quality}, pre_cache={args.count})")
        print(f"缓存目录: {CACHE_DIR}")
        print(f"HTTP 服务: http://127.0.0.1:{CACHE_HTTP_PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            scheduler.stop()
            print("已停止")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
