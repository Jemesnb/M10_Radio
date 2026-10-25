# -*- coding: utf-8 -*-
"""Network stream playback through mpv's local JSON IPC socket."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from typing import Callable, List, Optional, Sequence


class RadioPlayerError(RuntimeError):
    """A user-displayable playback error."""


@dataclass(frozen=True)
class PlayerEvent:
    kind: str
    message: str


class MpvRadioPlayer:
    """Small mpv controller with a process monitor.

    ``on_event`` is called from a worker thread.  The UI must put events into
    its own queue instead of updating Tk widgets directly from that callback.
    """

    def __init__(
        self,
        on_event: Optional[Callable[[PlayerEvent], None]] = None,
        socket_path: str = "/tmp/m10_radio_mpv.sock",
        initial_volume: int = 60,
    ) -> None:
        self.on_event = on_event
        self.socket_path = socket_path
        self.volume = max(0, min(100, int(initial_volume)))
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._monitor: Optional[threading.Thread] = None
        self._stop_requested = False
        self._paused = False
        self._generation = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def _emit(self, kind: str, message: str) -> None:
        if self.on_event is not None:
            self.on_event(PlayerEvent(kind, message))

    def play(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise RadioPlayerError("电台地址必须以 http:// 或 https:// 开头")
        if shutil.which("mpv") is None:
            raise RadioPlayerError("未找到 mpv，请先安装 mpv")

        self.stop(emit_event=False)
        self._kill_stale_players()
        self._remove_socket()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._stop_requested = False
            self._paused = False
            try:
                self._process = subprocess.Popen(
                    [
                        "mpv",
                        "--no-video",
                        "--no-terminal",
                        "--really-quiet",
                        f"--input-ipc-server={self.socket_path}",
                        f"--volume={self.volume}",
                        url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError as exc:
                self._process = None
                raise RadioPlayerError(f"无法启动音频播放器：{exc}") from exc

        self._emit("connecting", "正在连接电台…")
        self._monitor = threading.Thread(
            target=self._monitor_process,
            args=(generation,),
            name="m10-radio-mpv-monitor",
            daemon=True,
        )
        self._monitor.start()

    def pause(self) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise RadioPlayerError("当前没有正在播放的电台")
        self._send(["set_property", "pause", True])
        with self._lock:
            self._paused = True
        self._emit("paused", "已暂停")

    def resume(self) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise RadioPlayerError("当前没有可继续播放的电台")
        self._send(["set_property", "pause", False])
        with self._lock:
            self._paused = False
        self._emit("playing", "播放中")

    def toggle_pause(self) -> None:
        if self.is_paused:
            self.resume()
        else:
            self.pause()

    def stop(self, emit_event: bool = True) -> None:
        with self._lock:
            process = self._process
            self._stop_requested = True
            self._process = None
            self._paused = False
            self._generation += 1
        if process is not None and process.poll() is None:
            try:
                self._send(["quit"], timeout=0.8)
            except RadioPlayerError:
                pass
            try:
                process.wait(timeout=1.2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
        self._remove_socket()
        if emit_event:
            self._emit("stopped", "已停止")

    def set_volume(self, value: int) -> int:
        with self._lock:
            self.volume = max(0, min(100, int(value)))
            current = self.volume
            running = self._process is not None and self._process.poll() is None
        if running:
            self._send(["set_property", "volume", current])
        self._emit("volume", f"音量 {current}%")
        return current

    def adjust_volume(self, delta: int) -> int:
        return self.set_volume(self.volume + delta)

    def shutdown(self) -> None:
        self.stop(emit_event=False)

    def _send(self, command: Sequence[object], timeout: float = 1.5) -> None:
        payload = json.dumps({"command": list(command)}, ensure_ascii=False).encode("utf-8") + b"\n"
        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(max(0.1, deadline - time.monotonic()))
                    client.connect(self.socket_path)
                    client.sendall(payload)
                    # mpv returns a JSON reply for normal commands.  A short
                    # read is enough and avoids waiting for event traffic.
                    try:
                        client.recv(4096)
                    except socket.timeout:
                        pass
                    return
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
                last_error = exc
                time.sleep(0.05)
        raise RadioPlayerError(f"无法控制 mpv：{last_error}")

    def _monitor_process(self, generation: int) -> None:
        start = time.monotonic()
        socket_ready = False
        playing_emitted = False
        while True:
            with self._lock:
                process = self._process
                current_generation = self._generation
                stop_requested = self._stop_requested
            if current_generation != generation:
                return
            if process is None:
                return
            if os.path.exists(self.socket_path):
                socket_ready = True
                if not playing_emitted:
                    self._emit("playing", "播放中")
                    playing_emitted = True
            return_code = process.poll()
            if return_code is not None:
                with self._lock:
                    if self._process is process:
                        self._process = None
                self._remove_socket()
                if stop_requested:
                    return
                if not socket_ready or time.monotonic() - start < 4:
                    self._emit("station_error", "电台地址无法访问或音频格式不受支持")
                elif return_code != 0:
                    self._emit("audio_error", "音频播放失败，请检查输出设备")
                else:
                    self._emit("stopped", "电台已停止")
                return
            if not socket_ready and time.monotonic() - start > 8:
                self._emit("station_error", "连接电台超时")
                self.stop(emit_event=False)
                return
            time.sleep(0.25)

    def _kill_stale_players(self) -> None:
        """Reap an mpv left over from a previous run that was killed outright.

        The socket path is fixed, so a stray mpv keeps playing on its own and
        would compete with the new one for the same socket, giving two streams
        at once.  Only this player's own socket path is matched.
        """

        marker = f"--input-ipc-server={self.socket_path}"
        try:
            entries = os.listdir("/proc")
        except OSError:
            return
        for name in entries:
            if not name.isdigit() or int(name) == os.getpid():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as handle:
                    cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if marker in cmdline:
                try:
                    os.kill(int(name), signal.SIGTERM)
                except OSError:
                    pass

    def _remove_socket(self) -> None:
        # The M10 ships Python 3.7, where Path.unlink() has no ``missing_ok``
        # keyword (added in 3.8).  os.unlink() plus ENOENT gives the same
        # behaviour without the version dependency.
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


__all__ = ["MpvRadioPlayer", "PlayerEvent", "RadioPlayerError"]
