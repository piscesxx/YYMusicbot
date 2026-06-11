import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from cef_probe import DevToolsClient, scan_targets


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_SCRIPT = ROOT_DIR / "全豆要-聚合音源 v3.0.0.js"
CACHE_DIR = Path(__file__).resolve().parent / "yy_audio_cache"
DEFAULT_PORTS = [31867, 31488, 31592, 31195, 31318]


class YYAudioSourceError(Exception):
    pass


def run_node_source(script_path: Path, source: str, song_id: str, name: str, singer: str, quality: str) -> str:
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
  } catch (err) {
    cb(err)
  }
}
const lx = {
  EVENT_NAMES,
  request,
  on: (name, fn) => { if (name === EVENT_NAMES.request) handler = fn },
  send: () => {},
  env: 'desktop',
  version: '2.12.2',
}
const sandbox = { console: { log: () => {}, error: () => {}, warn: () => {} }, globalThis: { lx }, setTimeout, clearTimeout, URL, URLSearchParams, Buffer }
sandbox.globalThis.globalThis = sandbox.globalThis
vm.createContext(sandbox)
vm.runInContext(fs.readFileSync(payload.scriptPath, 'utf8'), sandbox, { filename: payload.scriptPath })
if (!handler) throw new Error('音源脚本未注册 request handler')
;(async () => {
  const info = {
    musicInfo: {
      songmid: payload.songId,
      songId: payload.songId,
      id: payload.songId,
      hash: payload.songId,
      name: payload.name,
      singer: payload.singer,
    },
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
        "scriptPath": str(script_path),
        "source": source,
        "songId": song_id,
        "name": name,
        "singer": singer,
        "quality": quality,
    }
    completed = subprocess.run(
        ["node", "-e", node_code, json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if completed.returncode != 0:
        raise YYAudioSourceError(completed.stderr.strip() or "音源脚本执行失败")
    data = json.loads(completed.stdout)
    url = data.get("url")
    if not url:
        raise YYAudioSourceError("音源脚本没有返回播放 URL")
    return url


def download_probe(url: str) -> tuple[int, str, Path]:
    CACHE_DIR.mkdir(exist_ok=True)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        data = response.read()
    file_md5 = hashlib.md5(data).hexdigest()
    suffix = ".flac" if ".flac" in url.lower() else ".mp3"
    file_path = CACHE_DIR / f"source_test_{int(time.time())}_{file_md5[:8]}{suffix}"
    file_path.write_bytes(data)
    return len(data), file_md5, file_path


def find_channel_audio_target(extra_ports: list[int]) -> dict:
    ports = list(dict.fromkeys(extra_ports + DEFAULT_PORTS))
    for target in scan_targets(ports):
        client = None
        try:
            client = DevToolsClient(target["webSocketDebuggerUrl"])
            probe = client.evaluate("(() => ({href: location.href, title: document.title, hasAudio: !!window.YYAudioPlayer}))()")
            if probe.get("hasAudio") and "base.c.yy.com" in probe.get("href", ""):
                return target
        except Exception:
            pass
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
    raise YYAudioSourceError("未找到带 YYAudioPlayer 的 YY 频道页，请确认 YY 已进入频道。")


def play_in_yy(target: dict, song: dict) -> dict:
    client = DevToolsClient(target["webSocketDebuggerUrl"])
    client.ws.settimeout(60)
    try:
        expression = """
(async () => {
  const player = window.YYAudioPlayer;
  const song = __SONG__;
  const timeout = (ms, label) => new Promise(resolve => setTimeout(() => resolve({__timeout: label}), ms));
  const call = async (label, fn, ms = 3000) => {
    try {
      const value = fn();
      if (value && typeof value.then === 'function') return await Promise.race([value, timeout(ms, label)]);
      return value;
    } catch (e) { return {__error: String(e)}; }
  };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const snap = async () => ({
    list: await call('getPlayList', () => player.getPlayList(), 1000),
    playId: await call('getPlayId', () => player.getPlayId(), 1000),
    playing: await call('getPlaying', () => player.getPlaying(), 1000),
    playTime: await call('getPlayTime', () => player.getPlayTime(), 1000),
    totalTime: await call('getTotalTime', () => player.getTotalTime(), 1000),
    mute: await call('getMute', () => player.getMute(), 1000),
    volume: await call('getVolume', () => player.getVolume(), 1000),
    localVolume: await call('getLocalVolume', () => player.getLocalVolume(), 1000),
  });
  const events = [];
  const logic = window.__yyAudioSourceProbeLogic = {events};
  const webInitResult = await call('webInit', () => player.webInit(logic), 2000);
  for (const key of ['sig_playState', 'sig_playTime', 'sig_downloadFinished', 'sig_downloadingSize', 'sig_downloadState']) {
    try {
      if (player[key] && typeof player[key].addListener === 'function') {
        player[key].addListener((...args) => events.push({signal: key, args, at: Date.now()}));
      }
    } catch (e) {
      events.push({signal: key, error: String(e)});
    }
  }
  await call('stop', () => player.stop(), 1500);
  await call('clearList', () => player.clearList(), 1500);
  await call('setMute', () => player.setMute(false), 1000);
  await call('setVolume', () => player.setVolume(100), 1000);
  await call('setLocalVolume', () => player.setLocalVolume(100), 1000);
  await wait(300);
  const addResult = await call('addPlayList', () => player.addPlayList(song), 2000);
  const afterAdd = await snap();
  const playResult = await call('play', () => player.play(song.id), 3000);
  const samples = [];
  for (let i = 0; i < 8; i += 1) {
    await wait(1000);
    samples.push(await snap());
  }
  const afterPlay = samples[samples.length - 1] || await snap();
  return {href: location.href, title: document.title, song, webInitResult, addResult, afterAdd, playResult, afterPlay, samples, events};
})()
""".replace("__SONG__", json.dumps(song, ensure_ascii=False))
        return client.evaluate(expression, await_promise=True)
    finally:
        client.close()


def build_song(name: str, singer: str, url: str, file_size: int, file_md5: str, file_path: Path) -> dict:
    return {
        "id": int(time.time() * 1000) % 1000000000,
        "title": name,
        "singer": singer,
        "filePath": file_path.as_posix(),
        "fileUrl": url,
        "fileMd5": file_md5,
        "fileSize": file_size,
        "totalTime": 0,
        "accompaniment": 1,
    }


def main():
    parser = argparse.ArgumentParser(description="测试 JS 音源脚本 + YYAudioPlayer 内置播放器播放。")
    parser.add_argument("keyword", nargs="?", default="晴天", help="歌曲名，仅用于展示和默认测试。默认：晴天")
    parser.add_argument("--singer", default="周杰伦", help="歌手名，默认：周杰伦")
    parser.add_argument("--song-id", default="0039MnYb0qxYhV", help="音源歌曲 ID/songmid/hash，默认是 QQ 音乐《晴天》songmid")
    parser.add_argument("--source", default="tx", choices=["wy", "tx", "kw", "kg", "mg", "qsvip"], help="音源平台，默认：tx")
    parser.add_argument("--quality", default="128k", help="音质，默认：128k")
    parser.add_argument("--script", type=Path, default=DEFAULT_SOURCE_SCRIPT, help="Lx Music 音源 JS 脚本路径")
    parser.add_argument("--port", type=int, action="append", default=[], help="额外指定 YY DevTools 端口，可重复传入")
    args = parser.parse_args()

    if not args.script.exists():
        raise SystemExit(f"音源脚本不存在：{args.script}")

    print(f"音源脚本：{args.script}")
    print(f"解析歌曲：{args.keyword} / {args.singer} / {args.source} / {args.quality}")
    url = run_node_source(args.script, args.source, args.song_id, args.keyword, args.singer, args.quality)
    print(f"播放 URL：{url}")

    file_size, file_md5, file_path = download_probe(url)
    print(f"下载校验：{file_size} bytes, md5={file_md5}")

    target = find_channel_audio_target(args.port)
    print(f"YY 页面：port={target.get('port')} title={target.get('title')} url={target.get('url')}")

    song = build_song(args.keyword, args.singer, url, file_size, file_md5, file_path)
    result = play_in_yy(target, song)
    samples = result.get("samples") or []
    play_times = [sample.get("playTime") for sample in samples if isinstance(sample.get("playTime"), (int, float))]
    advanced = bool(play_times and max(play_times) > min(play_times))
    after_play = result.get("afterPlay") or {}
    print(json.dumps({
        "ok": advanced,
        "playing": after_play.get("playing"),
        "playTimeAdvanced": advanced,
        "playTimes": play_times,
        "totalTime": after_play.get("totalTime"),
        "playId": after_play.get("playId"),
        "playlist": after_play.get("list"),
        "events": result.get("events"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except YYAudioSourceError as exc:
        raise SystemExit(str(exc)) from exc
