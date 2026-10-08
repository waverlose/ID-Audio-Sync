# -*- coding: utf-8 -*-
"""在系统默认输出设备上播放一段合成音乐（用于测试 ID Audio Sync 的推流效果）。

旋律：贝多芬《欢乐颂》（公版），加简单低音与 ADSR 包络，循环播放。
用法: python play_music.py [--minutes 10] [--volume 0.28]
"""
from __future__ import annotations

import argparse
import math
import sys
import time

import numpy as np
import soundcard as sc

SR = 48000

# 音名 -> 频率
NOTES = {
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.00, "A4": 440.00,
    "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.25, "F5": 698.46, "G5": 783.99,
    "G3": 196.00, "A3": 220.00, "F3": 174.61, "C3": 130.81, "E3": 164.81, "D3": 146.83,
    "REST": 0.0,
}

# (音名, 拍数)  4/4 拍
MELODY = [
    ("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1),
    ("G4", 1), ("F4", 1), ("E4", 1), ("D4", 1),
    ("C4", 1), ("C4", 1), ("D4", 1), ("E4", 1),
    ("E4", 1.5), ("D4", 0.5), ("D4", 2),
    ("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1),
    ("G4", 1), ("F4", 1), ("E4", 1), ("D4", 1),
    ("C4", 1), ("C4", 1), ("D4", 1), ("E4", 1),
    ("D4", 1.5), ("C4", 0.5), ("C4", 2),
    ("D4", 1), ("D4", 1), ("E4", 1), ("C4", 1),
    ("D4", 1), ("E4", 0.5), ("F4", 0.5), ("E4", 1), ("C4", 1),
    ("D4", 1), ("E4", 0.5), ("F4", 0.5), ("E4", 1), ("D4", 1),
    ("C4", 1), ("D4", 1), ("G3", 2),
    ("E4", 1), ("E4", 1), ("F4", 1), ("G4", 1),
    ("G4", 1), ("F4", 1), ("E4", 1), ("D4", 1),
    ("C4", 1), ("C4", 1), ("D4", 1), ("E4", 1),
    ("D4", 1.5), ("C4", 0.5), ("C4", 2),
]

# 低音走向（每两小节一个根音）
BASS = [("C3", 4), ("G3", 4), ("C3", 4), ("G3", 4), ("C3", 4), ("F3", 4), ("C3", 4), ("G3", 4),
        ("C3", 4), ("G3", 4), ("C3", 4), ("G3", 4), ("C3", 4), ("F3", 4), ("C3", 4), ("C3", 4)]


def tone(freq: float, dur: float, vol: float = 1.0, harmonics=(1.0, 0.28, 0.12)) -> np.ndarray:
    n = max(1, int(SR * dur))
    t = np.arange(n) / SR
    wave = np.zeros(n)
    for i, amp in enumerate(harmonics, start=1):
        wave += amp * np.sin(2 * math.pi * freq * i * t)
    wave /= sum(harmonics)
    # ADSR 包络：快速起音 + 自然衰减，避免爆音
    atk = max(1, int(0.012 * SR))
    rel = max(1, int(0.10 * SR))
    env = np.ones(n)
    env[:atk] = np.linspace(0.0, 1.0, atk)
    env[-rel:] *= np.linspace(1.0, 0.0, min(rel, n))
    # 轻微渐弱，像钢琴
    env *= np.linspace(1.0, 0.72, n)
    return wave * env * vol


def render_sequence(seq, beat: float, vol: float) -> np.ndarray:
    parts = []
    for name, beats in seq:
        freq = NOTES.get(name, 0.0)
        dur = beats * beat
        parts.append(tone(freq, dur, vol) if freq > 0 else np.zeros(int(SR * dur)))
    return np.concatenate(parts) if parts else np.zeros(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0, help="播放时长（分钟）")
    ap.add_argument("--volume", type=float, default=0.28)
    ap.add_argument("--bpm", type=float, default=112.0)
    args = ap.parse_args()

    beat = 60.0 / args.bpm
    melody = render_sequence(MELODY, beat, args.volume)
    bass = render_sequence(BASS, beat, args.volume * 0.55)

    n = max(len(melody), len(bass))
    mix = np.zeros(n)
    mix[:len(melody)] += melody
    mix[:len(bass)] += bass

    # 整段淡入淡出
    fade = int(0.15 * SR)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix[-fade:] *= np.linspace(1, 0, fade)
    mix = np.clip(mix, -0.98, 0.98).astype("float32")

    loop_sec = len(mix) / SR
    speaker = sc.default_speaker()
    print(f"输出设备: {speaker.name}", flush=True)
    print(f"单遍时长 {loop_sec:.1f} 秒，播放 {args.minutes} 分钟（Ctrl+C 停止）", flush=True)

    block = int(SR * 0.05)
    deadline = time.time() + args.minutes * 60
    with speaker.player(samplerate=SR, channels=1, blocksize=block) as player:
        while time.time() < deadline:
            for i in range(0, len(mix), block):
                if time.time() >= deadline:
                    break
                chunk = mix[i:i + block]
                if chunk.size < block:
                    chunk = np.pad(chunk, (0, block - chunk.size))
                player.play(chunk.reshape(-1, 1))
    print("播放结束", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
