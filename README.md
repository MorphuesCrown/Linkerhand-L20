# Linkerhand L20 左手

在 macOS 上通过 PEAK PCAN-USB 控制灵心巧手 L20 左手：读关节、点动、看触觉。

这套脚本对应的实机是序列号 `LHT20-011-261-L-B-1-D`。官方示例里常把 `0x27` 当作左手、`0x28` 当作右手，但这台左手出厂 CAN ID 是 **0x28**。程序启动时会在 `0x27` 和 `0x28` 里自动找有应答的那一只。

通信是 CAN 2.0，波特率 **1 Mbps**。

## 环境

- macOS，Python 3.10 及以上
- PEAK PCAN-USB。没装驱动时，系统信息里会显示成 `XCAN-USB`（厂商号 `0x0c72`，产品号 `0x000c`）
- [MacCAN PCBUSB](https://github.com/mac-can/PCBUSB-Library/releases) 用户态驱动，本机用的是 0.13
- 灵巧手需要单独供电。USB 只给 CAN 适配器供电

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

安装 PCBUSB（Intel 和 Apple 芯片都用 Universal 那个包）：

```bash
curl -fsSL -o /tmp/pcbusb.tar.gz \
  https://github.com/mac-can/PCBUSB-Library/releases/download/v0.13/macOS_Library_for_PCANUSB_v0.13.tar.gz
mkdir -p /tmp/pcbusb && tar -xzf /tmp/pcbusb.tar.gz -C /tmp/pcbusb
xattr -d com.apple.quarantine /tmp/pcbusb/PCBUSB/libPCBUSB.0.13.dylib 2>/dev/null || true
sudo mkdir -p /usr/local/lib
sudo cp /tmp/pcbusb/PCBUSB/libPCBUSB.0.13.dylib /usr/local/lib/
sudo ln -sf /usr/local/lib/libPCBUSB.0.13.dylib /usr/local/lib/libPCBUSB.dylib
```

`python-can` 会按名字查找 `libPCBUSB.dylib`，所以必须放在 `/usr/local/lib`。

## 控制面板

```bash
python3 l20_gui.py
```

浏览器打开 http://127.0.0.1:8765 。

- 五指各一张卡片。红色数字是当前关节位置（0–255）
- 拖动滑条后按 **走到**，手才会往该目标运动。只拖滑条不会动
- **-10 / +10** 相对当前位置点动
- 右上角调节速度（10–200），**停止** 会打断正在进行的运动
- 每张卡片下方是该指的 12×6 触觉矩阵。没按压时几乎全是 0
- 关节 11–14 是协议预留，只显示，不能下发

指定端口、或不自动打开浏览器：

```bash
python3 l20_gui.py --port 8765 --no-browser
```

## 命令行

```bash
python3 l20_left.py info
python3 l20_left.py state
python3 l20_left.py touch
python3 l20_left.py monitor
python3 l20_left.py jog 0 15 --speed 80
python3 l20_left.py move 0 180 --speed 80
python3 l20_left.py speed 80 80 80 80 80
```

`jog` 先走到目标，再回到点动前的位置，单次增量限制在 ±40。`move` 停在目标，不自动回位。

这只手收到一帧位置指令不会一直走，需要以大约 20 Hz 连续发送。`jog`、`move` 和控制面板都已经这样做。同一组里没被选中的关节会保持当前读数，避免被写成 0。

## 关节顺序

数值范围都是 0–255。

| 序号 | 名称 | 说明 |
| --- | --- | --- |
| 0–4 | 拇指 / 食指 / 中指 / 无名指 / 小指根部 | 根部弯曲 |
| 5–9 | 五指侧摆 | |
| 10 | 拇指横滚 | |
| 11–14 | 预留 | 只读，不要当关节去驱动 |
| 15–19 | 五指指尖 | |

分组和 CAN 帧的对应关系：0–4 用 `0x01`，5–9 用 `0x02`，10–14 用 `0x03`，15–19 用 `0x04`。帧里第一个字节是命令，后面 5 个字节是这一组的位置。

## 触觉

启动时读 `0xB0`。

- 返回 `2`：第二代矩阵触觉。每指向 `0xB1`–`0xB5` 发送 `0xC6`，得到 12 行 × 6 列，数值 0–255
- 返回 `1`：第一代力传感器，读法向力、切向力、方向和接近增量（`0x20`–`0x23`）

## 读数时要注意

- 速度帧 `0x05` 在静止时回的是 0，不代表速度上限没设上。运动前脚本会单独写一次速度
- 电流帧 `0x06` 在这台手上是一组不怎么变的 5 字节，不要当成实时电流
- 故障帧 `0x07` 这台固件没有回包
- 温度帧大多是 0，不能当成摄氏温度

## 文件

| 文件 | 作用 |
| --- | --- |
| `l20_left.py` | CAN 协议和命令行 |
| `l20_gui.py` | 浏览器控制面板，CAN 收发在单独线程里 |
| `requirements.txt` | Python 依赖 |
