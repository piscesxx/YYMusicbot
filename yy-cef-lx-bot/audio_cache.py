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
CACHE_MAX_AGE = 7200  # 缓存文件保留秒数 (2小时)
PRE_CACHE_COUNT = 3    # 提前缓存几首歌
RESOLVE_TIMEOUT = 30   # 音源脚本超时秒数
POLL_INTERVAL = 3      # 轮询间隔秒数

# 音源脚本路径 (从 test_yy_audio_source.py 沿用)
ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_SCRIPT = ROOT_DIR / "全豆要-聚合音源 v3.0.0.js"


# ================================================================
#  Node.js 音源脚本调用（移植自 test_yy_audio_source.py）
# ================================================================

class AudioResolveError(Exception):
    pass


def _find_source_script() -> Path:
    """依次查找可用的音源脚本"""
    candidates = [
        DEFAULT_SOURCE_SCRIPT,
        Path("全豆要-聚合音源 v3.0.0.js"),
        *list(Path(".").glob("*聚合音源*.js")),
        *list(Path(".").glob("*音源*.js")),
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()
    raise AudioResolveError("未找到音源脚本 (全豆要-聚合音源*.js)")


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
                JOIN my_list_music_info_order o ON m.id = o.musicInfoId
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
            JOIN my_list_music_info_order o ON m.id = o.musicInfoId
            WHERE o.listId = ? AND o."order" > ?
            GROUP BY m.id, o."order"
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

# 完整 QQ 音乐排行榜映射 (board__tx__{topid})
QQ_LEADERBOARDS: dict[str, int] = {
    "热歌榜": 26,
    "抖音热歌榜": 60,  # QQ Music 抖快榜
    "新歌榜": 27,
    "飙升榜": 62,
    "流行指数榜": 4,
    "抖快榜": 60,
    "内地榜": 5,
    "欧美榜": 3,
    "说唱榜": 58,
    "韩国榜": 16,
    "日本榜": 17,
    "香港地区榜": 59,
    "台湾地区榜": 61,
    "影视金曲榜": 29,
    "DJ舞曲榜": 63,
    "网络歌曲榜": 28,
    "喜力电音榜": 57,
    "国风热歌榜": 65,
    "综艺新歌榜": 64,
    "动漫音乐榜": 72,
    "游戏音乐榜": 73,
    "腾讯音乐人原创榜": 52,
    "校园音乐人排行榜": 131,
    "听歌识曲榜": 67,
    "K歌金曲榜": 36,
    "有声榜": 75,
}

# 标准化查询映射 (全小写无空格 → (显示名, topid))
QQ_LEADERBOARDS_NORM: dict[str, tuple[str, int]] = {
    k.lower().replace(" ", ""): (k, v) for k, v in QQ_LEADERBOARDS.items()
}


def fetch_qq_leaderboard(topid: int, num: int = 100) -> list[dict[str, str]]:
    """从 QQ 音乐公开 API 获取排行榜歌曲列表。

    Args:
        topid: 排行榜 ID (26=热歌榜, 27=新歌榜, 62=飙升榜, ...)
        num: 获取歌曲数量

    Returns:
        [{"songName": str, "singer": str}, ...]

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
        if name and singer:
            song_list.append({"songName": name, "singer": singer})

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

        # 歌曲推进 → 删除已播完的缓存文件
        if cur_index > self._last_index:
            for order in list(self._order_to_file):
                if order <= cur_index:
                    fpath = self._order_to_file.pop(order, None)
                    if fpath and fpath.exists():
                        try:
                            fpath.unlink()
                        except OSError:
                            pass
            self._cached_orders = {o for o in self._cached_orders if o > cur_index}
            self._last_index = cur_index

        if not list_id:
            return

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
        """删除缓存目录中所有文件（启动时清理旧缓存）。"""
        if not CACHE_DIR.exists():
            return
        for f in CACHE_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass

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

        如果目标音质解析或下载失败，自动降级重试。
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

        # 优先目标音质，解析或下载失败后降级
        qualities_to_try = [self.quality]
        fallbacks = {"flac24bit": ["flac", "320k", "128k"],
                     "flac": ["320k", "128k"],
                     "320k": ["128k"],
                     "128k": []}
        if self.quality in fallbacks:
            qualities_to_try.extend(f for f in fallbacks[self.quality] if f not in qualities_to_try)

        last_error = None
        for q in qualities_to_try:
            try:
                url = resolve_audio_url(source, raw_song_id, name, singer, q, self.source_script)
                ext = _guess_ext(url)
                filename = _safe_filename(song_id, ext)
                dest = CACHE_DIR / filename
                if not dest.exists():
                    download_audio(url, dest)

                local_url = self.http_server.url_for(filename)
                inject_local_url(song_id, source, local_url, self.quality)
                return dest
            except (AudioResolveError, subprocess.TimeoutExpired) as e:
                last_error = e
            except Exception as e:
                msg = str(e)
                if "404" in msg:
                    last_error = f"{q} 下载 404"
                else:
                    last_error = f"{q} 下载失败: {msg}"

        sys.stderr.write(f"[Cache] 所有音质缓存失败 [{song.get('order')}] {singer} - {name}: {last_error}\n")
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
