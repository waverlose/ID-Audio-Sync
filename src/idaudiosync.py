# -*- coding: utf-8 -*-
"""
ID Audio Sync —— 局域网跨机声音同步（独立版）

特点：
  * 不依赖任何第三方软件（不需要 Input Director），单文件自安装，只有托盘图标
  * 两台机器 **UDP 广播自动配对**，不用填 IP；配对结果会记住，下次秒连
  * 角色可自动也可指定：发送方（这台出声）/ 接收方（这台听声）
  * 自动模式下双方用主机名做确定性仲裁，不会出现"两边都在听"或"两边都在发"
  * 传输：PCM 48kHz/立体声，20ms 帧，TCP + 静音门限心跳，局域网约 1.5 Mbps

命令行（一般用不到，双击即可）：
  IDAudioSync.exe                 安装（首次）并常驻托盘
  IDAudioSync.exe --install       只安装     --uninstall 卸载
  IDAudioSync.exe --dry-run       只探测打印
  IDAudioSync.exe receiver|sender 直接跑角色（排错用）
  IDAudioSync.exe tone-test       播放测试音    list-devices 列出音频设备
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import numpy as np  # noqa: E402
import soundcard as sc  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NAME = "ID Audio Sync"
EXE_NAME = "IDAudioSync.exe"
# 安装目标与运行数据目录。
# 默认：程序装到 Program Files，配置/日志放 %LOCALAPPDATA%。
# 如需完全不落 C 盘（绿色/便携），设置环境变量即可：
#   set IDAS_INSTALL_DIR=D:\Apps\ID Audio Sync
#   set IDAS_DATA_DIR=D:\Apps\ID Audio Sync\data
INSTALL_DIR = os.environ.get("IDAS_INSTALL_DIR") or r"C:\Program Files\ID Audio Sync"
DATA_DIR = os.environ.get("IDAS_DATA_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ID Audio Sync"
)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "idaudiosync.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "IDAudioSync"
MUTEX_NAME = "Global\\IDAudioSync_SingleInstance"

DEFAULT_PORT = 31235          # 音频 TCP 端口
DISCOVERY_PORT = 31236        # 自动配对 UDP 端口
MULTICAST_GROUP = "239.255.42.99"

MAGIC = b"IDAS1"              # 音频流握手
DMAGIC = b"IDAS2"             # 配对握手
PROBE = b"PROBE"
HERE = b"HERE"

DEFAULT_SR = 48000
DEFAULT_CH = 2
DEFAULT_BLOCK_MS = 20

DEFAULT_CONFIG: dict = {
    "role": "auto",        # auto | sender | receiver | off
    "peer": "",            # 上次配对成功的主机（自动学习，加速重连）
    "port": DEFAULT_PORT,
    "mute_local": False,   # 发送方：发送期间静音本机扬声器
    "muted": False,        # 接收方：静音播放
    "volume": 1.0,         # 接收方：播放增益
    "autostart": True,
    "samplerate": DEFAULT_SR,
    "channels": DEFAULT_CH,
    "block_ms": DEFAULT_BLOCK_MS,
    "token": "",
}

_LOG_SINK = None
_LAST_SELF_LOG = 0.0


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def set_log_file(path: str | None) -> None:
    global _LOG_SINK
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _LOG_SINK = open(path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        if sys.stdout is not None:
            print(f"无法打开日志文件 {path}: {exc}", flush=True)


def log(tag: str, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass
    if _LOG_SINK is not None:
        try:
            _LOG_SINK.write(line + "\n")
        except OSError:
            pass


def rms_of(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block.astype("float64")))))


def list_devices() -> None:
    print("== 输出设备 ==")
    for i, s in enumerate(sc.all_speakers()):
        print(f"  [{i}] {s.name}")
    print("\n== loopback 抓取设备 ==")
    for i, m in enumerate(sc.all_microphones(include_loopback=True)):
        if getattr(m, "isloopback", False):
            print(f"  [{i}] {m.name}")


def resolve_speaker(substr: str | None):
    speakers = sc.all_speakers()
    if substr:
        for s in speakers:
            if substr.lower() in s.name.lower():
                return s
        raise SystemExit(f"找不到匹配 '{substr}' 的输出设备")
    try:
        return sc.default_speaker()
    except Exception:  # noqa: BLE001
        if speakers:
            return speakers[0]
        raise SystemExit("没有可用的输出设备")


def resolve_loopback(substr: str | None):
    loops = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]
    if not loops:
        raise SystemExit("没有发现 loopback 设备，无法抓取系统声音")
    if substr:
        for m in loops:
            if substr.lower() in m.name.lower():
                return m
        raise SystemExit(f"找不到匹配 '{substr}' 的 loopback 设备")
    try:
        spk = sc.default_speaker()
        for m in loops:
            if m.id == spk.id:
                return m
    except Exception:  # noqa: BLE001
        pass
    return loops[0]


class SystemMute:
    """通过 pycaw 控制默认输出设备静音；不可用时静默降级。"""

    def __init__(self) -> None:
        self.iface = None
        self._orig: bool | None = None
        try:
            from pycaw.pycaw import AudioUtilities

            self.iface = AudioUtilities.GetSpeakers().EndpointVolume
        except Exception:  # noqa: BLE001
            self.iface = None

    @property
    def available(self) -> bool:
        return self.iface is not None

    def is_muted(self) -> bool | None:
        try:
            return bool(self.iface.GetMute()) if self.iface else None
        except Exception:  # noqa: BLE001
            return None

    def set_mute(self, muted: bool) -> bool:
        if not self.iface:
            return False
        try:
            if self._orig is None:
                self._orig = self.is_muted()
            self.iface.SetMute(1 if muted else 0, None)
            return True
        except Exception:  # noqa: BLE001
            return False

    def restore(self) -> None:
        if self.iface and self._orig is not None:
            try:
                self.iface.SetMute(1 if self._orig else 0, None)
            except Exception:  # noqa: BLE001
                pass
            self._orig = None


# --------------------------------------------------------------------------- #
# 网络接口（取 IP / 掩码 / 广播地址，用 GetIpAddrTable，不受系统语言影响）
# --------------------------------------------------------------------------- #
class _MIB_IPADDRROW(ctypes.Structure):
    _fields_ = [
        ("dwAddr", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
        ("dwMask", wintypes.DWORD), ("dwBCastAddr", wintypes.DWORD),
        ("dwReasmSize", wintypes.DWORD), ("unused1", ctypes.c_ushort),
        ("wType", ctypes.c_ushort),
    ]


def local_interfaces() -> list[tuple[str, str, str]]:
    """返回 [(ip, mask, broadcast)]，跳过回环。"""
    try:
        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.ULONG(0)
        iphlpapi.GetIpAddrTable(None, ctypes.byref(size), False)
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), False) != 0:
            return []
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(_MIB_IPADDRROW))
        out = []
        for i in range(count):
            r = rows[i]
            ip = socket.inet_ntoa(struct.pack("<I", r.dwAddr))
            mask = socket.inet_ntoa(struct.pack("<I", r.dwMask))
            # dwBCastAddr 在 Windows 上不可靠（常为 0/垃圾），自行按 IP|~MASK 计算
            bcast = socket.inet_ntoa(struct.pack("<I", (r.dwAddr | (~r.dwMask & 0xFFFFFFFF)) & 0xFFFFFFFF))
            if ip.startswith("127.") or r.dwAddr == 0:
                continue
            out.append((ip, mask, bcast))
        return out
    except Exception:  # noqa: BLE001
        return []


def primary_ip() -> str:
    for ip, mask, _ in local_interfaces():
        if not ip.startswith("169.254."):
            return ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def broadcast_targets() -> list[str]:
    targets = []
    for ip, mask, bcast in local_interfaces():
        if ip.startswith("169.254."):
            continue
        if bcast and bcast != "255.255.255.255":
            targets.append(bcast)
    targets += ["255.255.255.255", MULTICAST_GROUP]
    seen, uniq = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# --------------------------------------------------------------------------- #
# 音频分帧协议
# --------------------------------------------------------------------------- #
def send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("<I", len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (ConnectionError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def to_int16(block: np.ndarray) -> bytes:
    return (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def from_int16(payload: bytes, channels: int) -> np.ndarray:
    arr = np.frombuffer(payload, dtype="<i2").astype("float32") / 32767.0
    if channels > 1 and arr.size % channels == 0:
        arr = arr.reshape(-1, channels)
    return arr


@dataclass
class Stats:
    frames: int = 0
    bytes: int = 0
    underruns: int = 0
    dropped: int = 0
    started: float = field(default_factory=time.time)

    def kbps(self) -> float:
        return self.bytes * 8 / 1000.0 / max(1e-6, time.time() - self.started)


# --------------------------------------------------------------------------- #
# 自动配对（UDP 广播）
# --------------------------------------------------------------------------- #
class DiscoveryResponder(threading.Thread):
    """接收方：监听 UDP 31236，回应探测包，并定期宣告自己。"""

    def __init__(self, name: str, tcp_port: int, stop_event: threading.Event) -> None:
        super().__init__(daemon=True, name="discovery")
        self.name = name
        self.tcp_port = tcp_port
        self.stop_event = stop_event
        self.sock: socket.socket | None = None

    def _reply(self, addr, payload: bytes) -> None:
        try:
            self.sock.sendto(payload, addr)  # type: ignore[union-attr]
        except OSError:
            pass

    def run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            s.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError as exc:
            log("pair", f"无法监听配对端口 {DISCOVERY_PORT}: {exc}")
            return
        s.settimeout(0.5)
        self.sock = s
        log("pair", f"已开始广播本机身份（{self.name}，端口 {self.tcp_port}）")
        announce = f"{DMAGIC.decode()}|{HERE.decode()}|{self.name}|{self.tcp_port}".encode()
        last_announce = 0.0
        while not self.stop_event.is_set():
            try:
                data, addr = s.recvfrom(512)
            except socket.timeout:
                data, addr = b"", None
            except OSError:
                break
            if data.startswith(DMAGIC + b"|" + PROBE):
                self._reply(addr, announce)
            if time.time() - last_announce > 3.0:
                last_announce = time.time()
                for tgt in broadcast_targets():
                    try:
                        s.sendto(announce, (tgt, DISCOVERY_PORT))
                    except OSError:
                        pass
        s.close()


def local_ips() -> set[str]:
    return {ip for ip, _, _ in local_interfaces()}


def discover_receiver(timeout: float = 1.2, exclude_self: bool = True) -> tuple[str, int, str] | None:
    """广播探测接收方，返回 (ip, tcp_port, name)。默认排除本机自己，避免自环。"""
    global _LAST_SELF_LOG
    me_name = socket.gethostname().lower()
    me_ips = local_ips()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    s.bind(("0.0.0.0", 0))
    s.settimeout(0.3)
    probe = f"{DMAGIC.decode()}|{PROBE.decode()}|{socket.gethostname()}".encode()
    best: tuple[str, int, str] | None = None
    deadline = time.time() + timeout
    try:
        for tgt in broadcast_targets():
            try:
                s.sendto(probe, (tgt, DISCOVERY_PORT))
            except OSError:
                pass
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            parts = data.decode("utf-8", "replace").split("|")
            if len(parts) < 4 or parts[0] != DMAGIC.decode() or parts[1] != HERE.decode():
                continue
            name = parts[2]
            try:
                port = int(parts[3])
            except ValueError:
                continue
            if exclude_self and (name.lower() == me_name or addr[0] in me_ips):
                if time.time() - _LAST_SELF_LOG > 15.0:   # 别刷屏
                    _LAST_SELF_LOG = time.time()
                    log("pair", "广播只收到本机自己的应答，继续等真正的对端…")
                continue
            best = (addr[0], port, name)
            break
    finally:
        s.close()
    return best


def probe_peer_alive(host: str, tcp_port: int, timeout: float = 1.5) -> bool:
    try:
        s = socket.create_connection((host, tcp_port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 接收方
# --------------------------------------------------------------------------- #
class Receiver:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sr = int(cfg.get("samplerate", DEFAULT_SR))
        self.ch = int(cfg.get("channels", DEFAULT_CH))
        self.block_frames = int(self.sr * float(cfg.get("block_ms", DEFAULT_BLOCK_MS)) / 1000)
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.silence = np.zeros((self.block_frames, self.ch), dtype="float32")
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=48)
        self.stats = Stats()
        self.gain = 0.0
        self.target_gain = 1.0
        self.muted = bool(cfg.get("muted", False))
        self.peer: str | None = None
        self.peer_name = ""
        self.stop_flag = threading.Event()
        self._shutdown = threading.Event()
        self._responder: DiscoveryResponder | None = None
        self.state = "启动中"

    # ---- 播放 ----
    def play_loop(self, speaker) -> None:
        fade_frames = max(1.0, self.sr * 0.06)
        step = self.block_frames / fade_frames
        vol = float(self.cfg.get("volume", 1.0))
        with speaker.player(samplerate=self.sr, channels=self.ch, blocksize=self.block_frames) as player:
            while not self._shutdown.is_set():
                try:
                    block = self.q.get(timeout=0.25)
                except queue.Empty:
                    block = self.silence
                    self.stats.underruns += 1
                if block.shape[0] != self.block_frames:
                    fixed = np.zeros((self.block_frames, self.ch), dtype="float32")
                    n = min(self.block_frames, block.shape[0])
                    fixed[:n] = block[:n]
                    block = fixed
                want = 0.0 if self.muted else 1.0
                if self.gain < want:
                    self.gain = min(want, self.gain + step)
                elif self.gain > want:
                    self.gain = max(want, self.gain - step)
                if self.gain <= 0.0:
                    player.play(self.silence)
                else:
                    player.play(block * (self.gain * vol))

    # ---- 接收 ----
    def session(self, conn: socket.socket, addr: tuple) -> None:
        self.peer = addr[0]
        self.state = f"已连接发送方 {addr[0]}"
        log("net", f"发送方已连接：{addr[0]}:{addr[1]}")
        try:
            while not self._shutdown.is_set():
                head = recv_exact(conn, 4)
                if head is None:
                    break
                (size,) = struct.unpack("<I", head)
                if size == 0:
                    try:
                        self.q.put_nowait(self.silence.copy())
                    except queue.Full:
                        pass
                    continue
                if size > self.block_frames * self.ch * 2 * 8:
                    log("net", f"异常帧长 {size}，断开")
                    break
                payload = recv_exact(conn, size)
                if payload is None:
                    break
                self.stats.frames += 1
                self.stats.bytes += size
                try:
                    self.q.put_nowait(from_int16(payload, self.ch))
                except queue.Full:
                    try:
                        self.q.get_nowait()
                        self.stats.dropped += 1
                    except queue.Empty:
                        pass
                    try:
                        self.q.put_nowait(from_int16(payload, self.ch))
                    except queue.Full:
                        self.stats.dropped += 1
        finally:
            log("net", "发送方断开")
            self.peer = None
            self.state = "等待发送方连接"
            self.target_gain = 1.0

    def run(self, seconds: float = 0.0) -> int:
        speaker = resolve_speaker(self.cfg.get("device"))
        log("init", f"接收方就绪 · 输出设备 {speaker.name} · 端口 {self.port}")

        self._responder = DiscoveryResponder(socket.gethostname(), self.port, self.stop_flag)
        self._responder.start()

        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(4)
            srv.settimeout(1.0)
        except OSError as exc:
            log("err", f"端口 {self.port} 无法监听：{exc}")
            self.stop_flag.set()
            return 1

        self.state = "等待发送方连接"
        threading.Thread(target=self.play_loop, args=(speaker,), daemon=True, name="play").start()

        deadline = time.time() + seconds if seconds else None
        try:
            while not self._shutdown.is_set():
                if deadline and time.time() > deadline:
                    log("init", f"到达 --seconds {seconds}，退出")
                    break
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                hs = recv_exact(conn, len(MAGIC) + len(str(self.cfg.get("token", "")).encode()))
                if hs is None or not hs.startswith(MAGIC):
                    log("net", f"忽略一个没有握手的探测连接（{addr[0]}）")
                    conn.close()
                    continue
                token = str(self.cfg.get("token", ""))
                if token and hs[len(MAGIC):].decode("utf-8", "replace") != token:
                    log("net", "令牌不匹配，拒绝")
                    conn.close()
                    continue
                self.session(conn, addr)
                try:
                    conn.close()
                except OSError:
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_flag.set()
            if self._responder:
                self._responder.join(timeout=1.0)
            try:
                srv.close()
            except OSError:
                pass
        return 0

    def shutdown(self) -> None:
        self._shutdown.set()
        self.stop_flag.set()


# --------------------------------------------------------------------------- #
# 发送方
# --------------------------------------------------------------------------- #
class Sender:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sr = int(cfg.get("samplerate", DEFAULT_SR))
        self.ch = int(cfg.get("channels", DEFAULT_CH))
        self.block_frames = int(self.sr * float(cfg.get("block_ms", DEFAULT_BLOCK_MS)) / 1000)
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.stats = Stats()
        self.stop_flag = threading.Event()
        self.mute = SystemMute()
        self._muted = False
        self.connected = False
        self.last_error = ""
        self.peer: str | None = str(cfg.get("peer") or "") or None
        self.peer_name = ""
        self.state = "启动中"

    def find_peer(self, deadline: float | None = None) -> tuple[str, int] | None:
        """先试上次配对过的地址，再广播自动发现。"""
        if self.peer and self.peer in local_ips():
            log("pair", f"记住的地址 {self.peer} 是本机自己，忽略")
            self.peer = None
        if self.peer:
            if probe_peer_alive(self.peer, self.port, timeout=1.0):
                self.state = f"重连上次的接收方 {self.peer}"
                return self.peer, self.port
            log("pair", f"上次的接收方 {self.peer} 无响应，开始广播搜索")
            self.peer = None
        while not self.stop_flag.is_set():
            if deadline and time.time() > deadline:
                return None
            self.state = "正在搜索接收方…"
            found = discover_receiver(timeout=1.5)
            if found:
                ip, port, name = found
                log("pair", f"发现接收方：{name}（{ip}:{port}）")
                self.peer, self.port, self.peer_name = ip, port, name
                self.cfg["peer"] = ip
                save_config(self.cfg)
                return ip, port
            self.stop_flag.wait(1.0)
        return None

    def connect(self, deadline: float | None = None) -> socket.socket | None:
        while not self.stop_flag.is_set():
            if deadline and time.time() > deadline:
                return None
            target = self.find_peer(deadline)
            if target is None:
                return None
            try:
                sock = socket.create_connection(target, timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(MAGIC + str(self.cfg.get("token", "")).encode())
                self.connected = True
                self.last_error = ""
                self.state = f"已连上接收方 {target[0]}"
                log("net", f"已连接接收方 {target[0]}:{target[1]}")
                return sock
            except OSError as exc:
                self.connected = False
                self.last_error = str(exc)
                self.peer = None
                log("net", f"连接失败（{exc}），重新搜索")
                self.stop_flag.wait(2.0)
        return None

    def capture_loop(self, mic, sock: socket.socket, deadline: float | None) -> None:
        silent = 0
        gate = bool(self.cfg.get("gate", True))
        thr = float(self.cfg.get("gate_threshold", 0.0015))
        with mic.recorder(samplerate=self.sr, channels=self.ch, blocksize=self.block_frames) as rec:
            while not self.stop_flag.is_set():
                if deadline and time.time() > deadline:
                    return
                block = rec.record(numframes=self.block_frames)
                if gate and rms_of(block) < thr:
                    silent += 1
                    if silent >= 5:
                        send_frame(sock, b"")
                        self.stats.frames += 1
                        continue
                else:
                    silent = 0
                payload = to_int16(block)
                send_frame(sock, payload)
                self.stats.frames += 1
                self.stats.bytes += len(payload)

    def run(self, seconds: float = 0.0) -> int:
        mic = resolve_loopback(self.cfg.get("device"))
        log("init", f"发送方就绪 · 抓取 {mic.name} · 端口 {self.port}")
        deadline = time.time() + seconds if seconds else None
        try:
            while not self.stop_flag.is_set():
                if deadline and time.time() > deadline:
                    break
                sock = self.connect(deadline)
                if sock is None:
                    break
                if self.cfg.get("mute_local", False) and self.mute.set_mute(True):
                    self._muted = True
                try:
                    self.capture_loop(mic, sock, deadline)
                except OSError as exc:
                    self.last_error = str(exc)
                finally:
                    self.connected = False
                    try:
                        sock.close()
                    except OSError:
                        pass
                    if self._muted:
                        self.mute.restore()
                        self._muted = False
                if self.stop_flag.is_set() or (deadline and time.time() > deadline):
                    break
                self.state = "连接中断，重新配对…"
                log("net", "连接中断，准备重新配对")
                self.stop_flag.wait(1.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_flag.set()
            self.mute.restore()
        return 0

    def shutdown(self) -> None:
        self.stop_flag.set()


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    # 兼容旧版配置：mode(client/director) -> role(sender/receiver)
    legacy = cfg.pop("mode", None)
    if legacy:
        cfg["_migrated"] = True
        if cfg.get("role") in (None, "auto"):
            cfg["role"] = {"client": "sender", "director": "receiver"}.get(str(legacy), "auto")
    if "director_host" in cfg:
        if not cfg.get("peer"):
            cfg["peer"] = cfg["director_host"]
        cfg.pop("director_host", None)
        cfg["_migrated"] = True
    if cfg.get("peer") and cfg["peer"] in local_ips():
        cfg["peer"] = ""          # 旧配置可能记的是本机地址
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log("cfg", f"配置保存失败: {exc}")


# --------------------------------------------------------------------------- #
# 安装 / 卸载
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def elevate(extra_args: str = "") -> bool:
    try:
        return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, extra_args.strip(), None, 1)) > 32
    except Exception:  # noqa: BLE001
        return False


def message_box(text: str, flags: int = 0x40) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags))
    except Exception:  # noqa: BLE001
        return 0


def acquire_single_instance() -> bool:
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:  # noqa: BLE001
        return True


def installed_exe() -> str:
    return os.path.join(INSTALL_DIR, EXE_NAME)


def is_installed() -> bool:
    return os.path.isfile(installed_exe())


def running_from_install_dir() -> bool:
    try:
        return os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(installed_exe())
    except Exception:  # noqa: BLE001
        return False


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(v)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{installed_exe()}" --tray')
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except OSError:
                    pass
        return True
    except OSError as exc:
        log("auto", f"设置开机自启失败: {exc}")
        return False


def firewall_rules(action: str, port: int) -> None:
    kw = dict(capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for name, proto, p in (
        (f"{APP_NAME} (TCP {port})", "TCP", port),
        (f"{APP_NAME} (UDP {DISCOVERY_PORT})", "UDP", DISCOVERY_PORT),
    ):
        try:
            if action == "add":
                subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"], **kw)
                subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                                "action=allow", f"protocol={proto}", f"localport={p}", "profile=any"], **kw)
            else:
                subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"], **kw)
        except Exception as exc:  # noqa: BLE001
            log("fw", f"防火墙操作失败({name}): {exc}")


def do_install(dry_run: bool = False) -> int:
    cfg = load_config()
    if not is_admin():
        if dry_run:
            log("install", "[需要管理员] 实际执行时会弹 UAC 提权")
        else:
            log("install", "需要管理员权限，正在提权…")
            if elevate("--install"):
                return 0
            message_box("安装需要管理员权限。\n请右键该程序，选择「以管理员身份运行」。", 0x30)
            return 1

    log("install", f"安装目录: {INSTALL_DIR}")
    log("install", f"数据目录: {DATA_DIR}")
    log("install", f"防火墙: 放行 TCP {cfg.get('port')} 与 UDP {DISCOVERY_PORT}")
    log("install", f"开机自启: {'开启' if cfg.get('autostart', True) else '关闭'}")
    if dry_run:
        log("install", "（dry-run：未做任何改动）")
        return 0

    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as exc:
        message_box(f"创建目录失败: {exc}", 0x30)
        return 1

    if not running_from_install_dir():
        try:
            shutil.copy2(sys.executable, installed_exe())
            log("install", f"已复制到 {installed_exe()}")
        except OSError as exc:
            message_box(f"复制程序失败: {exc}\n（请先在托盘里退出旧版本再安装）", 0x30)
            return 1

    if not os.path.isfile(CONFIG_PATH):
        save_config(cfg)
        log("install", "已生成默认配置")

    firewall_rules("add", int(cfg.get("port", DEFAULT_PORT)))
    if cfg.get("autostart", True):
        set_autostart(True)

    if not running_from_install_dir():
        try:
            subprocess.Popen([installed_exe(), "--tray"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log("install", "已启动托盘程序")
        except OSError as exc:
            log("install", f"启动失败: {exc}")
    return 0


def _spawn_self_delete() -> None:
    bat = os.path.join(tempfile.gettempdir(), "idas_cleanup.bat")
    try:
        with open(bat, "w", encoding="mbcs") as fh:
            fh.write("@echo off\r\nping -n 3 127.0.0.1 >nul\r\n"
                     f'rmdir /s /q "{INSTALL_DIR}"\r\ndel /f /q "{bat}"\r\n')
        subprocess.Popen(["cmd", "/c", bat],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        log("uninstall", f"清理脚本创建失败: {exc}")


def do_uninstall() -> int:
    if not is_admin():
        if elevate("--uninstall"):
            return 0
        message_box("卸载需要管理员权限。\n请右键该程序，选择「以管理员身份运行」。", 0x30)
        return 1
    set_autostart(False)
    firewall_rules("remove", int(load_config().get("port", DEFAULT_PORT)))
    try:
        subprocess.run(["taskkill", "/f", "/im", EXE_NAME], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        pass
    log("uninstall", "已移除自启与防火墙规则")
    if running_from_install_dir():
        _spawn_self_delete()
    else:
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    return 0


# --------------------------------------------------------------------------- #
# 托盘总管：自动配对 + 角色仲裁
# --------------------------------------------------------------------------- #
class Supervisor:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name = socket.gethostname()
        self.worker = None
        self.worker_thread: threading.Thread | None = None
        self.role_running: str | None = None
        self.stop_event = threading.Event()
        self.icon = None
        self.status = "启动中…"
        self.auto_role: str | None = None
        self._role_lock = threading.Lock()

    # ---------- 角色 ----------
    def effective_role(self) -> str:
        mode = str(self.cfg.get("role", "auto"))
        if mode in ("sender", "receiver", "off"):
            return mode
        return self.auto_role or "receiver"

    def decide_auto_role(self) -> str:
        """自动模式：先找接收方；找到就当发送方，找不到就当接收方。
        若双方都是接收方，用主机名做确定性仲裁（名字小的当发送方）。"""
        self.status = "正在搜索对端…"
        self.refresh_title()
        found = discover_receiver(timeout=2.0)
        if found:
            ip, port, name = found
            if name == self.name:
                return "receiver"
            log("sup", f"发现接收方 {name}（{ip}），本机自动作为发送方")
            self.cfg["peer"] = ip
            save_config(self.cfg)
            return "sender"
        # 没找到 → 当接收方；再等一轮，若出现同名仲裁对手且我名字更小则改当发送方
        log("sup", "未发现接收方，本机自动作为接收方（听声音）")
        again = discover_receiver(timeout=1.5)
        if again and again[2] != self.name and self.name < again[2]:
            log("sup", f"检测到另一台接收方 {again[2]}，按名字仲裁本机改为发送方")
            self.cfg["peer"] = again[0]
            save_config(self.cfg)
            return "sender"
        return "receiver"

    def start_role(self) -> None:
        with self._role_lock:
            role = self.effective_role()
            if role == "off":
                self.status = "已关闭（托盘菜单可切换）"
                self.refresh_title()
                return
            if self.role_running == role and self.worker_thread and self.worker_thread.is_alive():
                return
            self._stop_role_locked()
            self.role_running = role
            self.worker = Receiver(self.cfg) if role == "receiver" else Sender(self.cfg)
            self.worker_thread = threading.Thread(target=self.worker.run, daemon=True, name=f"role-{role}")
            self.worker_thread.start()
        log("sup", f"角色已启动: {role}")
        self.refresh_title()

    def stop_role(self, quiet: bool = False) -> None:
        with self._role_lock:
            self._stop_role_locked()
        if not quiet:
            log("sup", "角色已停止")

    def _stop_role_locked(self) -> None:
        if self.worker is not None:
            try:
                self.worker.shutdown()
            except Exception:  # noqa: BLE001
                pass
            if self.worker_thread is not None:
                self.worker_thread.join(timeout=3.0)
            self.worker = None
            self.worker_thread = None
        self.role_running = None

    # ---------- 状态 ----------
    def live_status(self, item=None) -> str:
        mode = str(self.cfg.get("role", "auto"))
        if mode == "off":
            return "已关闭（菜单里可切换）"
        w = self.worker
        if self.role_running == "receiver":
            peer = getattr(w, "peer", None)
            if peer:
                return f"接收方 · 正在播放来自 {peer} 的声音"
            return "接收方 · 等待发送方（另一台）连接…"
        if self.role_running == "sender":
            if getattr(w, "connected", False):
                return f"发送方 · 已连上接收方 {getattr(w, 'peer', '')}"
            return f"发送方 · {getattr(w, 'state', '搜索中')}"
        return self.status

    status_line = live_status

    def refresh_title(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.title = f"{APP_NAME} · {self.live_status()}"
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def status_loop(self) -> None:
        while not self.stop_event.is_set():
            self.refresh_title()
            self.stop_event.wait(2.0)

    def diagnostics_text(self) -> str:
        try:
            dev = sc.default_speaker().name
        except Exception:  # noqa: BLE001
            dev = "读取失败"
        ifs = ", ".join(ip for ip, _, _ in local_interfaces()) or "无"
        return "\n".join([
            f"程序：{APP_NAME}（{EXE_NAME}）",
            f"本机名：{self.name}",
            f"本机地址：{ifs}",
            f"安装目录：{INSTALL_DIR}",
            f"当前从安装目录运行：{running_from_install_dir()}",
            f"配置角色：{self.cfg.get('role')}（自动模式判定为 {self.auto_role or '-'}）",
            f"上次配对地址：{self.cfg.get('peer') or '（还没配对过）'}",
            f"音频端口：{self.cfg.get('port')}  配对端口：{DISCOVERY_PORT}",
            f"当前状态：{self.live_status()}",
            f"输出设备：{dev}",
            f"发送时静音本机扬声器：{self.cfg.get('mute_local')}",
            f"接收静音：{self.cfg.get('muted')}  音量：{self.cfg.get('volume')}",
            f"日志：{LOG_PATH}",
        ])

    # ---------- 菜单 ----------
    def build_menu(self):
        import pystray

        def set_role(role: str):
            def _cb(icon, item):
                self.cfg["role"] = role
                save_config(self.cfg)
                log("sup", f"角色切换为 {role}")
                self.stop_role(quiet=True)
                if role != "off":
                    self.start_role()
                else:
                    self.status = "已关闭（托盘菜单可切换）"
                self.refresh_title()
            return _cb

        def role_checked(role: str):
            return lambda item: self.cfg.get("role") == role

        def on_swap(icon, item):
            cur = self.effective_role()
            new = "sender" if cur == "receiver" else "receiver"
            log("sup", f"交换角色：{cur} -> {new}")
            set_role(new)(icon, item)

        def on_mute(icon, item):
            self.cfg["muted"] = not self.cfg.get("muted", False)
            save_config(self.cfg)
            if isinstance(self.worker, Receiver):
                self.worker.muted = self.cfg["muted"]
            log("sup", f"接收静音: {self.cfg['muted']}")

        def set_volume(v: float):
            def _cb(icon, item):
                self.cfg["volume"] = v
                save_config(self.cfg)
                if isinstance(self.worker, Receiver):
                    self.worker.cfg["volume"] = v
            return _cb

        def vol_checked(v: float):
            return lambda item: abs(float(self.cfg.get("volume", 1.0)) - v) < 0.01

        def on_toggle_mute_local(icon, item):
            self.cfg["mute_local"] = not self.cfg.get("mute_local", False)
            save_config(self.cfg)
            log("sup", f"发送时静音本机扬声器: {self.cfg['mute_local']}")
            if self.role_running == "sender":
                self.stop_role(quiet=True)
                self.start_role()

        def on_research(icon, item):
            self.cfg["peer"] = ""
            save_config(self.cfg)
            self.auto_role = None
            log("sup", "重新搜索对端")
            self.stop_role(quiet=True)
            if self.cfg.get("role") != "off":
                self.start_role()

        def on_autostart(icon, item):
            want = not autostart_enabled()
            if set_autostart(want):
                self.cfg["autostart"] = want
                save_config(self.cfg)

        def on_tone(icon, item):
            threading.Thread(target=_play_tone, daemon=True).start()

        def on_diag(icon, item):
            message_box(self.diagnostics_text())

        def on_open_log(icon, item):
            try:
                os.startfile(LOG_PATH)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass

        def on_open_cfg(icon, item):
            save_config(self.cfg)
            try:
                os.startfile(CONFIG_PATH)  # noqa: S606
            except Exception:  # noqa: BLE001
                pass

        def on_quit(icon, item):
            self.stop_event.set()
            self.stop_role()
            icon.stop()

        def on_uninstall(icon, item):
            if message_box("确定卸载 ID Audio Sync 吗？\n会移除自启、防火墙规则与程序文件。", 0x24) != 6:
                return
            self.stop_event.set()
            self.stop_role()
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
            threading.Thread(target=do_uninstall, daemon=True).start()

        M, S = pystray.MenuItem, pystray.Menu.SEPARATOR
        return pystray.Menu(
            M(self.status_line, None, enabled=False),
            S,
            M("角色", pystray.Menu(
                M("自动配对（推荐）", set_role("auto"), checked=role_checked("auto"), radio=True),
                M("发送方：我这台出声", set_role("sender"), checked=role_checked("sender"), radio=True),
                M("接收方：我这台听声", set_role("receiver"), checked=role_checked("receiver"), radio=True),
                M("关闭", set_role("off"), checked=role_checked("off"), radio=True),
            )),
            M("交换角色", on_swap),
            S,
            M("静音（接收方）", on_mute, checked=lambda item: bool(self.cfg.get("muted", False))),
            M("接收音量", pystray.Menu(
                M("100%", set_volume(1.0), checked=vol_checked(1.0), radio=True),
                M("70%", set_volume(0.7), checked=vol_checked(0.7), radio=True),
                M("40%", set_volume(0.4), checked=vol_checked(0.4), radio=True),
                M("15%", set_volume(0.15), checked=vol_checked(0.15), radio=True),
            )),
            M("发送时静音本机扬声器", on_toggle_mute_local,
              checked=lambda item: bool(self.cfg.get("mute_local", False))),
            S,
            M("重新搜索对端", on_research),
            M("播放测试音（检查扬声器）", on_tone),
            M("诊断信息", on_diag),
            M("登录时自动启动", on_autostart, checked=lambda item: autostart_enabled()),
            S,
            M("查看日志", on_open_log),
            M("编辑配置文件", on_open_cfg),
            S,
            M("卸载并退出", on_uninstall),
            M("退出", on_quit),
        )

    def _icon_image(self):
        from PIL import Image, ImageDraw

        if self.cfg.get("role") == "off":
            color = (150, 150, 150, 255)
        elif self.role_running == "receiver":
            peer = getattr(self.worker, "peer", None)
            color = (0, 190, 120, 255) if peer else (255, 170, 0, 255)
        elif self.role_running == "sender":
            color = (0, 160, 255, 255) if getattr(self.worker, "connected", False) else (255, 170, 0, 255)
        else:
            color = (200, 200, 200, 255)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(6, 24), (18, 24), (32, 10), (32, 54), (18, 40), (6, 40)], fill=color)
        d.arc([32, 16, 50, 48], start=-62, end=62, fill=color, width=4)
        d.arc([40, 6, 64, 58], start=-62, end=62, fill=color, width=4)
        return img

    def run(self, seconds: float = 0.0) -> int:
        import pystray

        os.makedirs(DATA_DIR, exist_ok=True)
        set_log_file(LOG_PATH)
        log("sup", f"启动 · 本机 {self.name} · 地址 {', '.join(ip for ip, _, _ in local_interfaces()) or '未知'}")
        log("sup", f"角色配置：{self.cfg.get('role')}")

        mode = str(self.cfg.get("role", "auto"))
        if mode == "auto":
            self.auto_role = self.decide_auto_role()
            self.cfg["role"] = "auto"
            save_config(self.cfg)

        self.icon = pystray.Icon("id-audio-sync", self._icon_image(), APP_NAME, self.build_menu())
        self.start_role()          # 先起角色，再开守护线程，避免重复启动
        threading.Thread(target=self.watch_loop, daemon=True, name="watch").start()
        threading.Thread(target=self.status_loop, daemon=True, name="status").start()

        if seconds:
            def _auto_stop() -> None:
                time.sleep(seconds)
                log("sup", f"到达 --seconds {seconds}，自动退出")
                self.stop_event.set()
                self.stop_role()
                try:
                    self.icon.stop()
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_auto_stop, daemon=True, name="auto-stop").start()

        self.icon.run()
        self.stop_event.set()
        self.stop_role()
        return 0

    def watch_loop(self) -> None:
        """角色线程挂了就重启；自动模式下做名字仲裁，避免两边都在听。"""
        ticks = 0
        while not self.stop_event.is_set():
            alive = self.worker_thread is not None and self.worker_thread.is_alive()
            if not alive and self.cfg.get("role") != "off":
                log("sup", "角色线程已退出，重新启动")
                self.stop_role(quiet=True)
                if str(self.cfg.get("role", "auto")) == "auto":
                    self.auto_role = self.decide_auto_role()
                self.start_role()
            ticks += 1
            if (str(self.cfg.get("role", "auto")) == "auto"
                    and self.role_running == "receiver" and ticks % 4 == 0):
                other = discover_receiver(timeout=1.5)
                if other and other[2] != self.name and self.name > other[2]:
                    log("sup", f"发现另一台接收方 {other[2]}，按名字仲裁本机改为发送方")
                    self.cfg["peer"] = other[0]
                    save_config(self.cfg)
                    self.auto_role = "sender"
                    self.stop_role(quiet=True)
                    self.start_role()
            self.refresh_title()
            self.stop_event.wait(3.0)


def _play_tone(seconds: float = 1.5) -> None:
    try:
        run_tone_test(build_parser().parse_args(["tone-test", "--seconds", str(seconds)]))
    except Exception as exc:  # noqa: BLE001
        log("tone", f"测试音失败: {exc}")


def run_tone_test(args: argparse.Namespace) -> int:
    speaker = resolve_speaker(getattr(args, "device", None))
    sr = args.samplerate
    dur = args.seconds or 1.5
    log("test", f"向 {speaker.name} 播放 {dur} 秒 440Hz 测试音…")
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    wave = (0.25 * np.sin(2 * math.pi * 440 * t)).astype("float32")
    block = int(sr * args.block_ms / 1000)
    with speaker.player(samplerate=sr, channels=1, blocksize=block) as player:
        for i in range(0, wave.size, block):
            player.play(wave[i:i + block].reshape(-1, 1))
    log("test", "播放完成")
    return 0


def run_pair_test(args: argparse.Namespace) -> int:
    """自检：本机抓 loopback → TCP 自环 → 校验帧（不播放，避免啸叫）。"""
    sr, ch = args.samplerate, args.channels
    block_frames = int(sr * args.block_ms / 1000)
    mic = resolve_loopback(getattr(args, "device", None))
    port = 31399
    got = {"n": 0, "b": 0, "peak": 0.0}
    stop = threading.Event()
    ready = threading.Event()

    def server() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        try:
            if recv_exact(conn, len(MAGIC)) != MAGIC:
                return
            while not stop.is_set():
                head = recv_exact(conn, 4)
                if head is None:
                    break
                (size,) = struct.unpack("<I", head)
                if size:
                    payload = recv_exact(conn, size)
                    if payload is None:
                        break
                    arr = np.frombuffer(payload, dtype="<i2").astype("float32") / 32767.0
                    got["n"] += 1
                    got["b"] += size
                    got["peak"] = max(got["peak"], float(np.max(np.abs(arr))) if arr.size else 0.0)
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=server, daemon=True).start()
    ready.wait(3)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(MAGIC)
    log("test", f"抓取 {mic.name}，采集 {args.seconds or 5} 秒（期间放点声音，峰值会变）…")
    deadline = time.time() + (args.seconds or 5)
    with mic.recorder(samplerate=sr, channels=ch, blocksize=block_frames) as rec:
        while time.time() < deadline:
            try:
                send_frame(sock, to_int16(rec.record(numframes=block_frames)))
            except OSError:
                break
    stop.set()
    sock.close()
    kbps = got["b"] * 8 / 1000.0 / (args.seconds or 5)
    log("test", f"收到 {got['n']} 帧 / {got['b']/1024:.0f} KiB ≈ {kbps:.0f} kbps，峰值 {got['peak']:.4f}")
    log("test", "结论：" + ("抓取+传输正常" if got["n"] else "链路失败"))
    return 0 if got["n"] else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="IDAudioSync",
        description=f"{APP_NAME} —— 局域网跨机声音同步（独立版，自动配对）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  双击 exe            安装（首次）并常驻托盘，托盘里选角色
  IDAudioSync.exe --install / --uninstall / --dry-run
  IDAudioSync.exe receiver        直接跑接收方（排错）
  IDAudioSync.exe sender          直接跑发送方（排错）
  IDAudioSync.exe tone-test       播放测试音
  IDAudioSync.exe list-devices    列出音频设备
""",
    )
    p.add_argument("mode", nargs="?", default=None,
                   choices=["receiver", "sender", "tray", "tone-test", "pair-test", "list-devices"])
    p.add_argument("--install", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-install", action="store_true", help="跳过自动安装，直接跑托盘")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--samplerate", type=int, default=DEFAULT_SR)
    p.add_argument("--channels", type=int, default=DEFAULT_CH)
    p.add_argument("--block-ms", type=float, default=DEFAULT_BLOCK_MS)
    p.add_argument("--device", default=None, help="设备名称子串")
    p.add_argument("--peer", default=None, help="直接指定对端地址（跳过自动搜索，排错用）")
    p.add_argument("--no-gate", dest="gate", action="store_false", default=True,
                   help="关闭静音门限（始终发送完整音频）")
    p.add_argument("--gate-threshold", type=float, default=None, help="静音判定 RMS 阈值（排错可设 1.0 只发心跳）")
    p.add_argument("--token", default="", help="可选共享令牌")
    p.add_argument("--seconds", type=float, default=0.0, help="运行 N 秒后退出（测试）")
    p.add_argument("--log-file", default=None)
    return p


def cmd_dry_run() -> int:
    ifs = local_interfaces()
    log("detect", f"本机名：{socket.gethostname()}")
    for ip, mask, bcast in ifs:
        log("detect", f"网卡：{ip} / {mask} / 广播 {bcast}")
    log("detect", f"广播目标：{', '.join(broadcast_targets())}")
    try:
        dev = sc.default_speaker().name
    except Exception as exc:  # noqa: BLE001
        dev = f"读取失败({exc})"
    log("detect", f"输出设备：{dev}")
    log("detect", f"已安装：{is_installed()}（{installed_exe()}）")
    log("detect", f"从安装目录运行：{running_from_install_dir()}")
    log("detect", f"开机自启：{autostart_enabled()}")
    log("detect", f"配置：{CONFIG_PATH}")
    found = discover_receiver(timeout=2.0)
    log("detect", f"搜索到的接收方：{found or '无'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.uninstall:
        return do_uninstall()
    if args.install:
        return do_install(dry_run=args.dry_run)
    if args.dry_run:
        return cmd_dry_run()
    if args.log_file:
        set_log_file(args.log_file)

    if args.mode == "list-devices":
        list_devices()
        return 0
    if args.mode == "tone-test":
        return run_tone_test(args)
    if args.mode == "pair-test":
        return run_pair_test(args)
    if args.mode in ("receiver", "sender"):
        cfg = load_config()
        cfg.update({"role": args.mode, "port": args.port, "samplerate": args.samplerate,
                    "channels": args.channels, "block_ms": args.block_ms})
        if args.peer:
            cfg["peer"] = args.peer
        cfg["gate"] = bool(args.gate)
        if args.gate_threshold is not None:
            cfg["gate_threshold"] = float(args.gate_threshold)
        if args.token:
            cfg["token"] = args.token
        worker = Receiver(cfg) if args.mode == "receiver" else Sender(cfg)
        return worker.run(args.seconds)

    # 默认：自安装 + 托盘
    frozen = bool(getattr(sys, "frozen", False))
    if frozen and not args.no_install and not is_installed():
        if not is_admin():
            log("main", "首次运行需要管理员权限安装，正在提权…")
            if elevate("--install"):
                return 0
            message_box("首次运行需要管理员权限来完成安装。\n请右键该程序，选择「以管理员身份运行」。", 0x30)
            return 1
        rc = do_install()
        if not running_from_install_dir():
            return rc

    if not acquire_single_instance():
        log("main", "已有实例在运行，本次退出")
        return 0

    cfg = load_config()
    if cfg.pop("_migrated", False):
        save_config(cfg)
        log("cfg", f"已把旧版配置升级为独立版格式（角色 {cfg.get('role')}，对端 {cfg.get('peer') or '未配对'}）")
    if args.port:
        cfg["port"] = args.port
    if args.token:
        cfg["token"] = args.token
    return Supervisor(cfg).run(seconds=args.seconds)


if __name__ == "__main__":
    sys.exit(main())
