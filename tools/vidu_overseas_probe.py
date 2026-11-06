"""vidu_overseas_probe.py —— 海外环境(api.vidu.com) Vidu 组件版 API 能力最小验证。

背景(2026-11-05): 公司切到海外版 Vidu,协议端点与国内《详细参数》逐字一致
(见 tools/TODO.md)。两个遗留问题必须**实测定案**,不靠兜底:

  Q1  上传通道二选一,明确用哪个、桥里只实现那一个(不写 fallback):
      方案A = 仓库现装 /tools/v2/files/uploads 三步式上传 → ssupload 短链
      方案B = image_uri 直接传 data:image/*;base64,<...> (海外文档明确支持)
      → 免费测试: `python3 tools/vidu_overseas_probe.py`(T4=方案A, T5=方案B)

  Q2  prompt_operation(type=11 参考图换装/手持物/换背景)海外文档无完整定义,
      只有 4 处规则性提及 → 协议认不认、字段对不对,只能连线实测。
      → 计费测试: `python3 tools/vidu_overseas_probe.py --live`
        (建一个 managed_rtc{} 会话 → conn_init → 发 type=11 → 盯回执/报错 → 挂断;
         按实测时长计 1 积分/秒,预计 < 1 分钟)

前置: config.json 里 vidu.api_key 已填海外 key(或用 --key 覆盖)。
输出: 每项 PASS/FAIL 一行 + 末尾「结论」块,直接抄进 TODO.md。
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONF = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

RESULTS: list[str] = []          # (T 编号, PASS/FAIL/ERR, 一句话)
UPSTREAM_IMG: bytes | None = None   # T4/T5 产物, 供 T6 复用
UPSTREAM_URI: str = ""              # 实测可达的 image_uri(哪个方案成了用哪个)


def _hdr(api_key: str) -> dict:
    return {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}


def _rec(tag: str, ok: bool | None, msg: str) -> None:
    verdict = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    RESULTS.append((tag, verdict, msg))
    print(f"  [{tag}] {verdict}  {msg}", flush=True)


def enable_doh(hostnames: tuple[str, ...]) -> None:
    """显式开关(--doh): 本机 UDP DNS 解析不了 vidu.com 时, 用 1.1.1.1 DoH 查 A 记录,
    劫持 socket.getaddrinfo 强制走已解析 IP(TTL 60s, 每次运行现查)。
    只影响本脚本进程, 不写系统 hosts。"""
    import socket
    import urllib.request
    orig = socket.getaddrinfo
    resolved: dict[str, tuple] = {}
    for name in hostnames:
        q = urllib.request.Request(
            f"https://1.1.1.1/dns-query?name={name}&type=A",
            headers={"accept": "application/dns-json"})
        d = json.loads(urllib.request.urlopen(q, timeout=15).read())
        ips = tuple(a["data"] for a in d.get("Answer", []) if a.get("type") == 1)
        if not ips:
            print(f"  [doh] {name} 无 A 记录, 保持系统解析", flush=True)
            continue
        resolved[name] = ips
        print(f"  [doh] {name} → {list(ips)}", flush=True)
    if not resolved:
        return

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        h = str(host or "")
        for name, ips in resolved.items():
            if h == name or h.endswith("." + name):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port or 443)))
                        for ip in ips]
        return orig(host, port, family, type, proto, flags)
    socket.getaddrinfo = patched   # websockets/asyncio 发起连接时同样经过这里
    return


def tiny_png() -> bytes:
    """64x64 纯色 PNG(标准库 zlib 手搓, 无 PIL 依赖)。"""
    import struct
    import zlib as zz

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + \
            struct.pack(">I", zz.crc32(tag + data) & 0xFFFFFFFF)

    w, h = 64, 64
    raw = b"".join(b"\x00" + b"\x88\xaa\xff" * w for _ in range(h))   # filter0 + RGB
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zz.compress(raw)) + chunk(b"IEND", b""))


def b64_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def mask_key(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else "…"


# ────────────────────────── 免费项 ──────────────────────────

def t1_credits(c: httpx.AsyncClient, key: str) -> None:
    """鉴权 + 余额 + host 可达性。"""
    r = c.get("https://api.vidu.com/ent/v2/credits", headers=_hdr(key),
              params={"show_detail": "false"}, timeout=15)
    if r.status_code != 200:
        _rec("T1", False, f"GET /ent/v2/credits → HTTP {r.status_code}: {r.text[:200]}")
        return
    d = r.json()
    total = sum(int(p.get("credit_remain") or 0)
                for p in (d.get("remains") or d.get("remaining_credits") or []))
    _rec("T1", True, f"鉴权 OK, 余额 ≈ {total} credits (host=api.vidu.com)")


def t2_action_libs(c: httpx.AsyncClient, key: str) -> None:
    r = c.get("https://api.vidu.com/live/v1/action-libraries", headers=_hdr(key),
              params={"pager.page": 0, "pager.pagesz": 20}, timeout=15)
    n = -1
    if r.status_code == 200:
        item = r.json().get("items") or r.json().get("data") or r.json()
        n = len(item) if isinstance(item, list) else -1
    _rec("T2", r.status_code == 200, f"GET action-libraries → HTTP {r.status_code}"
         + (f", {n} 个库" if n >= 0 else f", body: {r.text[:200]}"))


def t3_avatars(c: httpx.AsyncClient, key: str) -> None:
    r = c.get("https://api.vidu.com/live/v1/avatars", headers=_hdr(key),
              params={"pager.page": 0, "pager.pagesz": 20}, timeout=15)
    n = -1
    if r.status_code == 200:
        item = r.json().get("items") or r.json().get("data") or r.json()
        n = len(item) if isinstance(item, list) else -1
    _rec("T3", r.status_code == 200, f"GET avatars → HTTP {r.status_code}"
         + (f", {n} 个形象资产" if n >= 0 else f", body: {r.text[:200]}"))


def t4_three_step_upload(c: httpx.AsyncClient, key: str) -> dict:
    """方案A: 仓库现装的 /tools/v2/files/uploads 三步式, 海外通不通。"""
    global UPSTREAM_IMG
    UPSTREAM_IMG = tiny_png()
    # 1) 预创建
    r = c.post("https://api.vidu.com/tools/v2/files/uploads", headers=_hdr(key),
               json={"scene": "vidu"}, timeout=30)
    if r.status_code != 200:
        _rec("T4", False, f"方案A 第1步(POST) → HTTP {r.status_code}: {r.text[:200]}")
        return {}
    sess = r.json()
    fid, put_url = sess.get("id"), sess.get("put_url")
    if not (fid and put_url):
        _rec("T4", False, f"方案A 第1步响应缺 id/put_url: {str(sess)[:200]}")
        return {}
    # 2) 直传对象存储(不带鉴权头)
    r2 = c.put(put_url, content=UPSTREAM_IMG,
               headers={"Content-Type": "image/png"}, timeout=60)
    if r2.status_code >= 400:
        _rec("T4", False, f"方案A 第2步(PUT) → HTTP {r2.status_code}")
        return {}
    etag = (r2.headers.get("etag") or "").strip('"')
    # 3) finish
    r3 = c.put(f"https://api.vidu.com/tools/v2/files/uploads/{fid}/finish",
               headers=_hdr(key), json={"etag": etag}, timeout=30)
    if r3.status_code >= 400:
        _rec("T4", False, f"方案A 第3步(finish) → HTTP {r3.status_code}: {r3.text[:200]}")
        return {}
    uri = r3.json().get("uri") or f"ssupload:?id={fid}"
    _rec("T4", True, f"方案A(三步上传)可用 → {uri[:60]}")
    return {"plan": "A", "uri": uri, "file_id": fid}


def t5_base64_avatar(c: httpx.AsyncClient, key: str) -> dict:
    """方案B: image_uri 直传 data URI, /live/v1/avatars 收不收(收完立即 delete 清场)。"""
    global UPSTREAM_IMG, UPSTREAM_URI
    data = UPSTREAM_IMG or tiny_png()
    aid = ""
    try:
        r = c.post("https://api.vidu.com/live/v1/avatars", headers=_hdr(key),
                   json={"image_uri": b64_uri(data), "name": "probe-t5(会自动删除)"},
                   timeout=60)
        if r.status_code != 200:
            _rec("T5", False, f"方案B base64 建形象 → HTTP {r.status_code}: {r.text[:200]}")
            return {}
        d = r.json()
        aid = str(d.get("id") or "")
        status = d.get("status")
        # HTTP 端收了 data URI = 格式通道成立; 图片内容可人(处理状态)不影响结论
        _rec("T5", True, f"方案B(base64 直传)格式通道成立 → avatar_id={aid} status={status}")
        return {"plan": "B", "avatar_id": aid}
    finally:
        if aid:
            rd = c.delete(f"https://api.vidu.com/live/v1/avatars/{aid}",
                          headers=_hdr(key), timeout=30)
            if rd.status_code < 300:
                print(f"  [T5] 已删除测试形象 {aid}", flush=True)
            else:
                print(f"  [T5] ⚠ 测试形象 {aid} 删除未确认(HTTP {rd.status_code})", flush=True)


# ────────────────────────── 计费项(--live) ──────────────────────────

def t6_prompt_operation(c: httpx.AsyncClient, key: str, conn_id: str = "probe-t6") -> None:
    """建真会话(managed_rtc {}) → conn_init → 发 type=11 → 盯回执/报错/断连。

    判定(与国内版行为对齐: prompt_operation 成功不回执, 失败才回错):
      - 收到 error/103/14 帧        → 明确失败(贴原始帧)
      - 15s 内无错误帧且连接还活着  → 协议层接受, 疑似支持(建议开舞台页肉眼确认画面变化)
      - WS 被服务端直接断           → 不支持/审核不通过(贴 close 原因)"""
    import asyncio
    import websockets

    global UPSTREAM_IMG, UPSTREAM_URI

    async def run() -> None:
        img = b64_uri(UPSTREAM_IMG or tiny_png())
        body = {"model": "vidu-s2", "image_uri": img,
                "managed_rtc": {}, "action": {"enabled": True, "library_id": "0"}}
        r = c.post("https://api.vidu.com/live/s_avatar/component", headers=_hdr(key),
                   json=body, timeout=120)
        if r.status_code != 200:
            _rec("T6", False, f"建会话失败 HTTP {r.status_code}: {r.text[:300]}")
            return
        live = (r.json().get("live") or {})
        live_id = str(live.get("id") or "")
        t0 = time.time()
        print(f"  [T6] 会话已创建 live_id={live_id} (开始计费 1积分/秒)…", flush=True)

        url = f"wss://api.vidu.com/live/v1/external-lives/{live_id}/stream?conn_id={conn_id}"
        async with websockets.connect(url, additional_headers={"Authorization": f"Token {key}"},
                                      ssl=__import__("ssl").create_default_context(),
                                      ping_interval=10, ping_timeout=20, max_size=None) as ws:
            # conn_init 到就绪
            seq = [0]
            ready = False
            for _ in range(15):
                seq[0] += 1
                await ws.send(json.dumps({"type": 1, "live_id": live_id,
                                          "conn_id": conn_id, "seq_id": seq[0],
                                          "payload": {"conn_init": {"version": 1}}}))
                try:
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    continue
                a = (ack.get("payload") or {}).get("conn_init_ack") or {}
                if a.get("success"):
                    ready = True
                    break
                if a.get("error_code") not in ("NOT_READY", ""):
                    _rec("T6", False, f"conn_init 失败: {a}")
                    return
            if not ready:
                _rec("T6", False, "conn_init 多次未就绪(15 次)")
                return
            print(f"  [T6] conn_init 就绪, 发 prompt_operation(type=11)…", flush=True)
            seq[0] += 1
            await ws.send(json.dumps({
                "type": 11, "live_id": live_id, "conn_id": conn_id, "seq_id": seq[0],
                "payload": {"prompt_operation": {
                    "op_type": "switch",
                    "images": [{"image_uri": img, "image_id": "probe-t6-img",
                                "user_text": "给数字人换上图中这件毛衣"}]}}}))
            # 盯 15s 回执
            outcome: list[str] = []
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                except websockets.ConnectionClosed as e:
                    outcome.append(f"WS被服务端关闭 code={getattr(e, 'code', '?')} "
                                   f"reason={getattr(e, 'reason', '')}")
                    break
                if isinstance(msg, bytes):
                    continue
                frame = json.loads(msg)
                t = frame.get("type")
                if t in (2, 6):
                    continue
                outcome.append(f"收到帧 type={t}: {json.dumps(frame.get('payload'), ensure_ascii=False)[:300]}")
                if t in (103, 14):        # 明确失败 → 不用再等
                    break
            if not outcome:
                outcome = ["15s 内无任何回执/错误帧, 连接仍存活 → 协议层接受(疑似支持, 建议肉眼确认画面)"]
            _rec("T6", None, "prompt_operation 实测: " + " | ".join(outcome))
            # 收尾: 挂断 + 计费时长
            try:
                seq[0] += 1
                await ws.send(json.dumps({"type": 5, "live_id": live_id, "conn_id": conn_id,
                                          "seq_id": seq[0],
                                          "payload": {"hangup": {"hangup_reason": "probe_done"}}}))
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    asyncio.run(run())

    # 顺手查会话终态 + 本次估费
    time.sleep(3)
    try:
        lr = c.get("https://api.vidu.com/ent/v2/credits", headers=_hdr(key),
                   params={"show_detail": "false"}, timeout=15)
        if lr.status_code == 200:
            d = lr.json()
            total = sum(int(p.get("credit_remain") or 0)
                        for p in (d.get("remains") or d.get("remaining_credits") or []))
            print(f"  [T6] 实测后余额 ≈ {total} credits", flush=True)
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────── main ──────────────────────────

def main() -> int:
    global UPSTREAM_URI
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", default="", help="vda_…(默认读 config.json vidu.api_key)")
    ap.add_argument("--live", action="store_true",
                    help="追加 T6 计费项(建真会话实测 prompt_operation, ~1积分/秒)")
    ap.add_argument("--doh", action="store_true",
                    help="本机 DNS 解析不了 vidu.com 时, 走 1.1.1.1 DoH 拿 IP 直连(仅本进程)")
    args = ap.parse_args()

    key = args.key or (CONF.get("vidu") or {}).get("api_key") or ""
    if not key or "在这里填" in key:
        print("没有可用的 vidu.api_key —— 把海外 key 填进 config.json 或 --key 传入")
        return 2
    host = (CONF.get("vidu") or {}).get("host") or "api.vidu.com"
    if args.doh:
        enable_doh(["api.vidu.com", host])
    print(f"目标 host: https://{host}   key: {mask_key(key)}\n")

    def finish() -> int:
        print("\n── 结论 ──")
        verdicts = {tag: v for tag, v, _ in RESULTS}
        planA = verdicts.get("T4") == "PASS"
        planB = verdicts.get("T5") == "PASS"
        if planA is True:
            print("上传通道决议: 采用【方案A】(现有三步上传, 桥不用改)")
        elif planB is True:
            print("上传通道决议: 采用【方案B】(base64 直传; 桥里 upload_bytes 相关路径要改为直传, 不留 fallback)")
        else:
            print("上传通道决议: 两个方案都没有 PASS —— 先看上面失败原因再决定, 别急着改桥")
        if "--live" not in " ".join(sys.argv):
            print("prompt_operation(type=11): 未实测 —— 计费项, 跑 `--live` 后回填结论")
        print()
        for tag, v, m in RESULTS:
            print(f"  {tag}  {v}  {m}")
        return 0

    with httpx.Client() as c:
        t1_credits(c, key)
        if verdicts := [r for r in RESULTS if r[0] == "T1" and r[1] == "FAIL"]:
            print("鉴权/连通失败, 后续测试无意义 —— 先修 key/host")
            return finish()
        t2_action_libs(c, key)
        t3_avatars(c, key)
        t4_three_step_upload(c, key)
        t5_base64_avatar(c, key)
        if args.live:
            t6_prompt_operation(c, key)
        return finish()


if __name__ == "__main__":
    sys.exit(main())
