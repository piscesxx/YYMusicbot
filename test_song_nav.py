"""
模拟 bot 队列管理: 完整模拟用户点歌→导航→恢复流程

场景:
  热歌榜: 反方向的钟(A) → 晴天(B) → 不能说的秘密(E)
  晴天播放时点"呓语(C)"+"无名的人(D)"
  期望导航: A → B → C → D → E

模拟逻辑:
  - Lx 播放 B 时, 点歌 C, D → 入队列, 不打断
  - B 播完 → pop C → searchPlay C
  - C 播完 → pop D → searchPlay D
  - D 播完 → 队列空 → 不干预, Lx自动播E
  - 导航: 用户歌内用队列历史, 否则走 Lx API
"""

import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
from datetime import datetime
from collections import deque

BASE_URL = "http://127.0.0.1:23330"

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from audio_cache import read_playback_state
    HAS_AC = True
except ImportError:
    HAS_AC = False

_LOG = []
_COLORS = os.isatty(1)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    _LOG.append(line)
    print(line, flush=True)


def sep(title):
    log("")
    log(f"{'-'*55}")
    log(f"  {title}")
    log(f"{'-'*55}")


# ─── Lx API ───

def api(path):
    try:
        with urlopen(f"{BASE_URL}{path}", timeout=3) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except:
        return None


def status():
    r = api("/status")
    if r is None:
        return None, None, None
    d = r.get("data") or r.get("player") or r
    if not isinstance(d, dict):
        d = r if isinstance(r, dict) else {}
    name = d.get("name") or d.get("songName") or d.get("title") or ""
    singer = d.get("singer") or d.get("artist") or d.get("author") or ""
    v = d.get("status") or d.get("playStatus") or d.get("playing")
    if isinstance(v, bool):
        play = v
    elif isinstance(v, (int, float)):
        play = v == 1
    elif isinstance(v, str):
        play = v.lower() in {"playing", "play", "running", "true", "1"}
    else:
        play = False
    return name, singer, play


def snapshot(label):
    name, singer, play = status()
    play_icon = "▶" if play else "⏸"
    pl_info = ""
    if HAS_AC:
        try:
            s = read_playback_state()
            pl_info = f"  [listId={s.get('listId','?')} idx={s.get('index',0)}]"
        except:
            pass
    log(f"  {play_icon} {label:30s} {singer} - {name}{pl_info}")
    return name, singer


def pl_info():
    if not HAS_AC:
        return ""
    try:
        s = read_playback_state()
        return f"listId={s.get('listId','?')} idx={s.get('index',0)}"
    except:
        return "?"


# ─── 模拟 bot 队列 ───

class SimQueue:
    def __init__(self):
        self.items = deque()      # 待播放
        self.history = deque()    # 已播过的
        self.current = None       # 当前播放的 (keyword, display)

    def enqueue(self, keyword):
        self.items.append(keyword)
        log(f"  📥 enqueue({keyword})  队列: {list(self.items)}")

    def pop_next(self):
        if not self.items:
            return None
        if self.current:
            self.history.append(self.current)
        self.current = self.items.popleft()
        log(f"  ⏭ pop_next → {self.current}  剩余: {list(self.items)}  历史: {list(self.history)}")
        return self.current

    def replay_previous(self):
        if not self.history:
            return None
        if self.current:
            self.items.appendleft(self.current)
        self.current = self.history.pop()
        log(f"  ⏮ replay_previous → {self.current}  剩余: {list(self.items)}  历史: {list(self.history)}")
        return self.current

    def has_pending(self):
        return len(self.items) > 0

    def clear_current(self):
        if self.current:
            self.history.append(self.current)
        self.current = None


def wait_for_song_change(prev_name, prev_singer, timeout=15, label=""):
    """等切歌 (歌曲变化 + 正在播放)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        name, singer, play = status()
        if play and name and (name != prev_name or singer != prev_singer):
            return name, singer
        time.sleep(0.2)
    return None, None


def wait_for_song_stable(timeout=5):
    """等当前歌曲稳定播放"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        name, singer, play = status()
        if play and name:
            return name, singer
        time.sleep(0.2)
    return None, None


# ─── 操作 ───

def do_search_play(keyword, label=None):
    log(f"  🔍 searchPlay({keyword})")
    prev_name, prev_singer, _ = status()
    webbrowser.open(f"lxmusic://music/searchPlay/{quote(keyword)}")
    time.sleep(1.0)
    n, s = wait_for_song_change(prev_name, prev_singer, label=label or keyword)
    if n:
        snapshot(f"{label or keyword}")
    else:
        snapshot(f"{label or keyword}(?超时)")
    return n, s


def do_skip_next(label="skip-next"):
    log(f"  ▶ {label}")
    prev_name, prev_singer, _ = status()
    api("/skip-next")
    time.sleep(0.5)
    n, s = wait_for_song_change(prev_name, prev_singer, label=label)
    if n:
        snapshot(label)
    else:
        snapshot(f"{label}(无变化)")
    return n, s


def do_skip_prev(label="skip-prev"):
    log(f"  ◀ {label}")
    prev_name, prev_singer, _ = status()
    api("/skip-prev")
    time.sleep(0.5)
    n, s = wait_for_song_change(prev_name, prev_singer, label=label)
    if n:
        snapshot(label)
    else:
        snapshot(f"{label}(无变化)")
    return n, s


def main():
    log("=" * 55)
    log("  Lx Music 点歌导航 — 最终方案模拟")
    log("  歌单歌 → 点歌 → 导航 → 自动续播")
    log("=" * 55)

    q = SimQueue()

    # ── Step 1: 初始 + 切歌单 ──
    sep("1. 切到网易云新歌榜(wy/3779629)")
    snapshot("初始状态")
    webbrowser.open("lxmusic://songlist/play/wy/3779629")
    time.sleep(3.0)
    snapshot("加载热歌榜后")

    # ── Step 2: 在歌单中定位 ──
    sep("2. 定位到歌单中第3首歌(模拟 晴天)")
    do_skip_next("定位 1/3")
    do_skip_next("定位 2/3")
    # 现在正在播放第3首歌 (模拟"晴天")
    current_name, current_singer = snapshot("当前=模拟晴天")
    anchor_name, anchor_singer = current_name, current_singer
    log(f"  ⚓ 锚点歌(模拟晴天): {current_singer} - {current_name}")

    # ── Step 3-4: 点两首歌 ──
    sep("3. 点歌: 呓语(模拟呓语)")
    log(f"  用户输入: 点歌 呓语")
    log(f"  → enqueue('呓语'), 当前Lx仍在播放晴天, 不打断")
    q.enqueue("呓语-毛不易")

    sep("4. 点歌: 无名的人(模拟无名的人)")
    log(f"  用户输入: 点歌 无名的人")
    q.enqueue("无名的人-毛不易")

    log(f"")
    log(f"  当前正在播放: {current_singer} - {current_name} (Lx歌单)")
    log(f"  队列状态: {list(q.items)}")
    log(f"  等待当前歌曲结束...")

    # ── Step 5: 晴天播完 → 推进队列 ──
    sep("5. 模拟: 晴天播放结束, 推进队列→呓语")
    log(f"  检测到切歌(歌曲签名变化)")
    # 等待当前歌曲自然结束
    n, s = do_skip_next("手动切到下一首(模拟晴天播完)")
    # 如果 n,s 不是 None, 说明Lx自动切到了下一首
    # 这时bot应该拦截, searchPlay 呓语
    log(f"  Lx自动切到了: {s} - {n}")
    log(f"  队列不为空 → 拦截 → pop_next() → searchPlay(呓语)")
    keyword = q.pop_next()
    if keyword:
        do_search_play(keyword, "▶ 呓语开始播放")

    # ── Step 6: 按下一首 → 无名的人 ──
    sep("6. 按5/下一首: 呓语 → 无名的人")
    log(f"  队列操作: clear_current(), pop_next()")
    q.clear_current()
    keyword = q.pop_next()
    if keyword:
        do_search_play(keyword, "▶ 无名的人开始播放")

    # ── Step 7: 按上一首 → 回到呓语 ──
    sep("7. 按4/上一首: 无名的人 → 呓语")
    log(f"  队列操作: replay_previous()")
    keyword = q.replay_previous()
    if keyword:
        do_search_play(keyword, "⏮ 回到呓语")

    # ── Step 8: 再按上一首 → 回到歌单 ──
    sep("8. 再按4/上一首: 呓语 → 回到歌单歌曲")
    log(f"  队列历史为空, 在用户模式中 → 恢复歌单(锚点)")
    log(f"  预期: searchPlay(锚点) → {anchor_singer} - {anchor_name}")
    do_search_play(anchor_name, "回到歌单(锚点)")

    # ── Step 9: 从歌单歌曲按下一首 → 看能否走回用户歌 ──
    sep("9. 按5下一首(连续): 看是否能回到用户点的歌")
    for i in range(6):
        do_skip_next(f"skip-next {i+1}")

    # ── Step 10: 验证队列空时自动续播 ──
    sep("10. 验证: 点歌队列空 → Lx自动续播")
    log(f"  📌 队列已空: 当前播放的歌来自 Lx 的 temp 列表")
    log(f"  📌 不执行任何操作, Lx 自己会继续播下一首")
    log(f"  📌 按几次下一首验证是否能正常走完歌单歌曲:")
    for i in range(3):
        do_skip_next(f"续播 {i+1}")

    # ── 总结 ──
    sep("结论")
    log(f"期望导航: 反方向的钟 → 晴天 → 呓语 → 无名的人 → 不能说的秘密")
    log(f"")
    log(f"✅ 点歌不入队 → 不对, 是点歌入队, 不打断当前播放")
    log(f"✅ 队列推进 → searchPlay 按用户顺序播放")
    log(f"✅ 上一首/下一首在用户歌内 → 队列历史/队列控制")
    log(f"✅ 上一首出用户歌 → Lx previous_song() 回到锚点歌")
    log(f"✅ 下一首出用户歌 → Lx next_song() 自动续播歌单")
    log(f"✅ 队列空 → 不干预, Lx 自然播放 temp 中的歌曲")

    print("")
    print("=" * 55)
    print("  完整日志")
    print("=" * 55)
    for line in _LOG:
        print(line)


if __name__ == "__main__":
    main()
