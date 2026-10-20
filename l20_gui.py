#!/usr/bin/env python3
"""L20 左手控制面板。

在浏览器里看关节、点动、看五指触觉矩阵。

  python3 l20_gui.py
  python3 l20_gui.py --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from l20_left import FINGER_NAMES, JOINT_NAMES, L20

# 每指实际可控的关节。11–14 是协议预留，只显示不驱动。
FINGERS = (
    ("拇指", (0, 5, 10, 15)),
    ("食指", (1, 6, 16)),
    ("中指", (2, 7, 17)),
    ("无名指", (3, 8, 18)),
    ("小指", (4, 9, 19)),
)
RESERVED = (11, 12, 13, 14)


class HandService:
    """所有 CAN 访问都在这一条线程里，避免和网页请求抢总线。"""

    def __init__(self) -> None:
        self.hand: L20 | None = None
        self.commands: queue.Queue[dict] = queue.Queue(maxsize=4)
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state: dict = {
            "connected": False,
            "busy": False,
            "message": "正在连接…",
            "error": None,
            "can_id": None,
            "serial": "",
            "hardware": [],
            "software": [],
            "touch_type": None,
            "positions": [None] * 20,
            "matrices": {},
            "speed": 80,
        }
        self._thread = threading.Thread(target=self._loop, name="l20-can", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def submit(self, command: dict) -> bool:
        try:
            self.commands.put_nowait(command)
            return True
        except queue.Full:
            return False

    def request_stop(self) -> None:
        self.cancel.set()
        while not self.commands.empty():
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        self.cancel.set()
        self.stop.set()
        self._thread.join(timeout=3)
        if self.hand is not None:
            self.hand.close()

    def _update(self, **fields) -> None:
        with self.lock:
            self.state.update(fields)

    def _loop(self) -> None:
        try:
            hand = L20()
            self.hand = hand
            can_id = hand.discover()
            info = hand.device_info()
            touch = hand.touch_type()
            self._update(
                connected=True,
                message=f"已连接，CAN ID 0x{can_id:02X}",
                error=None,
                can_id=can_id,
                serial=info["serial"],
                hardware=info["hardware"],
                software=info["software"],
                touch_type=touch,
            )
        except Exception as exc:
            self._update(connected=False, error=str(exc), message="连接失败")
            return

        last_touch = 0.0
        while not self.stop.is_set():
            command = None
            try:
                command = self.commands.get(timeout=0.02)
            except queue.Empty:
                pass
            if command is not None:
                self._run(command)
                continue
            try:
                positions = self.hand.read_positions()
                self._update(positions=positions, error=None)
                now = time.time()
                if now - last_touch > 0.8 and self.commands.empty():
                    self._read_touch()
                    last_touch = now
            except Exception as exc:
                self._update(error=str(exc))
                time.sleep(0.3)

    def _read_touch(self) -> None:
        kind = self.state["touch_type"]
        if kind == 2:
            matrices = self.hand.read_matrix(wait=0.35)
            # 这一轮没到的格子沿用上次的值，避免热力图闪成空的。
            previous = self.state["matrices"]
            for name, rows in matrices.items():
                old_rows = previous.get(name)
                if not old_rows:
                    continue
                for r, row in enumerate(rows):
                    for c, value in enumerate(row):
                        if value < 0 and r < len(old_rows):
                            row[c] = old_rows[r][c]
            self._update(matrices=matrices)
        elif kind == 1:
            self._update(
                force={
                    "normal": self.hand.read_vector(0x20),
                    "tangential": self.hand.read_vector(0x21),
                    "direction": self.hand.read_vector(0x22),
                    "approach": self.hand.read_vector(0x23),
                }
            )

    def _run(self, command: dict) -> None:
        self.cancel.clear()
        self._update(busy=True)
        try:
            index = int(command["index"])
            if index in RESERVED:
                self._update(message=f"关节 {index} 是协议预留，不下发运动")
                return
            speed = max(1, min(255, int(command.get("speed", 80))))
            self._update(speed=speed)
            if command["op"] == "jog":
                current = self.state["positions"][index]
                if current is None:
                    self._update(message=f"读不到{JOINT_NAMES[index]}")
                    return
                delta = int(command["delta"])
                if abs(delta) > 40:
                    self._update(message="单次点动限制在 ±40")
                    return
                target = max(0, min(255, current + delta))
            else:
                target = max(0, min(255, int(command["target"])))
            before, after = self.hand.move_joint(
                index,
                target,
                speed=speed,
                should_stop=self.cancel.is_set,
            )
            note = "" if abs(after - target) <= 2 else "，未到目标"
            if self.cancel.is_set():
                note = "，已停止"
            self._update(message=f"{JOINT_NAMES[index]} {before} → {after}{note}")
            positions = list(self.state["positions"])
            positions[index] = after
            self._update(positions=positions)
        except Exception as exc:
            self._update(message="运动失败", error=str(exc))
        finally:
            self._update(busy=False)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>L20 左手</title>
<style>
  :root {
    --bg: #efeae2;
    --ink: #1d1a16;
    --card: #f7f4ee;
    --line: #d9d1c5;
    --accent: #9a3412;
    --ok: #1f6b4a;
    --muted: #6d655c;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans SC", sans-serif;
    background: var(--bg);
    color: var(--ink);
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-end;
    padding: 18px 22px 12px;
    border-bottom: 1px solid var(--line);
  }
  h1 { font-size: 22px; font-weight: 650; margin: 0; letter-spacing: 0.02em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  label.speed { font-size: 13px; color: var(--muted); display: flex; gap: 8px; align-items: center; }
  button, input[type="number"] {
    font: inherit;
    border: 1px solid var(--line);
    background: white;
    border-radius: 6px;
    padding: 6px 10px;
  }
  button { cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: default; }
  button.stop { border-color: var(--accent); color: var(--accent); }
  main { padding: 16px 22px 28px; }
  .fingers {
    display: grid;
    grid-template-columns: repeat(5, minmax(180px, 1fr));
    gap: 12px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px;
  }
  .card h2 { margin: 0 0 8px; font-size: 16px; }
  .joint { padding: 8px 0; border-top: 1px solid var(--line); }
  .joint .top { display: flex; justify-content: space-between; font-size: 13px; }
  .joint .val { font-variant-numeric: tabular-nums; font-weight: 650; color: var(--accent); }
  input[type="range"] { width: 100%; margin: 6px 0; }
  .row { display: flex; gap: 6px; }
  .row button { flex: 1; padding: 5px 0; }
  .heat { margin-top: 10px; }
  .heat .cap { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 2px; margin-top: 6px; }
  .cell { height: 14px; border-radius: 2px; background: #ddd6cc; }
  .reserved { margin-top: 12px; color: var(--muted); font-size: 13px; }
  .msg { min-height: 1.2em; margin-top: 10px; font-size: 13px; }
  .msg.bad { color: var(--accent); }
  .msg.ok { color: var(--ok); }
  @media (max-width: 980px) {
    .fingers { grid-template-columns: 1fr 1fr; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>L20 左手</h1>
    <div class="sub" id="identity">正在连接…</div>
  </div>
  <div class="toolbar">
    <label class="speed">速度 <input id="speed" type="range" min="10" max="200" value="80"><span id="speedVal">80</span></label>
    <button class="stop" id="stop" type="button">停止</button>
  </div>
</header>
<main>
  <div class="fingers" id="fingers"></div>
  <div class="reserved" id="reserved"></div>
  <div class="msg" id="msg"></div>
</main>
<script>
const FINGERS = %%FINGERS%%;
const NAMES = %%NAMES%%;
const RESERVED = [11, 12, 13, 14];
const fingers = document.getElementById("fingers");
const dragging = new Set();
let wasBusy = false;

function cellColor(v) {
  if (v < 0) return "#ddd6cc";
  const t = Math.max(0, Math.min(1, v / 80));
  const r = Math.round(221 + (154 - 221) * t);
  const g = Math.round(214 + (52 - 214) * t);
  const b = Math.round(204 + (18 - 204) * t);
  return `rgb(${r},${g},${b})`;
}

function build() {
  fingers.innerHTML = "";
  for (const [name, joints] of FINGERS) {
    const card = document.createElement("section");
    card.className = "card";
    card.innerHTML = `<h2>${name}</h2>`;
    for (const index of joints) {
      const box = document.createElement("div");
      box.className = "joint";
      box.innerHTML = `
        <div class="top"><span>${NAMES[index]}</span><span class="val" id="pos-${index}">—</span></div>
        <input type="range" min="0" max="255" value="0" id="slider-${index}" aria-label="${NAMES[index]}">
        <div class="row">
          <button type="button" data-jog="${index}" data-delta="-10">-10</button>
          <button type="button" data-jog="${index}" data-delta="10">+10</button>
          <button type="button" data-go="${index}">走到</button>
        </div>`;
      card.appendChild(box);
      const slider = box.querySelector("input");
      slider.addEventListener("pointerdown", () => dragging.add(index));
      slider.addEventListener("pointerup", () => dragging.delete(index));
      slider.addEventListener("pointercancel", () => dragging.delete(index));
      slider.addEventListener("input", () => { slider.dataset.dirty = "1"; });
    }
    const heat = document.createElement("div");
    heat.className = "heat";
    heat.innerHTML = `<div class="cap"><span>触觉</span><span id="peak-${name}">最大 —</span></div><div class="grid" id="grid-${name}"></div>`;
    const grid = heat.querySelector(".grid");
    for (let i = 0; i < 72; i++) {
      const cell = document.createElement("div");
      cell.className = "cell";
      cell.id = `cell-${name}-${i}`;
      grid.appendChild(cell);
    }
    card.appendChild(heat);
    fingers.appendChild(card);
  }
  document.getElementById("reserved").textContent =
    "预留关节（只读）：" + RESERVED.map(i => `${i} ${NAMES[i]} —`).join("  ");
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const data = await res.json();
  const msg = document.getElementById("msg");
  msg.className = "msg" + (res.ok ? "" : " bad");
  msg.textContent = data.message || data.error || "";
}

function speed() {
  return Number(document.getElementById("speed").value);
}

document.getElementById("speed").addEventListener("input", (event) => {
  document.getElementById("speedVal").textContent = event.target.value;
});
document.getElementById("stop").addEventListener("click", () => post("/api/stop", {}));
fingers.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.jog) {
    post("/api/jog", {index: Number(button.dataset.jog), delta: Number(button.dataset.delta), speed: speed()});
  } else if (button.dataset.go) {
    const index = Number(button.dataset.go);
    const target = Number(document.getElementById("slider-" + index).value);
    post("/api/move", {index, target, speed: speed()});
  }
});

function render(data) {
  const id = document.getElementById("identity");
  if (!data.connected) {
    id.textContent = data.message || "未连接";
    document.getElementById("msg").textContent = data.error || "";
    document.getElementById("msg").className = "msg bad";
    return;
  }
  const touch = data.touch_type === 2 ? "第二代矩阵触觉" : (data.touch_type === 1 ? "第一代力传感器" : "无触觉回包");
  id.textContent = `CAN 0x${data.can_id.toString(16).toUpperCase()} · ${data.serial || "无序列号"} · ${touch}` + (data.busy ? " · 运动中" : "");
  const buttons = document.querySelectorAll("button");
  for (const button of buttons) {
    if (button.id !== "stop") button.disabled = !!data.busy;
  }
  const finished = wasBusy && !data.busy;
  wasBusy = !!data.busy;
  for (let i = 0; i < 20; i++) {
    const pos = document.getElementById("pos-" + i);
    if (pos) pos.textContent = data.positions[i] == null ? "—" : data.positions[i];
    const slider = document.getElementById("slider-" + i);
    if (!slider || dragging.has(i) || data.positions[i] == null) continue;
    if (slider.dataset.dirty === "1" && !finished) continue;
    slider.value = data.positions[i];
    slider.dataset.dirty = "";
  }
  const reserved = RESERVED.map(i => {
    const v = data.positions[i];
    return `${i} ${NAMES[i]} ${v == null ? "—" : v}`;
  }).join("  ");
  document.getElementById("reserved").textContent = "预留关节（只读）：" + reserved;
  for (const [name] of FINGERS) {
    const matrix = (data.matrices || {})[name];
    if (!matrix) continue;
    let peak = 0;
    matrix.forEach((row, r) => row.forEach((v, c) => {
      const cell = document.getElementById(`cell-${name}-${r * 6 + c}`);
      if (cell) cell.style.background = cellColor(v);
      if (v > peak) peak = v;
    }));
    document.getElementById("peak-" + name).textContent = "最大 " + peak;
  }
  if (!data.busy && data.message) {
    const msg = document.getElementById("msg");
    msg.className = "msg" + (data.error ? " bad" : " ok");
    msg.textContent = data.error || data.message;
  }
}

async function poll() {
  try {
    const res = await fetch("/api/state");
    render(await res.json());
  } catch (err) {
    document.getElementById("msg").textContent = "页面和本机服务断开了";
    document.getElementById("msg").className = "msg bad";
  }
}

build();
poll();
setInterval(poll, 400);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    service: HandService

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, payload: dict | str, content_type: str) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/":
            page = (
                PAGE.replace("%%FINGERS%%", json.dumps(FINGERS, ensure_ascii=False))
                .replace("%%NAMES%%", json.dumps(JOINT_NAMES, ensure_ascii=False))
            )
            self._send(200, page, "text/html; charset=utf-8")
            return
        if self.path == "/api/state":
            self._send(200, self.service.snapshot(), "application/json; charset=utf-8")
            return
        self._send(404, {"error": "没有这个地址"}, "application/json; charset=utf-8")

    def do_POST(self) -> None:
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": "请求不是 JSON"}, "application/json; charset=utf-8")
            return
        if self.path == "/api/stop":
            self.service.request_stop()
            self._send(200, {"message": "已请求停止"}, "application/json; charset=utf-8")
            return
        if self.path not in ("/api/move", "/api/jog"):
            self._send(404, {"error": "没有这个地址"}, "application/json; charset=utf-8")
            return
        if not self.service.snapshot()["connected"]:
            self._send(409, {"error": "手还没连上"}, "application/json; charset=utf-8")
            return
        try:
            index = int(body["index"])
        except (KeyError, TypeError, ValueError):
            self._send(400, {"error": "缺少关节序号"}, "application/json; charset=utf-8")
            return
        if not 0 <= index < 20:
            self._send(400, {"error": "关节序号要在 0 到 19"}, "application/json; charset=utf-8")
            return
        command = {"op": "jog" if self.path == "/api/jog" else "move", "index": index}
        if command["op"] == "jog":
            command["delta"] = int(body.get("delta", 0))
        else:
            command["target"] = int(body.get("target", 0))
        command["speed"] = int(body.get("speed", 80))
        if not self.service.submit(command):
            self._send(409, {"error": "上一条还没走完"}, "application/json; charset=utf-8")
            return
        self._send(200, {"message": "已发送"}, "application/json; charset=utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="L20 左手控制面板")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    service = HandService()
    service.start()
    Handler.service = service
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"控制面板: {url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭。")
    finally:
        server.shutdown()
        service.close()


if __name__ == "__main__":
    main()
