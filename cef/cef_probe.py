import argparse
import json
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

try:
    import websocket
except ImportError as exc:
    raise SystemExit("缺少 websocket-client，请先安装：pip install websocket-client") from exc


HTTP_TIMEOUT = 0.25
WS_TIMEOUT = 3
DEFAULT_PORTS = (33395, 38980, 30796, 39007)
KEYWORDS = ("yy", "channel", "room", "sess", "cef", "chat", "mic", "guild", "live", "nav", "enter", "join", "switch", "goto", "open")
MODULE_CANDIDATES = (
    "YY.Channel.ChannelMessage",
    "YY.Channel.ChannelInfo",
    "YY.Channel.ChannelUser",
    "YY.Channel.ChannelBase",
    "YY.Channel.ChannelMain",
    "YY.Channel.ChannelMic",
    "YY.Channel.ChannelMicList",
    "YY.Channel.ChannelNavigate",
    "YY.Channel.ChannelNavigation",
    "YY.Channel.ChannelSession",
    "YY.Channel.ChannelRoom",
    "YY.Channel.EnterChannel",
    "YY.Channel.JoinChannel",
    "YY.Channel.SwitchChannel",
    "YY.Channel.OpenChannel",
    "YY.Channel.GotoChannel",
    "YY.Client.Main",
    "YY.Client.Navigate",
    "YY.Client.Navigation",
    "YY.Client.Channel",
    "YY.Room.Main",
    "YY.Room.Channel",
    "YY.Room.Navigation",
    "YY.Chat.ChannelMessage",
)


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
            result = message.get("result", {})
            if "exceptionDetails" in result:
                details = result["exceptionDetails"]
                raise RuntimeError(json.dumps(details, ensure_ascii=False))
            return result.get("result", {}).get("value")


def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_candidate_ports(extra_ports: list[int]) -> list[int]:
    ports: set[int] = set(DEFAULT_PORTS) | set(extra_ports)
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


def scan_targets(extra_ports: list[int]) -> list[dict[str, Any]]:
    targets = []
    seen: set[tuple[int, str]] = set()
    for port in get_candidate_ports(extra_ports):
        if not is_port_open(port):
            continue
        try:
            items = fetch_json(f"http://127.0.0.1:{port}/json/list")
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            websocket_url = item.get("webSocketDebuggerUrl")
            if not websocket_url:
                continue
            key = (port, websocket_url)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "port": port,
                "id": item.get("id"),
                "type": item.get("type"),
                "title": item.get("title"),
                "url": item.get("url"),
                "webSocketDebuggerUrl": websocket_url,
            })
    return targets


def js_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def probe_expression(module_candidates: tuple[str, ...], include_modules: bool) -> str:
    return f"""
(() => {{
  const keywords = {js_string(KEYWORDS)};
  const moduleCandidates = {js_string(module_candidates)};
  const includeModules = {str(include_modules).lower()};
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2 || null;
  const yy = api && api._yy || null;
  const ownNames = (obj) => {{
    if (!obj) return [];
    try {{ return Object.getOwnPropertyNames(obj); }} catch (e) {{ return []; }}
  }};
  const valueInfo = (obj, key) => {{
    let value;
    try {{ value = obj[key]; }} catch (e) {{ return {{ name: key, error: String(e) }}; }}
    const type = typeof value;
    const info = {{ name: key, type }};
    if (value === null) info.isNull = true;
    if (type === 'object' || type === 'function') {{
      try {{ info.ctor = value && value.constructor && value.constructor.name || ''; }} catch (e) {{}}
      try {{ info.keys = ownNames(value).filter(k => keywords.some(w => k.toLowerCase().includes(w))).slice(0, 80); }} catch (e) {{}}
    }}
    if (type === 'string' || type === 'number' || type === 'boolean') info.value = value;
    return info;
  }};
  const keywordProps = (obj) => ownNames(obj)
    .filter(k => keywords.some(w => k.toLowerCase().includes(w)))
    .slice(0, 120)
    .map(k => valueInfo(obj, k));
  const methodNames = (obj) => {{
    if (!obj) return [];
    const names = new Set();
    let current = obj;
    for (let depth = 0; current && depth < 3; depth += 1) {{
      for (const name of ownNames(current)) {{
        if (name === 'constructor') continue;
        try {{
          if (typeof current[name] === 'function') names.add(name);
        }} catch (e) {{}}
      }}
      try {{ current = Object.getPrototypeOf(current); }} catch (e) {{ break; }}
    }}
    return Array.from(names).sort().slice(0, 120);
  }};
  const safeGet = (fn) => {{ try {{ return fn(); }} catch (e) {{ return {{ error: String(e) }}; }} }};
  const functionInfo = (fn) => {{
    if (typeof fn !== 'function') return null;
    let source = '';
    try {{ source = Function.prototype.toString.call(fn).slice(0, 1200); }} catch (e) {{ source = String(e); }}
    return {{ length: fn.length, name: fn.name || '', source }};
  }};
  const objectSnapshot = (obj) => {{
    if (!obj) return null;
    const out = {{}};
    for (const key of ownNames(obj).slice(0, 80)) {{
      let value;
      try {{ value = obj[key]; }} catch (e) {{ out[key] = {{ error: String(e) }}; continue; }}
      const type = typeof value;
      if (value === null || type === 'string' || type === 'number' || type === 'boolean' || type === 'undefined') {{
        out[key] = value;
      }} else if (type === 'function') {{
        out[key] = functionInfo(value);
      }} else {{
        out[key] = {{ type, ctor: value && value.constructor && value.constructor.name || '', keys: ownNames(value).slice(0, 80) }};
      }}
    }}
    return out;
  }};
  const modules = [];
  if (includeModules && yy && yy.chat && yy.chat.cef && typeof yy.chat.cef.getModule === 'function') {{
    for (const name of moduleCandidates) {{
      try {{
        const module = yy.chat.cef.getModule(name);
        if (module && !module.isNull) {{
          modules.push({{ name, isNull: !!module.isNull, keys: ownNames(module).slice(0, 80), methods: methodNames(module) }});
        }}
      }} catch (e) {{
        modules.push({{ name, error: String(e) }});
      }}
    }}
  }}
  return {{
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    userAgent: navigator.userAgent,
    currentChannelSessId: String(window.CurrentChannelSessId || ''),
    hasHdyyapv2: !!window.hdyyapv2,
    hasMFApiImplYyapiPcV2: !!window.MFApiImpl_yyapi_pcV2,
    hasApi: !!api,
    hasYY: !!yy,
    yyLoginUid: yy && yy.loginUid || '',
    windowKeywordProps: keywordProps(window),
    apiKeywordProps: keywordProps(api),
    yyKeywordProps: keywordProps(yy),
    yyMethodNames: methodNames(yy),
    channelLikeObjects: {{
      yyChannel: safeGet(() => yy && yy.channel ? {{ keys: ownNames(yy.channel), methods: methodNames(yy.channel) }} : null),
      yyRoom: safeGet(() => yy && yy.room ? {{ keys: ownNames(yy.room), methods: methodNames(yy.room) }} : null),
      yyChat: safeGet(() => yy && yy.chat ? {{ keys: ownNames(yy.chat), methods: methodNames(yy.chat) }} : null),
      yyChatCef: safeGet(() => yy && yy.chat && yy.chat.cef ? {{ keys: ownNames(yy.chat.cef), methods: methodNames(yy.chat.cef) }} : null),
      yyClient: safeGet(() => yy && yy.client ? {{ keys: ownNames(yy.client), methods: methodNames(yy.client) }} : null),
      yyNav: safeGet(() => yy && yy.nav ? {{ keys: ownNames(yy.nav), methods: methodNames(yy.nav) }} : null),
      yyNavigation: safeGet(() => yy && yy.navigation ? {{ keys: ownNames(yy.navigation), methods: methodNames(yy.navigation) }} : null)
    }},
    deepChannel: safeGet(() => yy && yy.channel ? {{
      args: objectSnapshot(yy.channel.args),
      channelInfo: objectSnapshot(yy.channel.channelInfo),
      cef: objectSnapshot(yy.channel.cef),
      joinChannel: functionInfo(yy.channel.joinChannel),
      leaveChannel: functionInfo(yy.channel.leaveChannel),
      call: functionInfo(yy.channel.call),
      callMethod: functionInfo(yy.channel.callMethod),
      callMethodAsync: functionInfo(yy.channel.callMethodAsync),
      getChannelInfo: functionInfo(yy.channel.getChannelInfo)
    }} : null),
    deepYY: safeGet(() => yy ? {{
      init: functionInfo(yy.init),
      awaitReady: functionInfo(yy.awaitReady),
      getChannel: functionInfo(yy.getChannel),
      getCef: functionInfo(yy.getCef),
      props: objectSnapshot(yy)
    }} : null),
    modules,
    hookCalls: window.__YY_CEF_PROBE__ && window.__YY_CEF_PROBE__.calls || [],
    hookModules: window.__YY_CEF_PROBE__ && window.__YY_CEF_PROBE__.modules || []
  }};
}})()
"""


def hook_expression() -> str:
    return r"""
(() => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2 || null;
  const yy = api && api._yy || null;
  if (!window.__YY_CEF_PROBE__) window.__YY_CEF_PROBE__ = { calls: [], modules: [], installed: false, installedAt: '' };
  const state = window.__YY_CEF_PROBE__;
  if (!yy) return { installed: false, waitingYY: true };
  if (state.installed) return { installed: true, already: true, installedAt: state.installedAt };
  const simplify = (value) => {
    const type = typeof value;
    if (value === null || type === 'string' || type === 'number' || type === 'boolean') return value;
    if (Array.isArray(value)) return value.slice(0, 8).map(simplify);
    if (type === 'object') {
      const out = {};
      for (const key of Object.keys(value).slice(0, 12)) {
        try { out[key] = simplify(value[key]); } catch (e) { out[key] = String(e); }
      }
      return out;
    }
    return `[${type}]`;
  };
  const record = (path, args) => {
    state.calls.push({ time: new Date().toISOString(), path, args: Array.from(args).map(simplify) });
    if (state.calls.length > 300) state.calls.splice(0, state.calls.length - 300);
  };
  const wrap = (obj, name, path) => {
    if (!obj || typeof obj[name] !== 'function' || obj[name].__yyCefProbeWrapped) return false;
    const original = obj[name];
    const wrapped = function(...args) {
      record(path, args);
      return original.apply(this, args);
    };
    wrapped.__yyCefProbeWrapped = true;
    try { obj[name] = wrapped; return true; } catch (e) { return false; }
  };
  const walk = (obj, path, depth, seen) => {
    if (!obj || depth > 2 || seen.has(obj)) return;
    seen.add(obj);
    let names = [];
    try { names = Object.getOwnPropertyNames(obj); } catch (e) { return; }
    for (const name of names) {
      const lower = name.toLowerCase();
      const interesting = /channel|room|sess|enter|join|switch|goto|open|nav|jump|login|client/.test(lower);
      let value;
      try { value = obj[name]; } catch (e) { continue; }
      if (typeof value === 'function' && interesting) wrap(obj, name, `${path}.${name}`);
      if (value && typeof value === 'object' && interesting) walk(value, `${path}.${name}`, depth + 1, seen);
    }
  };
  if (yy && yy.chat && yy.chat.cef && typeof yy.chat.cef.getModule === 'function' && !yy.chat.cef.getModule.__yyCefProbeWrapped) {
    const originalGetModule = yy.chat.cef.getModule;
    yy.chat.cef.getModule = function(name, ...args) {
      state.modules.push({ time: new Date().toISOString(), name: String(name) });
      if (state.modules.length > 300) state.modules.splice(0, state.modules.length - 300);
      return originalGetModule.call(this, name, ...args);
    };
    yy.chat.cef.getModule.__yyCefProbeWrapped = true;
  }
  walk(yy, 'yy', 0, new WeakSet());
  walk(api, 'api', 0, new WeakSet());
  state.installed = true;
  state.installedAt = new Date().toISOString();
  return { installed: true, already: false, installedAt: state.installedAt };
})()
"""


def target_key(target: dict[str, Any]) -> str:
    return f"{target.get('port')}:{target.get('id') or target.get('webSocketDebuggerUrl')}"


def should_probe(target: dict[str, Any], probe_all: bool) -> bool:
    if probe_all:
        return True
    url = str(target.get("url") or "").lower()
    title = str(target.get("title") or "").lower()
    return "yy.com" in url or "yy" in title or url.startswith("devtools://") is False


def snapshot_target(target: dict[str, Any], args) -> dict[str, Any]:
    item = {"target": target, "probe": None, "error": None}
    client = None
    try:
        client = DevToolsClient(target["webSocketDebuggerUrl"])
        if args.hooks:
            item["hookInstall"] = client.evaluate(hook_expression())
        item["probe"] = client.evaluate(probe_expression(MODULE_CANDIDATES, not args.no_modules))
    except Exception as exc:
        item["error"] = str(exc)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
    return item


def compact_state(item: dict[str, Any]) -> dict[str, Any]:
    target = item.get("target") or {}
    probe = item.get("probe") or {}
    modules = probe.get("modules") if isinstance(probe, dict) else []
    return {
        "port": target.get("port"),
        "id": target.get("id"),
        "title": target.get("title"),
        "url": target.get("url"),
        "href": probe.get("href") if isinstance(probe, dict) else None,
        "session": probe.get("currentChannelSessId") if isinstance(probe, dict) else None,
        "uid": probe.get("yyLoginUid") if isinstance(probe, dict) else None,
        "hasYY": probe.get("hasYY") if isinstance(probe, dict) else None,
        "modules": [m.get("name") for m in modules if isinstance(m, dict) and not m.get("error")],
        "hookCallCount": len(probe.get("hookCalls") or []) if isinstance(probe, dict) else 0,
        "hookModuleCount": len(probe.get("hookModules") or []) if isinstance(probe, dict) else 0,
        "error": item.get("error"),
    }


def print_changes(previous: dict[str, str], snapshots: list[dict[str, Any]]):
    for item in snapshots:
        key = target_key(item.get("target") or {})
        compact = compact_state(item)
        text = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if previous.get(key) == text:
            continue
        previous[key] = text
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 变化 {key}")
        print(json.dumps(compact, ensure_ascii=False, indent=2))


def run(args):
    out_path = Path(args.out)
    previous: dict[str, str] = {}
    started = time.monotonic()
    print(f"开始探测 YY CEF，日志写入：{out_path}")
    print("请现在从未进入频道状态开始操作 YY，输入频道号并进入频道。按 Ctrl+C 停止。")
    with out_path.open("a", encoding="utf-8") as output:
        try:
            while True:
                targets = [target for target in scan_targets(args.port) if should_probe(target, args.all_targets)]
                snapshots = [snapshot_target(target, args) for target in targets]
                record = {
                    "time": datetime.now().isoformat(timespec="milliseconds"),
                    "targets": snapshots,
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                print_changes(previous, snapshots)
                if args.duration and time.monotonic() - started >= args.duration:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("已停止探测。")


def main():
    parser = argparse.ArgumentParser(description="YY CEF 频道切换探测脚本。")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔秒数，默认 1.0")
    parser.add_argument("--duration", type=float, default=0, help="运行秒数，默认一直运行")
    parser.add_argument("--out", default="yy-cef-lx-bot/cef_probe_log.jsonl", help="JSONL 日志路径")
    parser.add_argument("--port", type=int, action="append", default=[], help="额外指定 DevTools 端口，可重复传入")
    parser.add_argument("--all-targets", action="store_true", help="探测所有 DevTools target，默认优先探测 YY 相关页面")
    parser.add_argument("--no-modules", action="store_true", help="不主动尝试 getModule 候选模块")
    parser.add_argument("--hooks", action="store_true", help="注入轻量 hook，记录 channel/room/navigation 相关函数调用")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
