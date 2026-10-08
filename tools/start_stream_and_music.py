# -*- coding: utf-8 -*-
"""一键演示：后台启动"发送方推流" + "本机播放合成音乐"，用来实测对端能不能听到。

用法:
    python tools/start_stream_and_music.py --peer 10.96.79.239 --minutes 10

会在仓库根目录的 logs/ 下写两个日志文件：
    logs/stream-sender.log   推流侧
    logs/music.out           音乐播放侧
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src", "idaudiosync.py")
PY = sys.executable
DETACH = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def spawn(cmd: list[str], log_path: str) -> int:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    fh = open(log_path, "ab")
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                         creationflags=DETACH | NOWINDOW, close_fds=True)
    print(f"已启动 PID={p.pid} -> {os.path.relpath(log_path, ROOT)}")
    return p.pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer", default="", help="接收方地址；留空则走 UDP 自动配对")
    ap.add_argument("--port", type=int, default=31235)
    ap.add_argument("--minutes", type=float, default=10.0, help="演示时长（分钟）")
    ap.add_argument("--volume", type=float, default=0.35, help="本机音乐音量 0~1")
    args = ap.parse_args()

    logs = os.path.join(ROOT, "logs")
    seconds = max(1.0, args.minutes * 60.0)

    sender_cmd = [PY, SRC, "sender", "--port", str(args.port),
                  "--seconds", str(seconds),
                  "--log-file", os.path.join(logs, "stream-sender.log")]
    if args.peer:
        sender_cmd += ["--peer", args.peer]
    spawn(sender_cmd, os.path.join(logs, "stream-sender.out"))

    spawn([PY, os.path.join(HERE, "play_music.py"),
           "--minutes", str(args.minutes), "--volume", str(args.volume)],
          os.path.join(logs, "music.out"))

    target = args.peer or "自动配对"
    print(f"推流目标 {target}:{args.port}，本机音乐播放 {args.minutes} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
