# -*- coding: utf-8 -*-
"""Wi-Fi control for M10_Radio.

The M10 runs Linux and exposes NetworkManager through ``nmcli``.  This module
keeps all command execution in one place and never invokes a shell, so SSIDs
and passwords containing shell metacharacters are passed safely to nmcli.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import shutil
import subprocess
from typing import List, Optional, Sequence, Tuple


class WifiError(RuntimeError):
    """A user-displayable Wi-Fi error."""


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int
    security: str

    @property
    def is_open(self) -> bool:
        return self.security in ("", "--", "开放", "开放网络")


@dataclass(frozen=True)
class WifiStatus:
    connected: bool
    ssid: str = ""
    ip_address: str = ""
    state: str = "未连接"
    error: str = ""


class WifiManager:
    """NetworkManager wrapper used by the touch UI."""

    def __init__(self, interface: str = "wlan0") -> None:
        self.interface = interface

    @staticmethod
    def _check_command() -> None:
        if shutil.which("nmcli") is None:
            raise WifiError("未找到 nmcli，请安装 network-manager")

    @staticmethod
    def _run(args: Sequence[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
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
            raise WifiError("未找到 nmcli，请安装 network-manager") from exc
        except subprocess.TimeoutExpired as exc:
            raise WifiError("Wi-Fi 操作超时，请检查路由器和信号") from exc
        except OSError as exc:
            raise WifiError(f"无法执行 Wi-Fi 操作：{exc}") from exc

    @staticmethod
    def _split_terse_line(line: str) -> List[str]:
        """Split nmcli terse output while honoring backslash escaping.

        nmcli uses ':' as its terse separator and escapes separators contained
        in values.  This parser also handles escaped backslashes.
        """

        values: List[str] = []
        current: List[str] = []
        escaped = False
        for char in line.rstrip("\r\n"):
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == ":":
                values.append("".join(current))
                current = []
            else:
                current.append(char)
        if escaped:
            current.append("\\")
        values.append("".join(current))
        return values

    @classmethod
    def parse_scan_output(cls, output: str) -> List[WifiNetwork]:
        """Parse ``nmcli -t -f SSID,SIGNAL,SECURITY device wifi list``."""

        networks = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = cls._split_terse_line(line)
            if len(fields) < 3:
                continue
            ssid = fields[0].strip()
            if not ssid:
                # Hidden SSIDs cannot be selected by name from this screen.
                continue
            try:
                signal = max(0, min(100, int(fields[1] or 0)))
            except ValueError:
                signal = 0
            security = fields[2].strip() or "--"
            network = WifiNetwork(ssid=ssid, signal=signal, security=security)
            previous = networks.get(ssid)
            if previous is None or network.signal > previous.signal:
                networks[ssid] = network
        return sorted(networks.values(), key=lambda item: (-item.signal, item.ssid.casefold()))

    def scan(self) -> List[WifiNetwork]:
        self._check_command()
        result = self._run(
            [
                "nmcli",
                "-t",
                "-e",
                "yes",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "--rescan",
                "yes",
                "ifname",
                self.interface,
            ],
            timeout=25,
        )
        if result.returncode != 0:
            raise WifiError(self._friendly_error(result.stdout, "Wi-Fi 扫描失败"))
        networks = self.parse_scan_output(result.stdout)
        if not networks:
            raise WifiError("没有发现可用网络，请确认附近有 2.4G Wi-Fi")
        return networks

    def connect(self, ssid: str, password: str = "", open_network: bool = False) -> WifiStatus:
        if not ssid:
            raise WifiError("未选择 Wi-Fi 网络")
        self._check_command()
        if not open_network and not password:
            raise WifiError("请输入 Wi-Fi 密码")

        args = [
            "nmcli",
            "--wait",
            "25",
            "device",
            "wifi",
            "connect",
            ssid,
        ]
        if not open_network:
            args.extend(["password", password])
        args.extend(["ifname", self.interface])
        result = self._run(args, timeout=35)
        if result.returncode != 0:
            raise WifiError(self._friendly_error(result.stdout, "Wi-Fi 连接失败"))

        status = self.status()
        if not status.connected:
            raise WifiError("Wi-Fi 命令已返回，但连接状态仍未成功")
        return status

    def status(self) -> WifiStatus:
        if shutil.which("nmcli") is None:
            return WifiStatus(False, state="未找到 nmcli", error="请安装 network-manager")
        try:
            result = self._run(
                [
                    "nmcli",
                    "-t",
                    "-e",
                    "yes",
                    "-f",
                    "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS",
                    "device",
                    "show",
                    self.interface,
                ],
                timeout=8,
            )
        except WifiError as exc:
            return WifiStatus(False, state="状态未知", error=str(exc))

        if result.returncode != 0:
            return WifiStatus(False, state="未连接", error=result.stdout.strip())

        values = {}
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key] = value.strip()

        raw_state = values.get("GENERAL.STATE", "")
        lowered = raw_state.casefold()
        # "disconnected" contains the substring "connected", so the negative
        # spelling has to be ruled out before the positive substring test.
        connected = raw_state.startswith("100") or ("connected" in lowered and "disconnected" not in lowered)
        state = "已连接" if connected else self._state_text(raw_state)
        connection = values.get("GENERAL.CONNECTION", "")
        if connection in ("--", "-- "):
            connection = ""
        ip_value = next((value for key, value in values.items() if key.startswith("IP4.ADDRESS")), "")
        ip_address = self._extract_ip(ip_value)
        ssid = self._resolve_ssid(connection) if connected and connection else ""
        return WifiStatus(connected, ssid, ip_address, state)

    def _resolve_ssid(self, profile: str) -> str:
        """Map a NetworkManager profile name to the SSID it connects to.

        GENERAL.CONNECTION holds the *profile* name, not the SSID.  The stock
        M10 image names that profile literally "wifi", so the screen showed
        "SSID: wifi" no matter which network was joined.  The real SSID lives
        in the profile's 802-11-wireless.ssid property.
        """

        try:
            result = self._run(
                ["nmcli", "-t", "-e", "yes", "-f", "802-11-wireless.ssid", "connection", "show", profile],
                timeout=6,
            )
        except WifiError:
            return profile
        if result.returncode != 0:
            return profile
        for line in result.stdout.splitlines():
            fields = self._split_terse_line(line)
            if len(fields) >= 2 and fields[0].endswith("wireless.ssid"):
                return fields[1].strip() or profile
        return profile

    @staticmethod
    def _extract_ip(value: str) -> str:
        for candidate in re.split(r"[,\s]+", value):
            candidate = candidate.strip()
            if not candidate:
                continue
            candidate = candidate.split("/", 1)[0]
            try:
                parsed = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if parsed.version == 4:
                return candidate
        return ""

    @staticmethod
    def _state_text(raw_state: str) -> str:
        lower = raw_state.casefold()
        if "connecting" in lower or raw_state.startswith("20") or raw_state.startswith("40"):
            return "连接中"
        if "disconnect" in lower or raw_state.startswith("30"):
            return "未连接"
        return raw_state or "未连接"

    @staticmethod
    def _friendly_error(output: str, default: str) -> str:
        text = " ".join(line.strip() for line in output.splitlines() if line.strip())
        lower = text.casefold()
        if "no network" in lower or "not found" in lower:
            return "没有找到该 Wi-Fi，请重新扫描"
        if "secrets were required" in lower or "incorrect password" in lower or "密码" in text:
            return "Wi-Fi 密码错误或认证失败"
        if "timeout" in lower or "timed out" in lower:
            return "Wi-Fi 连接超时，请检查信号和密码"
        return f"{default}：{text[:100]}" if text else default


__all__ = ["WifiError", "WifiNetwork", "WifiStatus", "WifiManager"]
