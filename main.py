# -*- coding: utf-8 -*-
"""M10_Radio: a touch-only Internet radio for UNIHIKER M10."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import signal
import threading
import time
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

from unihiker import GUI

from bluetooth_manager import BluetoothDevice, BluetoothError, BluetoothManager
from radio_player import MpvRadioPlayer, PlayerEvent, RadioPlayerError
from wifi_manager import WifiError, WifiManager, WifiNetwork, WifiStatus


SCREEN_WIFI = "wifi"
SCREEN_PASSWORD = "password"
SCREEN_RADIO = "radio"
SCREEN_BLUETOOTH = "bluetooth"


@dataclass(frozen=True)
class Station:
    name: str
    url: str


class RadioApp:
    NETWORKS_PER_PAGE = 4
    STATIONS_PER_PAGE = 3
    DEVICES_PER_PAGE = 3

    def __init__(self) -> None:
        self.gui = GUI()
        self.events: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.tasks = set()
        self.screen = SCREEN_WIFI
        self.message = ""
        self.networks: List[WifiNetwork] = []
        self.network_page = 0
        self.selected_network: Optional[WifiNetwork] = None
        self.password = ""
        self.keyboard_mode = "lower"
        self.wifi = WifiManager()
        self.wifi_status = WifiStatus(False, state="检查中")
        self.stations, station_error = self._load_stations()
        if station_error:
            self.message = station_error
        self.station_index = 0
        self.station_page = 0
        self.radio_state = "未播放"
        self.volume = 60
        self.player = MpvRadioPlayer(on_event=self._on_player_event, initial_volume=self.volume)
        self.bluetooth = BluetoothManager()
        self.bluetooth_devices: List[BluetoothDevice] = []
        self.bluetooth_page = 0
        self.bluetooth_state = "未连接"
        self.connected_bluetooth: Optional[BluetoothDevice] = None
        self.running = True
        self.next_status_poll = 0.0
        self.screen_off = False

        self.gui.on_a_click(self._toggle_screen_off)
        self._render()
        self._start_task("wifi_status", self.wifi.status)
        # A headset may already be connected from a previous run; find out so
        # the header mark and the device list reflect reality.
        self._start_task("bt_status", self.bluetooth.connected_device)

    # Background work is kept out of GUI callbacks so a scan or connection
    # never blocks touch handling.
    def _start_task(self, name: str, operation: Callable[[], object]) -> None:
        if name in self.tasks:
            return
        self.tasks.add(name)

        def worker() -> None:
            try:
                result = operation()
            except (WifiError, BluetoothError, RadioPlayerError) as exc:
                self.events.put(("task_error", (name, str(exc))))
            except Exception as exc:
                self.events.put(("task_error", (name, f"操作失败：{exc}")))
            else:
                self.events.put(("task_done", (name, result)))

        threading.Thread(target=worker, name=f"m10-radio-{name}", daemon=True).start()

    def _drain_events(self) -> None:
        changed = False
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "task_done":
                name, result = payload  # type: ignore[misc]
                self.tasks.discard(name)
                changed = self._handle_task_done(name, result) or changed
            elif kind == "task_error":
                name, error = payload  # type: ignore[misc]
                self.tasks.discard(name)
                self._handle_task_error(name, str(error))
                changed = True
            elif kind == "player":
                self._handle_player_event(payload)  # type: ignore[arg-type]
                changed = True
        if changed:
            self._render()

    def _handle_task_done(self, name: str, result: object) -> bool:
        if name == "wifi_status":
            previous = self.wifi_status
            self.wifi_status = result  # type: ignore[assignment]
            if not self.wifi_status.connected and self.radio_state in ("播放中", "连接中", "已暂停"):
                self.player.stop(emit_event=False)
                self.radio_state = "未播放"
                self.message = "Wi-Fi 已断开，电台播放已停止"
                return True
            # The status poll runs every few seconds; repainting an unchanged
            # status is what made the screen flash and redraw widget by widget.
            return self.wifi_status != previous
        if name == "wifi_scan":
            self.networks = result  # type: ignore[assignment]
            self.network_page = 0
            self.message = f"发现 {len(self.networks)} 个 Wi-Fi 网络"
            return True
        if name == "wifi_connect":
            self.wifi_status = result  # type: ignore[assignment]
            self.message = f"Wi-Fi 连接成功：{self.wifi_status.ip_address or 'IP 获取中'}"
            self.screen = SCREEN_RADIO
            return True
        if name == "bt_status":
            self.connected_bluetooth = result  # type: ignore[assignment]
            if result is not None:
                self.bluetooth_state = "已连接"
            elif self.bluetooth_state in ("已连接", "未连接"):
                self.bluetooth_state = "未连接"
            return True
        if name == "bt_scan":
            self.bluetooth_devices = result  # type: ignore[assignment]
            self.bluetooth_page = 0
            hidden = getattr(self.bluetooth, "last_scan_hidden", 0)
            self.bluetooth_state = f"发现 {len(self.bluetooth_devices)} 个设备"
            self.message = (
                f"已隐藏 {hidden} 个无名蓝牙设备（多为传感器或家电），点击设备名称开始配对"
                if hidden
                else "点击设备名称开始配对和连接"
            )
            return True
        if name == "bt_pair":
            self.connected_bluetooth = result  # type: ignore[assignment]
            self.bluetooth_state = "已连接"
            self.message = "蓝牙耳机已连接，音频将优先输出到耳机"
            return True
        return False

    def _handle_task_error(self, name: str, error: str) -> None:
        if name == "bt_status":
            # Background probe: no headset connected is a normal outcome.
            return
        self.message = error
        if name == "wifi_status":
            self.wifi_status = WifiStatus(False, state="未连接", error=error)
        elif name == "wifi_scan":
            self.networks = []
        elif name == "wifi_connect":
            self.wifi_status = WifiStatus(False, state="连接失败", error=error)
        elif name == "bt_scan":
            self.bluetooth_state = "搜索失败"
            self.bluetooth_devices = []
        elif name == "bt_pair":
            self.bluetooth_state = "连接失败"
        elif name == "play_station":
            self.radio_state = "播放失败"

    def _on_player_event(self, event: PlayerEvent) -> None:
        self.events.put(("player", event))

    def _handle_player_event(self, event: PlayerEvent) -> None:
        if event.kind == "connecting":
            self.radio_state, self.message = "连接中", event.message
        elif event.kind == "playing":
            self.radio_state, self.message = "播放中", ""
        elif event.kind == "paused":
            self.radio_state, self.message = "已暂停", "点击“播放/暂停”继续播放"
        elif event.kind == "volume":
            try:
                self.volume = int(event.message.rsplit(" ", 1)[-1].rstrip("%"))
            except ValueError:
                pass
            self.message = event.message
        elif event.kind == "station_error":
            self.radio_state, self.message = "电台地址无法访问", event.message
        elif event.kind == "audio_error":
            self.radio_state, self.message = "音频播放失败", event.message
        elif event.kind == "stopped":
            self.radio_state = "已停止"

    # ------------------------------------------------------------------
    # Screen rendering helpers
    # ------------------------------------------------------------------
    def _render(self) -> None:
        self.gui.clear()
        if self.screen_off:
            self._render_screen_off()
            return
        if self.screen == SCREEN_WIFI:
            self._render_wifi()
        elif self.screen == SCREEN_PASSWORD:
            self._render_password()
        elif self.screen == SCREEN_RADIO:
            self._render_radio()
        else:
            self._render_bluetooth()

    def _render_screen_off(self) -> None:
        """Paint the panel black while playback continues.

        The M10 exposes no way to switch the LCD backlight off from user space
        (no /sys/class/backlight, fb_blank returns EINVAL, Xorg runs with
        -dpms), so this blanks the picture rather than the panel.  The whole
        area stays tappable so a touch also brings the display back.
        """

        self.gui.fill_round_rect(
            x=0, y=0, w=240, h=320, r=0, color="#000000",
            onclick=self._toggle_screen_off,
        )

    def _toggle_screen_off(self) -> None:
        self.screen_off = not self.screen_off
        self._render()

    def _header(self, title: str) -> None:
        self.gui.fill_round_rect(x=0, y=0, w=240, h=32, r=0, color="#183B56")
        # unihiker renders a line height of 2x the font size on this panel, so
        # every vertical slot in the render methods below is budgeted against
        # that.  The widest title ("M10_Radio · Wi-Fi") measures 175px at font
        # size 14; centring it on x=88 keeps it inside the 240px panel and
        # clear of the status marks at x=204.
        self.gui.draw_text(x=88, y=16, text=title, color="#FFFFFF", font_size=14, origin="center")
        wifi_mark = "Wi+" if self.wifi_status.connected else "Wi-"
        bt_mark = "BT+" if self.connected_bluetooth and self.connected_bluetooth.connected else "BT-"
        self.gui.draw_text(x=204, y=16, text=f"{wifi_mark} {bt_mark}", color="#D5F5FF", font_size=8, origin="center")

    def _text(self, text: str, x: int, y: int, width: int = 230, size: int = 10, color: str = "#1D2A33") -> None:
        self.gui.draw_text(x=x, y=y, w=width, text=text, color=color, font_size=size)

    def _button(self, text: str, x: int, y: int, w: int, h: int, callback: Callable[[], None], state: str = "normal") -> None:
        self.gui.add_button(x=x, y=y, w=w, h=h, text=text, state=state, onclick=callback)

    @staticmethod
    def _clip(text: str, limit: float) -> str:
        """Trim to about ``limit`` full-width glyphs so a long SSID or device
        name cannot wrap a label onto a second line and break the layout."""

        width = 0.0
        kept = []
        for char in text:
            width += 1.0 if ord(char) > 0x2E80 else 0.55
            if width > limit:
                break
            kept.append(char)
        return "".join(kept)

    def _message(self, y: int, width: int = 230) -> None:
        if self.message:
            # Font size 8 wraps at roughly 26 CJK glyphs per 230px line, so the
            # strip is capped at two lines (32px) to stay on the 320px panel.
            self._text(self.message[:52], 5, y, width=width, size=8, color="#B03A2E")

    def _render_wifi(self) -> None:
        self._header("M10_Radio · Wi-Fi")
        status = self.wifi_status
        # Three lines at font size 9 occupy 54px (34..88).
        self._text(
            f"状态：{status.state}\nSSID：{self._clip(status.ssid or '未连接', 16)}\nIP：{status.ip_address or '未分配'}",
            5,
            34,
            size=9,
        )
        scanning = "wifi_scan" in self.tasks
        self._button("正在扫描…" if scanning else "扫描 Wi-Fi", 8, 90, 104, 28, self._scan_wifi, "disabled" if scanning else "normal")
        self._button(
            "进入电台",
            124,
            90,
            104,
            28,
            lambda: self._go(SCREEN_RADIO),
            "normal" if status.connected else "disabled",
        )
        self._text("点击网络名称，输入密码：", 5, 121, size=8)
        start = self.network_page * self.NETWORKS_PER_PAGE
        visible = self.networks[start : start + self.NETWORKS_PER_PAGE]
        for row, network in enumerate(visible):
            self._button(
                f"{self._clip(network.ssid, 18)}  {network.signal}%",
                8,
                140 + row * 29,
                220,
                27,
                lambda n=network: self._select_network(n),
            )
        if not visible and not scanning:
            self._text("尚未扫描。Wi-Fi 仅支持 2.4G 热点。", 8, 148, size=9, color="#65737E")
        total_pages = max(1, (len(self.networks) + self.NETWORKS_PER_PAGE - 1) // self.NETWORKS_PER_PAGE)
        self._button("上一页", 8, 257, 52, 24, self._previous_network_page, "normal" if self.network_page > 0 else "disabled")
        self._button("下一页", 64, 257, 52, 24, self._next_network_page, "normal" if self.network_page + 1 < total_pages else "disabled")
        self.gui.draw_text(x=142, y=269, text=f"{self.network_page + 1}/{total_pages}", color="#65737E", font_size=9, origin="center")
        self._button("退出", 168, 257, 64, 24, self._exit_app)
        self._message(286)

    def _render_password(self) -> None:
        self._header("输入 Wi-Fi 密码")
        network = self.selected_network
        if network is None:
            self._go(SCREEN_WIFI)
            return
        self._text(f"网络：{self._clip(network.ssid, 20)}", 5, 34, size=9)
        if network.is_open:
            self._text("开放网络，无需输入密码", 5, 58, size=9, color="#247A48")
        else:
            masked = "*" * len(self.password) if self.password else "（未输入）"
            self.gui.fill_round_rect(x=5, y=56, w=230, h=26, r=4, color="#EAF2F8")
            self.gui.draw_text(x=10, y=69, text=masked[-24:], color="#1D2A33", font_size=11, origin="center")
        for index, (label, mode) in enumerate((("abc", "lower"), ("ABC", "upper"), ("123", "digits"), ("#=", "symbols"))):
            self._button(label, 4 + index * 58, 85, 54, 22, lambda m=mode: self._set_keyboard_mode(m))
        for index, char in enumerate(self._keyboard_chars(self.keyboard_mode)):
            row, column = divmod(index, 8)
            self._button(char, 3 + column * 29, 110 + row * 28, 27, 26, lambda c=char: self._append_password(c))
        self._button("退格", 3, 224, 52, 28, self._password_backspace)
        self._button("清空", 59, 224, 52, 28, self._password_clear)
        self._button("返回", 115, 224, 52, 28, lambda: self._go(SCREEN_WIFI))
        self._button("连接", 171, 224, 66, 28, self._connect_selected_wifi)
        self._message(286)

    def _render_radio(self) -> None:
        self._header("M10_Radio · 电台")
        if not self.wifi_status.connected:
            self._text("Wi-Fi 未连接，请先进入 Wi-Fi 页面完成连接。", 5, 34, size=8, color="#B03A2E")
        else:
            self._text(
                f"Wi-Fi：{self._clip(self.wifi_status.ssid, 18)}\nIP：{self.wifi_status.ip_address or '获取中'}",
                5,
                34,
                size=8,
            )
        station = self.stations[self.station_index] if self.stations else None
        self._text(f"当前电台：{self._clip(station.name if station else '未配置', 16)}", 5, 68, size=10)
        self._text(
            f"播放状态：{self.radio_state}    音量：{self.volume}%",
            5,
            89,
            size=8,
            color="#247A48" if self.radio_state == "播放中" else "#65737E",
        )
        start = self.station_page * self.STATIONS_PER_PAGE
        visible = self.stations[start : start + self.STATIONS_PER_PAGE]
        for row, item in enumerate(visible):
            index = start + row
            self._button(
                ("▶ " if index == self.station_index else "") + self._clip(item.name, 20),
                8,
                108 + row * 27,
                220,
                24,
                lambda i=index: self._select_station(i),
            )
        if not visible:
            self._text("请在 stations.json 中配置至少一个有效电台。", 8, 115, size=9, color="#B03A2E")
        total_pages = max(1, (len(self.stations) + self.STATIONS_PER_PAGE - 1) // self.STATIONS_PER_PAGE)
        self._button("上一页", 8, 189, 60, 22, self._previous_station_page, "normal" if self.station_page > 0 else "disabled")
        self.gui.draw_text(x=120, y=200, text=f"{self.station_page + 1}/{total_pages}", color="#65737E", font_size=8, origin="center")
        self._button("下一页", 172, 189, 60, 22, self._next_station_page, "normal" if self.station_page + 1 < total_pages else "disabled")
        enabled = "normal" if self.wifi_status.connected and self.stations else "disabled"
        self._button("上一台", 2, 214, 58, 28, self._previous_station, enabled)
        self._button("播放/暂停", 63, 214, 75, 28, self._toggle_play, enabled)
        self._button("下一台", 141, 214, 58, 28, self._next_station, enabled)
        self._button("停止", 202, 214, 36, 28, self._stop_play, "normal" if self.radio_state != "未播放" else "disabled")
        self._button("音量 −", 2, 245, 54, 26, lambda: self._adjust_volume(-5), "normal" if self.stations else "disabled")
        self._button("音量 +", 60, 245, 54, 26, lambda: self._adjust_volume(5), "normal" if self.stations else "disabled")
        self._button("蓝牙", 119, 245, 54, 26, lambda: self._go(SCREEN_BLUETOOTH))
        self._button("Wi-Fi", 179, 245, 59, 26, lambda: self._go(SCREEN_WIFI))
        self._message(286)

    def _render_bluetooth(self) -> None:
        self._header("M10_Radio · 蓝牙")
        self._text(
            f"蓝牙状态：{self.bluetooth_state}",
            5,
            34,
            size=10,
            color="#247A48" if self.bluetooth_state == "已连接" else "#65737E",
        )
        scanning = "bt_scan" in self.tasks
        pairing = "bt_pair" in self.tasks
        self._button("正在搜索…" if scanning else "搜索设备", 8, 57, 104, 28, self._scan_bluetooth, "disabled" if scanning else "normal")
        self._button("返回电台", 124, 57, 104, 28, lambda: self._go(SCREEN_RADIO))
        # Four lines at font size 8 = 64px (88..152).
        self._text(
            "连接说明：\n1. 耳机开机并进入配对模式\n2. 点击下方设备名称配对\n3. 连接成功后再播放电台",
            5,
            88,
            size=8,
        )
        start = self.bluetooth_page * self.DEVICES_PER_PAGE
        visible = self.bluetooth_devices[start : start + self.DEVICES_PER_PAGE]
        for row, device in enumerate(visible):
            state = "已连接" if device.connected else ("已配对" if device.paired else "点击配对")
            self._button(
                f"{self._clip(device.name, 14)} {state}",
                8,
                160 + row * 28,
                220,
                26,
                lambda d=device: self._pair_bluetooth(d),
                "disabled" if pairing else "normal",
            )
        if not visible and not scanning:
            self._text("暂时没有设备，请让耳机进入配对模式后搜索。", 8, 165, size=9, color="#65737E")
        total_pages = max(1, (len(self.bluetooth_devices) + self.DEVICES_PER_PAGE - 1) // self.DEVICES_PER_PAGE)
        self._button("上一页", 8, 246, 60, 24, self._previous_bluetooth_page, "normal" if self.bluetooth_page > 0 else "disabled")
        self.gui.draw_text(x=120, y=258, text=f"{self.bluetooth_page + 1}/{total_pages}", color="#65737E", font_size=8, origin="center")
        self._button("下一页", 172, 246, 60, 24, self._next_bluetooth_page, "normal" if self.bluetooth_page + 1 < total_pages else "disabled")
        self._message(286)

    # ------------------------------------------------------------------
    # Touch callbacks
    # ------------------------------------------------------------------
    def _go(self, screen: str) -> None:
        self.screen = screen
        self.message = ""
        if screen == SCREEN_BLUETOOTH:
            # Re-check on entry so a headset connected outside the app shows up.
            self._start_task("bt_status", self.bluetooth.connected_device)
        self._render()

    def _exit_app(self) -> None:
        """Leave the app from the touch screen.

        unihiker runs Tk's mainloop on a non-daemon thread, so returning from
        run() is not enough to end the process: os._exit() is what actually
        gives the Home screen back.
        """

        self.running = False
        try:
            self.player.shutdown()
        finally:
            os._exit(0)

    def _scan_wifi(self) -> None:
        self.message = "正在扫描附近 Wi-Fi…"
        self._render()
        self._start_task("wifi_scan", self.wifi.scan)

    def _select_network(self, network: WifiNetwork) -> None:
        self.selected_network = network
        self.password = ""
        self.keyboard_mode = "lower"
        self._go(SCREEN_PASSWORD)

    def _connect_selected_wifi(self) -> None:
        if self.selected_network is None:
            self._go(SCREEN_WIFI)
            return
        network = self.selected_network
        self.message = "正在连接 Wi-Fi，请稍候…"
        self._render()
        self._start_task(
            "wifi_connect",
            lambda: self.wifi.connect(network.ssid, self.password, open_network=network.is_open),
        )

    def _set_keyboard_mode(self, mode: str) -> None:
        self.keyboard_mode = mode
        self._render()

    def _append_password(self, char: str) -> None:
        if len(self.password) < 63:
            self.password += char
        self._render()

    def _password_backspace(self) -> None:
        self.password = self.password[:-1]
        self._render()

    def _password_clear(self) -> None:
        self.password = ""
        self._render()

    def _select_station(self, index: int) -> None:
        if not self.stations:
            return
        self.station_index = max(0, min(len(self.stations) - 1, index))
        self.station_page = self.station_index // self.STATIONS_PER_PAGE
        self._play_current_station()

    def _play_current_station(self) -> None:
        if not self.wifi_status.connected:
            self.message = "Wi-Fi 未连接，不能访问和播放网络电台"
            self._render()
            return
        if not self.stations:
            self.message = "stations.json 中没有可用电台"
            self._render()
            return
        self.radio_state = "连接中"
        self.message = "正在连接电台…"
        station = self.stations[self.station_index]
        self._render()
        self._start_task("play_station", lambda: self.player.play(station.url))

    def _toggle_play(self) -> None:
        if self.radio_state == "已暂停":
            self._start_task("resume_station", self.player.resume)
        elif self.radio_state == "播放中":
            self._start_task("pause_station", self.player.pause)
        else:
            self._play_current_station()

    def _stop_play(self) -> None:
        self.player.stop()
        self.radio_state = "已停止"
        self._render()

    def _previous_station(self) -> None:
        if not self.stations:
            return
        self.station_index = (self.station_index - 1) % len(self.stations)
        self.station_page = self.station_index // self.STATIONS_PER_PAGE
        self._play_current_station()

    def _next_station(self) -> None:
        if not self.stations:
            return
        self.station_index = (self.station_index + 1) % len(self.stations)
        self.station_page = self.station_index // self.STATIONS_PER_PAGE
        self._play_current_station()

    def _adjust_volume(self, delta: int) -> None:
        try:
            self.volume = self.player.adjust_volume(delta)
        except RadioPlayerError as exc:
            self.message = str(exc)
        self._render()

    def _scan_bluetooth(self) -> None:
        self.bluetooth_state = "搜索中"
        self.message = "请确认蓝牙耳机已打开并处于配对模式"
        self._render()
        self._start_task("bt_scan", self.bluetooth.scan)

    def _pair_bluetooth(self, device: BluetoothDevice) -> None:
        self.bluetooth_state = "配对连接中"
        self.message = f"正在连接 {device.name}…"
        self._render()
        self._start_task("bt_pair", lambda: self.bluetooth.pair_and_connect(device.address))

    # ------------------------------------------------------------------
    # Paging and keyboard helpers
    # ------------------------------------------------------------------
    def _previous_network_page(self) -> None:
        self.network_page = max(0, self.network_page - 1)
        self._render()

    def _next_network_page(self) -> None:
        max_page = max(0, (len(self.networks) - 1) // self.NETWORKS_PER_PAGE)
        self.network_page = min(max_page, self.network_page + 1)
        self._render()

    def _previous_station_page(self) -> None:
        self.station_page = max(0, self.station_page - 1)
        self._render()

    def _next_station_page(self) -> None:
        max_page = max(0, (len(self.stations) - 1) // self.STATIONS_PER_PAGE)
        self.station_page = min(max_page, self.station_page + 1)
        self._render()

    def _previous_bluetooth_page(self) -> None:
        self.bluetooth_page = max(0, self.bluetooth_page - 1)
        self._render()

    def _next_bluetooth_page(self) -> None:
        max_page = max(0, (len(self.bluetooth_devices) - 1) // self.DEVICES_PER_PAGE)
        self.bluetooth_page = min(max_page, self.bluetooth_page + 1)
        self._render()

    @staticmethod
    def _keyboard_chars(mode: str) -> List[str]:
        pages = {
            "lower": list("qwertyuiopasdfghjklzxcvbnm"),
            "upper": list("QWERTYUIOPASDFGHJKLZXCVBNM"),
            "digits": list("0123456789-_. "),
            "symbols": list("!@#$%^&*()[]{}+=,;:'?/\\|<>~`\""),
        }
        return pages.get(mode, pages["lower"])[:32]

    @staticmethod
    def _load_stations() -> Tuple[List[Station], str]:
        path = Path(__file__).with_name("stations.json")
        try:
            with path.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except FileNotFoundError:
            return [], "找不到 stations.json，请创建电台配置文件"
        except (OSError, json.JSONDecodeError) as exc:
            return [], f"电台配置读取失败：{exc}"
        if not isinstance(raw, list):
            return [], "电台配置必须是 JSON 数组"
        stations: List[Station] = []
        invalid = 0
        for item in raw:
            if not isinstance(item, dict):
                invalid += 1
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            parsed = urlparse(url)
            if not name or parsed.scheme not in ("http", "https") or not parsed.netloc:
                invalid += 1
                continue
            stations.append(Station(name, url))
        if not stations:
            return [], "没有有效电台，请检查 stations.json 中的名称和 URL"
        return stations, (f"已忽略 {invalid} 条无效电台配置" if invalid else "")

    def run(self) -> None:
        try:
            while self.running:
                self._drain_events()
                now = time.monotonic()
                if now >= self.next_status_poll and "wifi_status" not in self.tasks:
                    self.next_status_poll = now + 5
                    self._start_task("wifi_status", self.wifi.status)
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.player.shutdown()


def main() -> None:
    app = RadioApp()

    def terminate(_signum: int, _frame: object) -> None:
        # pkill or a systemd stop would otherwise leave mpv playing forever.
        app._exit_app()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, terminate)
        except (ValueError, OSError):
            pass

    app.run()


if __name__ == "__main__":
    main()
