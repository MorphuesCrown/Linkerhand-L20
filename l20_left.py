#!/usr/bin/env python3
"""L20 左手调试：关节位置、速度、故障、温度，以及触觉（若有）。

依赖 macOS 上的 PCBUSB（libPCBUSB.dylib，装在 /usr/local/lib）和 python-can。
适配器是 PEAK PCAN-USB。灵巧手 CAN 波特率为 1 Mbps。

这台左手的出厂 CAN ID 是 0x28（官方示例里 0x28 常被当作右手，0x27 当作左手）。
脚本启动时会在 0x27 / 0x28 里自动找有应答的那一只。

关节顺序（0–255）：
  0-4   拇指/食指/中指/无名指/小指 根部弯曲
  5-9   拇指/食指/中指/无名指/小指 侧摆
  10    拇指横滚
  11-14 协议预留
  15-19 拇指/食指/中指/无名指/小指 指尖

用法：
  python3 l20_left.py info
  python3 l20_left.py state
  python3 l20_left.py touch
  python3 l20_left.py monitor
  python3 l20_left.py jog 0 15          # 关节 0 增加 15，停一下，再回到原位
  python3 l20_left.py move 0 180        # 把关节 0 走到 180，不自动回位
  python3 l20_left.py speed 40 40 40 40 40
  python3 l20_gui.py                    # 打开浏览器控制面板
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

import can

JOINT_NAMES = [
    "拇指根部",
    "食指根部",
    "中指根部",
    "无名指根部",
    "小指根部",
    "拇指侧摆",
    "食指侧摆",
    "中指侧摆",
    "无名指侧摆",
    "小指侧摆",
    "拇指横滚",
    "预留11",
    "预留12",
    "预留13",
    "预留14",
    "拇指指尖",
    "食指指尖",
    "中指指尖",
    "无名指指尖",
    "小指指尖",
]

GROUP_FRAME = (0x01, 0x02, 0x03, 0x04)
FINGER_NAMES = ("拇指", "食指", "中指", "无名指", "小指")
MATRIX_ROW = {i * 16: i for i in range(12)}

FAULT_BITS = (
    (0x01, "过压/欠压"),
    (0x02, "磁编码异常"),
    (0x04, "电机过温"),
    (0x08, "电流过流"),
    (0x20, "负载过载"),
)


def decode_fault(code: int) -> str:
    if code == 0:
        return "正常"
    names = [name for bit, name in FAULT_BITS if code & bit]
    extra = code & ~0x2F
    if extra:
        names.append(f"未知位0x{extra:02X}")
    return "、".join(names) if names else f"0x{code:02X}"


class L20:
    def __init__(self, channel: str = "PCAN_USBBUS1", bitrate: int = 1_000_000):
        self.bus = can.Bus(interface="pcan", channel=channel, bitrate=bitrate)
        self.can_id: int | None = None

    def close(self) -> None:
        self.bus.shutdown()

    def send(self, can_id: int, data: list[int]) -> None:
        self.bus.send(
            can.Message(
                arbitration_id=can_id,
                data=bytes(int(b) & 0xFF for b in data),
                is_extended_id=False,
            )
        )

    def collect(self, seconds: float) -> list[can.Message]:
        frames: list[can.Message] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            msg = self.bus.recv(timeout=0.05)
            if msg is not None and not msg.is_error_frame:
                frames.append(msg)
        return frames

    def transact(self, cmd: int, payload: list[int] | None = None, wait: float = 0.08) -> list[bytes]:
        if self.can_id is None:
            raise RuntimeError("尚未发现灵巧手")
        data = [cmd] if not payload else [cmd, *payload]
        self.send(self.can_id, data)
        matched = []
        for msg in self.collect(wait):
            if msg.arbitration_id == self.can_id and msg.data and msg.data[0] == cmd:
                matched.append(bytes(msg.data))
        return matched

    def discover(self) -> int:
        hits: dict[int, list[can.Message]] = defaultdict(list)
        for can_id in (0x27, 0x28):
            self.send(can_id, [0xC2])
            time.sleep(0.03)
        for msg in self.collect(0.4):
            if msg.data:
                hits[msg.arbitration_id].append(msg)
        if not hits:
            raise RuntimeError(
                "CAN 适配器已打开，但 0x27 / 0x28 都没有应答。"
                "请确认灵巧手已单独供电，且 CANH/CANL 没有接反。"
            )
        # 左手默认优先 0x27；这台设备实际应答在 0x28。
        self.can_id = 0x27 if 0x27 in hits else sorted(hits)[0]
        return self.can_id

    def read_group(self, frame: int) -> list[int] | None:
        frames = self.transact(frame, wait=0.06)
        if not frames:
            return None
        return list(frames[-1][1:])

    def read_positions(self) -> list[int | None]:
        values: list[int | None] = [None] * 20
        for group, frame in enumerate(GROUP_FRAME):
            data = self.read_group(frame)
            if not data:
                continue
            for offset, value in enumerate(data[:5]):
                values[group * 5 + offset] = value
        return values

    def read_vector(self, cmd: int) -> list[int] | None:
        frames = self.transact(cmd, wait=0.06)
        if not frames:
            return None
        return list(frames[-1][1:])

    def device_info(self) -> dict:
        chunks: dict[int, bytes] = {}
        self.send(self.can_id, [0xC0])
        time.sleep(0.02)
        self.send(self.can_id, [0xC1])
        time.sleep(0.02)
        self.send(self.can_id, [0xC2])
        for msg in self.collect(0.3):
            if msg.arbitration_id != self.can_id or not msg.data:
                continue
            kind = msg.data[0]
            if kind == 0xC0 and len(msg.data) >= 2:
                chunks[msg.data[1]] = bytes(msg.data[2:])
            elif kind == 0xC1:
                chunks["hw"] = bytes(msg.data[1:])
            elif kind == 0xC2:
                chunks["sw"] = bytes(msg.data[1:])
        serial = b"".join(chunks[i] for i in sorted(k for k in chunks if isinstance(k, int)))
        return {
            "serial": serial.decode("ascii", errors="replace").strip("\x00"),
            "hardware": list(chunks.get("hw", b"")),
            "software": list(chunks.get("sw", b"")),
        }

    def touch_type(self) -> int | None:
        frames = self.transact(0xB0, wait=0.08)
        if not frames or len(frames[-1]) < 2:
            return None
        return frames[-1][1]

    def read_matrix(self, wait: float = 0.8) -> dict[str, list[list[int]]]:
        """第二代触觉：每指 12x6。请求码 0xC6。"""
        matrices = {name: [[-1] * 6 for _ in range(12)] for name in FINGER_NAMES}
        for cmd in (0xB1, 0xB2, 0xB3, 0xB4, 0xB5):
            self.send(self.can_id, [cmd, 0xC6])
            time.sleep(0.01)
        finger_of = {0xB1: "拇指", 0xB2: "食指", 0xB3: "中指", 0xB4: "无名指", 0xB5: "小指"}
        for msg in self.collect(wait):
            if msg.arbitration_id != self.can_id or len(msg.data) != 8:
                continue
            finger = finger_of.get(msg.data[0])
            row = MATRIX_ROW.get(msg.data[1])
            if finger is None or row is None:
                continue
            matrices[finger][row] = list(msg.data[2:])
        return matrices

    def read_scalar_touch(self) -> list[int] | None:
        """每指一个汇总值。第二代在 0xB1..0xB5 的短帧里，第一代在 0x20。"""
        values = []
        for cmd in (0xB1, 0xB2, 0xB3, 0xB4, 0xB5):
            frames = self.transact(cmd, wait=0.05)
            short = [frame for frame in frames if len(frame) <= 3]
            if not short:
                return None
            values.append(short[-1][1] if len(short[-1]) > 1 else -1)
        return values

    def set_speed(self, speed: list[int]) -> None:
        if len(speed) != 5:
            raise ValueError("速度必须是 5 个数，分别对应五指")
        self.send(self.can_id, [0x05, *[max(0, min(255, int(v))) for v in speed]])
        time.sleep(0.02)

    def move_joint(
        self,
        index: int,
        target: int,
        speed: int = 80,
        timeout: float = 2.5,
        should_stop=None,
    ) -> tuple[int, int]:
        """按 20Hz 连续发送位置帧，直到接近目标、停下或超时。

        这只手收到单次位置帧不会保持运动，需要把目标连续发出来。
        同一组里的其他关节保持当前读数，避免被写成 0。
        """
        if not 0 <= index < 20:
            raise ValueError("关节序号必须在 0..19")
        positions = self.read_positions()
        group = index // 5
        offset = index % 5
        current_group = positions[group * 5 : (group + 1) * 5]
        if any(v is None for v in current_group):
            raise RuntimeError(f"读不到第 {group} 组关节，取消运动")
        before = int(current_group[offset])
        target = max(0, min(255, int(target)))
        updated = [int(v) for v in current_group]
        updated[offset] = target
        frame = GROUP_FRAME[group]
        self.set_speed([speed] * 5)
        after = before
        last = before
        still_since = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                break
            self.send(self.can_id, [frame, *updated])
            msg = self.bus.recv(timeout=0.05)
            if msg is None or msg.arbitration_id != self.can_id or not msg.data or msg.data[0] != frame:
                continue
            if len(msg.data) <= offset + 1:
                continue
            after = int(msg.data[offset + 1])
            if abs(after - target) <= 2:
                break
            if after != last:
                last = after
                still_since = time.time()
            elif time.time() - still_since > 0.45:
                break
        return before, after


def fmt_positions(values: list[int | None]) -> str:
    lines = []
    for i, (name, value) in enumerate(zip(JOINT_NAMES, values)):
        shown = "—" if value is None else f"{value:3d}"
        lines.append(f"  [{i:02d}] {name:<8} {shown}")
    return "\n".join(lines)


def cmd_info(hand: L20) -> None:
    info = hand.device_info()
    touch = hand.touch_type()
    touch_name = {1: "第一代力传感器", 2: "第二代矩阵触觉", None: "无应答"}.get(touch, f"未知({touch})")
    print(f"CAN ID      : 0x{hand.can_id:02X}")
    print(f"序列号      : {info['serial'] or '无'}")
    print(f"硬件版本    : {info['hardware'] or '无'}")
    print(f"软件版本    : {info['software'] or '无'}")
    print(f"触觉类型    : {touch_name}")


def cmd_state(hand: L20) -> None:
    print(fmt_positions(hand.read_positions()))
    speed = hand.read_vector(0x05)
    current = hand.read_vector(0x06)
    fault = hand.read_vector(0x07)
    temps = []
    for cmd in (0x09, 0x0B, 0x0C, 0x0D):
        part = hand.read_vector(cmd)
        temps.extend(part or [])
    print("速度帧      :", speed, "（静止时为 0，不代表速度上限）")
    print("电流帧      :", current)
    if fault:
        print("故障        :", [f"{v}({decode_fault(v)})" for v in fault])
    else:
        print("故障        : 无应答")
    if temps:
        print("温度        :", temps)
    else:
        print("温度        : 无应答")


def cmd_touch(hand: L20) -> None:
    kind = hand.touch_type()
    print(f"触觉类型    : {kind}")
    if kind == 1:
        normal = hand.read_vector(0x20)
        tangential = hand.read_vector(0x21)
        direction = hand.read_vector(0x22)
        approach = hand.read_vector(0x23)
        print("法向力      :", normal)
        print("切向力      :", tangential)
        print("切向方向    :", direction)
        print("接近增量    :", approach)
        return
    if kind != 2:
        print("这只手没有返回触觉数据。")
        return
    scalar = hand.read_scalar_touch()
    print("五指合力    :", scalar, "（拇指 食指 中指 无名指 小指）")
    matrices = hand.read_matrix()
    for name, matrix in matrices.items():
        filled = sum(cell != -1 for row in matrix for cell in row)
        peak = max((cell for row in matrix for cell in row), default=-1)
        print(f"  {name} 矩阵 {filled}/72 点有数据，最大值 {peak}")
        for row in matrix:
            print("   ", " ".join(f"{v:3d}" if v >= 0 else "  ." for v in row))


def cmd_monitor(hand: L20) -> None:
    print("持续读取关节和触觉合力，Ctrl+C 结束。")
    kind = hand.touch_type()
    try:
        while True:
            pos = hand.read_positions()
            shown = " ".join("--" if v is None else f"{v:3d}" for v in pos)
            extra = ""
            if kind == 2:
                extra = "  touch=" + str(hand.read_scalar_touch())
            print(shown + extra, flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n停止。")


def cmd_jog(hand: L20, index: int, delta: int, speed: int) -> None:
    if abs(delta) > 40:
        raise SystemExit("单次点动限制在 ±40，避免一下打到极限。")
    positions = hand.read_positions()
    current = positions[index]
    if current is None:
        raise SystemExit(f"读不到关节 {index}")
    target = max(0, min(255, current + delta))
    print(f"关节 [{index:02d}] {JOINT_NAMES[index]} : {current} -> {target}，速度 {speed}")
    before, after = hand.move_joint(index, target, speed=speed)
    print(f"到位反馈    : {before} -> {after}" + ("" if abs(after - target) <= 2 else "（未到目标，多半是限位）"))
    time.sleep(0.3)
    back_before, back_after = hand.move_joint(index, current, speed=speed)
    print(f"回到原位    : {back_before} -> {back_after}")


def cmd_move(hand: L20, index: int, target: int, speed: int) -> None:
    before, after = hand.move_joint(index, target, speed=speed)
    print(f"关节 [{index:02d}] {JOINT_NAMES[index]} : {before} -> {after}（目标 {target}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="L20 左手 CAN 调试")
    parser.add_argument("command", choices=("info", "state", "touch", "monitor", "jog", "move", "speed"))
    parser.add_argument("args", nargs="*")
    parser.add_argument("--channel", default="PCAN_USBBUS1")
    parser.add_argument("--speed", type=int, default=40, help="点动/移动时的五指速度 0-255")
    ns = parser.parse_args()

    hand = L20(channel=ns.channel)
    try:
        can_id = hand.discover()
        print(f"已连接 L20，CAN ID 0x{can_id:02X}")
        if ns.command == "info":
            cmd_info(hand)
        elif ns.command == "state":
            cmd_state(hand)
        elif ns.command == "touch":
            cmd_touch(hand)
        elif ns.command == "monitor":
            cmd_monitor(hand)
        elif ns.command == "jog":
            if len(ns.args) != 2:
                raise SystemExit("用法: l20_left.py jog <关节0-19> <增量>")
            cmd_jog(hand, int(ns.args[0]), int(ns.args[1]), ns.speed)
        elif ns.command == "move":
            if len(ns.args) != 2:
                raise SystemExit("用法: l20_left.py move <关节0-19> <目标0-255>")
            cmd_move(hand, int(ns.args[0]), int(ns.args[1]), ns.speed)
        elif ns.command == "speed":
            if len(ns.args) != 5:
                raise SystemExit("用法: l20_left.py speed v0 v1 v2 v3 v4")
            hand.set_speed([int(v) for v in ns.args])
            time.sleep(0.05)
            print("当前速度    :", hand.read_vector(0x05))
    finally:
        hand.close()


if __name__ == "__main__":
    try:
        main()
    except can.CanError as exc:
        print(f"CAN 错误: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"打不开 PCAN: {exc}", file=sys.stderr)
        print("需要 /usr/local/lib/libPCBUSB.dylib（MacCAN PCBUSB）。", file=sys.stderr)
        sys.exit(1)
