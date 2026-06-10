import argparse
import json
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from itertools import count
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

try:
    import websocket
except ImportError as exc:
    raise SystemExit("缺少 websocket-client，请先安装：pip install websocket-client") from exc


DEBUG_PORT_START = 30000
DEBUG_PORT_END = 45000
HTTP_TIMEOUT = 0.25
WS_TIMEOUT = 5


@dataclass
class CefPage:
    port: int
    title: str
    url: str
    websocket_url: str


class DevToolsClient:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
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

            result = message.get("result", {}).get("result", {})
            return result.get("value")


def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_candidate_ports() -> list[int]:
    ports: set[int] = set()

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

    for port in (33395, 38980, 30796, 39007):
        ports.add(port)

    return sorted(ports)


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
        if not websocket_url:
            continue
        pages.append(CefPage(
            port=port,
            title=target.get("title", ""),
            url=target.get("url", ""),
            websocket_url=websocket_url,
        ))
    return pages


def scan_pages(full_scan: bool = False) -> list[CefPage]:
    pages: list[CefPage] = []
    seen: set[tuple[int, str]] = set()

    candidate_ports = get_candidate_ports()
    if full_scan:
        candidate_ports.extend(range(DEBUG_PORT_START, DEBUG_PORT_END + 1))

    for port in dict.fromkeys(candidate_ports):
        if not is_port_open(port):
            continue

        for page in pages_from_port(port):
            key = (page.port, page.websocket_url)
            if key in seen:
                continue
            seen.add(key)
            pages.append(page)

    return pages


def find_channel_page() -> CefPage:
    pages = scan_pages()
    preferred = [page for page in pages if page.url.rstrip("/") == "https://base.c.yy.com"]
    if preferred:
        return preferred[0]

    candidates = [page for page in pages if "YYCefChannel" in page.title or "base.c.yy.com" in page.url]
    if candidates:
        return candidates[0]

    yy_pages = [page for page in pages if "yy.com" in page.url]
    if yy_pages:
        return yy_pages[0]

    raise SystemExit("未找到 YY CEF 页面。请确认 YY 客户端已进入频道，并且 CEF 调试端口存在。")


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

    if repaired.count("�") <= text.count("�"):
        return repaired
    return text


def message_to_row(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("senderProp") or {}
    composite = message.get("compositeMsg") or []
    composite_text = "".join(normalize_text(item.get("data", "")) for item in composite if isinstance(item, dict))
    full_text = normalize_text(message.get("fullText") or message.get("textMsg") or composite_text)
    nick = normalize_text(sender.get("nick"))

    return {
        "msgType": message.get("msgType"),
        "uid": message.get("uid") or sender.get("uid"),
        "imid": sender.get("imid"),
        "nick": nick,
        "text": full_text,
        "textUUID": sender.get("textUUID"),
        "timestamp": sender.get("textMICROSECOND_TIMESTAMP"),
        "isSelfSend": bool(sender.get("Send")),
    }


def get_status(client: DevToolsClient) -> Any:
    return client.evaluate(r"""
(() => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  const module = yy && yy.chat && yy.chat.cef && yy.chat.cef.getModule('YY.Channel.ChannelMessage');
  return {
    href: location.href,
    title: document.title,
    userAgent: navigator.userAgent,
    CurrentChannelSessId: window.CurrentChannelSessId,
    hasApi: !!api,
    hasYY: !!yy,
    hasChannelMessage: !!module && !module.isNull,
    loginUid: yy && yy.loginUid,
    ready: yy ? {
      isLoginSuccess: yy.isLoginSuccess,
      isJoinChannelSuccess: yy.isJoinChannelSuccess,
      isTransferReady: yy.isTransferReady,
      isAllReady: yy.isAllReady,
      isChannelInfoReady: yy.isChannelInfoReady,
      sid: yy.sid,
      ssid: yy.ssid,
      asid: yy.asid
    } : null,
    chatMethods: yy && yy.chat ? Object.getOwnPropertyNames(Object.getPrototypeOf(yy.chat)).filter(k => /message|chat|send|listen|receive|recv/i.test(k)) : []
  };
})()
""")


def get_cache_messages(client: DevToolsClient) -> list[dict[str, Any]]:
    messages = client.evaluate(r"""
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


def send_public_message(client: DevToolsClient, text: str) -> Any:
    expression = """
((text) => {
  const api = window.hdyyapv2 || window.MFApiImpl_yyapi_pcV2;
  const yy = api && api._yy;
  if (!yy || !yy.chat || typeof yy.chat.sendPublicMessage !== 'function') {
    throw new Error('yy.chat.sendPublicMessage 不可用');
  }
  const result = yy.chat.sendPublicMessage(text);
  return { ok: true, result: result == null ? null : String(result) };
})(%s)
""" % json.dumps(text, ensure_ascii=False)
    return client.evaluate(expression)


def print_page(page: CefPage):
    print(f"端口: {page.port}")
    print(f"标题: {normalize_text(page.title)}")
    print(f"地址: {page.url}")
    print(f"调试: {page.websocket_url}")


def command_list(args):
    pages = scan_pages(args.full_scan)
    if not pages:
        print("未发现 CEF DevTools 页面。")
        return

    for index, page in enumerate(pages, 1):
        print(f"[{index}]")
        print_page(page)
        print()


def open_client() -> tuple[CefPage, DevToolsClient]:
    page = find_channel_page()
    client = DevToolsClient(page.websocket_url)
    return page, client


def command_status(args):
    page, client = open_client()
    try:
        print_page(page)
        print()
        print(json.dumps(get_status(client), ensure_ascii=False, indent=2))
    finally:
        client.close()


def command_read(args):
    page, client = open_client()
    try:
        rows = get_cache_messages(client)
        rows = rows[-args.limit:]
        if args.raw_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        for row in rows:
            print(format_message(row))
    finally:
        client.close()


def command_watch(args):
    page, client = open_client()
    seen: set[str] = set()

    try:
        print("开始监听 YY 公屏缓存，按 Ctrl+C 退出。")
        print_page(page)
        print()

        while True:
            rows = get_cache_messages(client)
            for row in rows:
                message_id = row.get("textUUID") or f"{row.get('uid')}:{row.get('timestamp')}:{row.get('text')}"
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                if row.get("msgType") == 2:
                    print(format_message(row))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止监听。")
    finally:
        client.close()


def channel_status_snapshot(client: DevToolsClient) -> dict[str, Any]:
    status = get_status(client)
    ready = status.get("ready") or {}
    return {
        "CurrentChannelSessId": status.get("CurrentChannelSessId"),
        "href": status.get("href"),
        "title": status.get("title"),
        "sid": ready.get("sid"),
        "ssid": ready.get("ssid"),
        "asid": ready.get("asid"),
    }


def command_watch_channel(args):
    client = None
    last_snapshot = None

    try:
        print("开始观察频道状态变化，手动跳转频道后这里会打印变化，按 Ctrl+C 退出。")

        while True:
            if client is None:
                try:
                    page, client = open_client()
                except SystemExit as exception:
                    print(f"{exception} {args.interval} 秒后重试。")
                    time.sleep(args.interval)
                    continue
                except Exception as exception:
                    print(f"连接 YY CEF 页面失败: {exception}，{args.interval} 秒后重试。")
                    time.sleep(args.interval)
                    continue

                print_page(page)
                print()
                last_snapshot = None

            try:
                snapshot = channel_status_snapshot(client)
            except Exception as exception:
                print(f"CEF 连接已断开，正在重新查找页面: {exception}")
                try:
                    client.close()
                except Exception:
                    pass
                client = None
                time.sleep(args.interval)
                continue

            if snapshot != last_snapshot:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                print()
                last_snapshot = snapshot
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止观察频道状态。")
    finally:
        if client is not None:
            client.close()


def command_send(args):
    page, client = open_client()
    try:
        result = send_public_message(client, args.text)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


def format_message(row: dict[str, Any]) -> str:
    uid = row.get("uid") or ""
    imid = row.get("imid") or ""
    nick = row.get("nick") or ""
    text = row.get("text") or ""
    self_mark = " self" if row.get("isSelfSend") else ""
    return f"[{row.get('msgType')}{self_mark}] {nick} uid={uid} imid={imid}: {text}"


def main():
    parser = argparse.ArgumentParser(description="YY 客户端 CEF 内部 API 探针。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出本机 CEF DevTools 页面")
    list_parser.add_argument("--full-scan", action="store_true", help="扫描 30000-45000 全端口，较慢")

    subparsers.add_parser("status", help="检查 YY CEF 页面和内部 API 状态")

    read_parser = subparsers.add_parser("read", help="读取当前公屏缓存")
    read_parser.add_argument("--limit", type=int, default=20, help="显示最后 N 条，默认 20")
    read_parser.add_argument("--raw-json", action="store_true", help="输出原始 JSON 结构，便于排查编码和字段")

    watch_parser = subparsers.add_parser("watch", help="轮询公屏缓存并打印新消息")
    watch_parser.add_argument("--interval", type=float, default=0.5, help="轮询间隔秒数，默认 0.5")

    watch_channel_parser = subparsers.add_parser("watch-channel", help="轮询频道状态并打印变化")
    watch_channel_parser.add_argument("--interval", type=float, default=0.5, help="轮询间隔秒数，默认 0.5")

    send_parser = subparsers.add_parser("send", help="通过内部 API 发送公屏消息")
    send_parser.add_argument("text", help="要发送的文本")

    args = parser.parse_args()
    commands = {
        "list": command_list,
        "status": command_status,
        "read": command_read,
        "watch": command_watch,
        "watch-channel": command_watch_channel,
        "send": command_send,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
