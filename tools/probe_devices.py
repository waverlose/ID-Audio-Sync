# -*- coding: utf-8 -*-
"""列出本机所有扬声器/麦克风，并测试 WASAPI loopback 抓取是否可用。"""
import sys
import soundcard as sc


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        return f"<错误: {exc}>"


def main() -> int:
    print("== 默认设备 ==")
    print(f"  默认扬声器: {safe(sc.default_speaker)}")
    print(f"  默认麦克风: {safe(sc.default_microphone)}")

    print("\n== 全部扬声器 (all_speakers) ==")
    try:
        speakers = sc.all_speakers()
    except Exception as exc:  # noqa: BLE001
        print(f"  枚举失败: {exc}")
        speakers = []
    for i, s in enumerate(speakers):
        print(f"  [{i}] {s.name}\n       id={s.id}")

    print("\n== 全部麦克风 (all_microphones, 不含 loopback) ==")
    try:
        mics = sc.all_microphones(include_loopback=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  枚举失败: {exc}")
        mics = []
    for i, m in enumerate(mics):
        print(f"  [{i}] {m.name}")

    print("\n== loopback 设备（抓系统播放声音用）==")
    try:
        loops = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]
    except Exception as exc:  # noqa: BLE001
        print(f"  枚举失败: {exc}")
        loops = []
    for i, m in enumerate(loops):
        print(f"  [{i}] {m.name}\n       id={m.id}")

    if not loops:
        print("\n!! 没有发现 loopback 设备")
        return 2

    print("\n== 抓取测试（0.3 秒，48kHz/立体声）==")
    import numpy as np

    target = loops[0]
    try:
        with target.recorder(samplerate=48000, channels=2, blocksize=4800) as rec:
            data = rec.record(numframes=4800)
        peak = float(np.max(np.abs(data)))
        rms = float(np.sqrt(np.mean(np.square(data.astype("float64")))))
        print(f"  设备: {target.name}")
        print(f"  形状: {data.shape}  峰值: {peak:.4f}  RMS: {rms:.6f}")
        print("  结论: loopback 抓取正常")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 抓取失败: {exc}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
