# -*- coding: utf-8 -*-
"""Bluetooth headset discovery and connection through BlueZ bluetoothctl."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import subprocess
import time
from typing import List, Optional, Sequence


class BluetoothError(RuntimeError):
    """A user-displayable Bluetooth error."""


@dataclass(frozen=True)
class BluetoothDevice:
    address: str
    name: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False

    @property
    def has_name(self) -> bool:
        """False for nameless BLE beacons, which BlueZ aliases to the address."""

        return bool(self.name.strip())


class BluetoothManager:
    # BlueZ gives a nameless device an Alias that is just its own address
    # (e.g. "6E-B8-20-6E-39-5D"), which is not a usable display name.
    _ADDRESS_NAME = re.compile(r"^[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}$")
    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, scan_seconds: int = 8) -> None:
        self.scan_seconds = max(4, min(20, scan_seconds))
        self.last_scan_hidden = 0

    @staticmethod
    def _check_command() -> None:
        if shutil.which("bluetoothctl") is None:
            raise BluetoothError("未找到 bluetoothctl，请安装 bluez")

    @staticmethod
    def _run(args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BluetoothError("未找到 bluetoothctl，请安装 bluez") from exc
        except subprocess.TimeoutExpired as exc:
            raise BluetoothError("蓝牙操作超时，请检查蓝牙开关") from exc
        except OSError as exc:
            raise BluetoothError(f"无法执行蓝牙操作：{exc}") from exc

    def scan(self) -> List[BluetoothDevice]:
        self._check_command()
        # Keep one bluetoothctl session alive while discovery is active.  A
        # plain ``bluetoothctl scan on`` command can return before discovery
        # has had time to find nearby headsets, so the session is held open
        # explicitly and closed after the scan interval.
        try:
            process = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if process.stdin is None:
                raise BluetoothError("无法控制蓝牙扫描")
            process.stdin.write("power on\nagent NoInputNoOutput\ndefault-agent\nscan on\n")
            process.stdin.flush()
            time.sleep(self.scan_seconds)
            process.stdin.write("scan off\nquit\n")
            process.stdin.flush()
            process.communicate(timeout=6)
        except FileNotFoundError as exc:
            raise BluetoothError("未找到 bluetoothctl，请安装 bluez") from exc
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise BluetoothError("蓝牙扫描超时，请检查蓝牙状态") from exc
        except OSError as exc:
            raise BluetoothError(f"无法执行蓝牙扫描：{exc}") from exc
        listed = self._run(["bluetoothctl", "devices"], timeout=8)
        devices = self.parse_devices(listed.stdout)
        enriched: List[BluetoothDevice] = []
        hidden = 0
        for device in devices:
            current = self.info(device.address, fallback=device)
            if current.has_name:
                enriched.append(current)
            else:
                # Nameless BLE beacons (sensors, appliances, trackers) cannot
                # be used as an audio output and only clutter the list.
                hidden += 1
        self.last_scan_hidden = hidden
        if not enriched:
            raise BluetoothError("没有发现可用的蓝牙设备，请让耳机进入配对模式后重试")
        return enriched

    @classmethod
    def parse_devices(cls, output: str) -> List[BluetoothDevice]:
        devices: List[BluetoothDevice] = []
        pattern = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s*(.*)$")
        seen = set()
        for line in output.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            address, name = match.groups()
            if address.upper() in seen:
                continue
            seen.add(address.upper())
            name = name.strip()
            if cls._ADDRESS_NAME.match(name):
                name = ""
            devices.append(BluetoothDevice(address.upper(), name))
        return devices

    def info(self, address: str, fallback: Optional[BluetoothDevice] = None) -> BluetoothDevice:
        result = self._run(["bluetoothctl", "info", address], timeout=8)
        if result.returncode != 0:
            return fallback or BluetoothDevice(address)
        paired = self._property_yes(result.stdout, "Paired")
        trusted = self._property_yes(result.stdout, "Trusted")
        connected = self._property_yes(result.stdout, "Connected")
        name = fallback.name if fallback else ""
        for key in ("Name", "Alias"):
            # Same tab indentation as _property_yes above.
            match = re.search(rf"^\s*{key}:\s*(.+)$", result.stdout, flags=re.MULTILINE)
            if not match:
                continue
            candidate = match.group(1).strip()
            # An Alias that is only the address means the device advertises no
            # name at all, so it is not a usable display name.
            if candidate and not self._ADDRESS_NAME.match(candidate):
                name = candidate
                break
        return BluetoothDevice(address.upper(), name, paired, trusted, connected)

    def pair_and_connect(self, address: str, timeout: int = 20) -> BluetoothDevice:
        self._check_command()
        if not re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", address):
            raise BluetoothError("蓝牙设备地址无效")

        # Each step runs as its own bluetoothctl invocation.  Piping the whole
        # script into one session let "connect" fire while "pair" was still in
        # flight, which is why every device reported "paired but not connected".
        self._run(["bluetoothctl", "power on"], timeout=10)

        current = self.info(address)
        if not current.paired:
            result = self._run(
                ["bluetoothctl", "--agent", "NoInputNoOutput", "--timeout", str(timeout), "pair", address],
                timeout=timeout + 10,
            )
            output = result.stdout or ""
            if not self._pair_succeeded(output):
                raise BluetoothError(self._friendly_error(output, "蓝牙配对失败"))

        self._run(["bluetoothctl", "trust", address], timeout=10)

        result = self._run(
            ["bluetoothctl", "--timeout", str(timeout), "connect", address],
            timeout=timeout + 10,
        )
        device = self._wait_for_connection(address, timeout=12)
        if device is None or not device.connected:
            raise BluetoothError(self._connect_failure_reason(result.stdout or ""))
        self._set_default_bluetooth_sink(address)
        return device

    @staticmethod
    def _pair_succeeded(output: str) -> bool:
        lower = output.casefold()
        # BlueZ spells the already-paired error both "AlreadyExists" and
        # "already exists" depending on version.
        if "pairing successful" in lower or "alreadyexists" in lower or "already exists" in lower or "already paired" in lower:
            return True
        return "failed to pair" not in lower and "authentication" not in lower

    def _wait_for_connection(self, address: str, timeout: float = 12.0) -> Optional[BluetoothDevice]:
        """Connection is asynchronous, so poll instead of checking once."""

        deadline = time.monotonic() + timeout
        device: Optional[BluetoothDevice] = None
        while True:
            device = self.info(address)
            if device.connected:
                return device
            if time.monotonic() >= deadline:
                return device
            time.sleep(1.0)

    @staticmethod
    def _connect_failure_reason(output: str) -> str:
        lower = output.casefold()
        if "not available" in lower or "not ready" in lower:
            return "设备已不在范围内，请确认耳机已开机并靠近行空板"
        return "该设备已配对但未建立音频连接。请确认它是蓝牙耳机或音箱，而不是传感器、家电等其他蓝牙设备"

    def disconnect(self, address: str) -> None:
        self._check_command()
        result = self._run(["bluetoothctl", "disconnect", address], timeout=12)
        if result.returncode != 0:
            raise BluetoothError(self._friendly_error(result.stdout, "蓝牙断开失败"))

    def connected_device(self) -> Optional[BluetoothDevice]:
        if shutil.which("bluetoothctl") is None:
            return None
        listed = self._run(["bluetoothctl", "devices"], timeout=8)
        for device in self.parse_devices(listed.stdout):
            current = self.info(device.address, fallback=device)
            if current.connected:
                return current
        return None

    @staticmethod
    def _property_yes(output: str, property_name: str) -> bool:
        # bluetoothctl indents every property line with a tab ("\tConnected:
        # yes"), so the pattern has to tolerate leading whitespace -- without
        # \s* this silently reported every device as unpaired and disconnected.
        match = re.search(
            rf"^\s*{re.escape(property_name)}:\s*(yes|no)\s*$",
            output,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        return bool(match and match.group(1).casefold() == "yes")

    @classmethod
    def _friendly_error(cls, output: str, default: str) -> str:
        text = cls._ANSI.sub("", output)
        lower = text.casefold()
        if "no default controller" in lower or "no controller" in lower:
            return "没有找到蓝牙适配器，请检查蓝牙状态"
        if "alreadyexists" in lower or "already exists" in lower:
            return "该设备已经配对过了"
        if "authenticationtimeout" in lower or "authentication timeout" in lower or "authenticationcanceled" in lower:
            return "配对超时，请让耳机进入配对模式，并先断开它与手机的连接"
        if "authentication" in lower or "passkey" in lower:
            return "蓝牙认证失败，请确认耳机处于配对模式"
        if "not available" in lower or "not ready" in lower:
            return "蓝牙不可用，请打开蓝牙并重试"
        # Only surface the error lines; the rest is bluetoothctl prompt noise.
        detail = " ".join(
            line.strip() for line in text.splitlines()
            if "fail" in line.casefold() or "error" in line.casefold()
        )
        return f"{default}：{detail[:80]}" if detail else default

    @staticmethod
    def _set_default_bluetooth_sink(address: str) -> None:
        """Best-effort audio routing; connection itself does not depend on pactl."""

        if shutil.which("pactl") is None:
            return
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            compact = address.replace(":", "_").casefold()
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2 and ("bluez_output" in fields[1].casefold() or compact in fields[1].casefold()):
                    subprocess.run(
                        ["pactl", "set-default-sink", fields[1]],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                    return
        except (OSError, subprocess.SubprocessError):
            return


__all__ = ["BluetoothDevice", "BluetoothError", "BluetoothManager"]
