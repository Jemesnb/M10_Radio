# M10_Radio

运行在行空板 M10 上的纯触摸屏网络电台。程序不提供网页控制端，Wi-Fi、蓝牙、选台、播放和音量操作都通过 M10 触摸屏完成。

## 1. 硬件和系统依赖

- 行空板 M10，建议使用较新的 M10 系统镜像。
- 2.4G Wi-Fi 热点。M10 的 Wi-Fi 连接以 2.4G 网络为准。
- 蓝牙耳机，或 USB 扬声器/USB 音频转接设备。M10 本身没有板载扬声器。
- M10 Linux 中需要 NetworkManager、BlueZ、mpv 和 PulseAudio 的 `pactl` 工具。

在 M10 终端执行：

```bash
sudo apt-get update
sudo apt-get install -y network-manager bluez mpv pulseaudio-utils ca-certificates
python3 -m pip install -U unihiker
sudo systemctl enable --now NetworkManager
sudo systemctl enable --now bluetooth
```

项目只使用标准库和官方 `unihiker` 库；`mpv`、`nmcli`、`bluetoothctl` 是系统依赖。

## 2. 电台地址配置

编辑同目录的 `stations.json`：

```json
[
  {"name": "我的电台", "url": "https://example.com/stream.mp3"},
  {"name": "另一个电台", "url": "https://example.com/live.aac"}
]
```

每条记录必须包含非空的 `name` 和 `http://`/`https://` 地址。程序启动时会忽略无效记录；如果没有有效记录，电台页面会提示检查配置。

默认配置包含 8 个电台，前 5 个为中文台（已在行空板上实测可正常播放）：

| 电台 | 地址 |
| --- | --- |
| 环球资讯广播（CRI） | `http://sk.cri.cn/905.m3u8` |
| 中国之声（央广） | `http://ngcdn001.cnr.cn/live/zgzs/index.m3u8` |
| 经济之声（央广） | `http://ngcdn002.cnr.cn/live/jjzs/index.m3u8` |
| 音乐之声（央广） | `http://ngcdn001.cnr.cn/live/yyzs/index.m3u8` |
| 中文环球（CRI） | `http://sk.cri.cn/hyhq.m3u8` |

后 3 个为 Radio Paradise 和 SomaFM 公共流。这些地址是官方直播源，不保证长期有效；
播放失败时替换为本地可访问的 MP3/AAC/HLS 电台流即可。

## 3. 运行

```bash
cd /path/to/M10_Radio
python3 main.py
```

也可以在行空板 Home 菜单中运行 `main.py`。如果要开机自动启动，请先确认程序能正常退出和播放，再使用 M10 系统的自动启动设置。

## 4. 触摸屏使用

### Wi-Fi

1. 点击“扫描 Wi-Fi”。
2. 点击列表中的 SSID。
3. 使用屏幕键盘输入密码；`abc`、`ABC`、`123`、`#=` 可切换字符页。
4. 点击“连接”。
5. 成功后页面显示 SSID 和 IP；失败会显示错误原因。

只有 Wi-Fi 连接成功后，电台播放按钮才会启用。

### 电台播放

在电台页面点击电台名称即可自动连接并播放。可使用：

- 上一台、下一台
- 播放/暂停
- 停止
- 音量 −、音量 +
- Wi-Fi 和蓝牙页面切换

### 蓝牙耳机

1. 点击“蓝牙”。
2. 点击“搜索设备”。
3. 将耳机置于配对模式（并先断开它与手机的连接）。
4. 点击耳机名称，等待配对和连接完成。

列表只显示有名称的设备。没有名字的 BLE 信标（传感器、家电等）会被自动隐藏，
页面底部会提示隐藏了几个。这类设备即使配对成功也不会提供音频输出，
点击它们会提示“已配对但未建立音频连接”。

如果蓝牙耳机未连接，程序会提示检查蓝牙状态或使用 USB 扬声器/USB 音频转接设备播放。

### 息屏（按 A 键）

按行空板上的 **A 键**可以把屏幕涂黑，再按一次（或触摸屏幕）恢复，**电台会继续播放**。

**这是真的关背光，不只是把画面涂黑**。板子自带 `/usr/sbin/brightness`，
往 PWM `pwmchip1/pwm0` 的 `duty_cycle` 写值（范围 0-100），程序息屏时执行
`brightness 0`、恢复时执行 `brightness 100`。

> 之前一直以为这块板子的背光不可调 —— `/sys/class/backlight` 是空的，
> `fb_blank` 返回 EINVAL，Xorg 也以 `-dpms` 启动。其实官方走的是 PWM 那条路，
> 按 backlight 去找永远找不到。手动试：`sudo brightness 0` / `sudo brightness 100`
> （非 root 要加 sudo）。已在板上实测通过。

如果板子上没有这个脚本（比如刷了第三方系统），程序会静默跳过，退化成只涂黑画面。
黑屏期间的状态轮询不会把界面重新画出来；**退出程序时如果还在息屏状态，会自动把背光恢复**。

### 退出程序

两种方式：

- **按行空板的 Home 键** —— 从行空板 Home 菜单启动程序时，按 Home 键即可退出回到菜单。
  （Home 菜单只管理它自己启动的进程，所以这是最自然的方式。）
- **点屏幕上的“退出”按钮**（Wi-Fi 页面右下角）—— 任何启动方式都有效。
  如果你是从 SSH 手动运行 `python3 main.py`，Home 菜单并不知道这个进程存在，
  此时 Home 键无效，请用这个按钮退出。

## 5. 常见问题

### 找不到 Wi-Fi

确认热点是 2.4G、行空板距离路由器较近，并关闭手机热点的隐藏 SSID。重新点击“扫描 Wi-Fi”。

### Wi-Fi 密码错误或一直连接失败

重新输入密码，注意大小写和符号。可以在 M10 终端检查：

```bash
nmcli device status
nmcli device wifi list
```

### 电台地址无法访问

先确认 M10 已显示 IP，再在终端测试地址或替换为可访问的 MP3/AAC/HLS 流。部分电台限制地区、连接数或 User-Agent。

### mpv 未找到或播放失败

```bash
which mpv
mpv --no-video https://example.com/stream.mp3
```

如果没有输出设备，请接入 USB 扬声器、USB 音频转接设备或蓝牙耳机。

### 蓝牙搜索不到耳机

确认耳机没有连接到手机，重新进入配对模式，并执行：

```bash
bluetoothctl show
systemctl status bluetooth
```

### 触摸位置不准确

使用 M10 Home 菜单中的触摸屏校准功能后重新运行程序。官方 `unihiker` GUI 的屏幕坐标为 240×320，项目界面按此尺寸设计。

## 6. 官方参考

- [行空板 M10 unihiker Python 库](https://www.unihiker.com.cn/wiki/m10/unihiker_python_lib)
- [M10 网络连接说明](https://www.unihiker.com.cn/wiki/m10/networking)
- [mpv JSON IPC 手册](https://mpv.io/manual/stable/)
- [BlueZ bluetoothctl 文档](https://github.com/bluez/bluez/blob/master/doc/bluetoothctl.rst)
