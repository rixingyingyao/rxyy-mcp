# -*- coding: utf-8 -*-
"""生成 notify.wav 提示音（柔和双音上行叮咚，替代系统 SystemAsterisk）。

纯标准库，可随时调参重新生成：python gen_notify_wav.py
"""
import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
OUT_PATH = Path(__file__).resolve().parent / "notify.wav"

# (频率Hz, 起始秒, 时长秒, 音量) —— G5 → C6，正弦为主、少量泛音，软木琴质感
NOTES = [
    (783.99, 0.00, 0.45, 0.40),
    (1046.50, 0.14, 0.55, 0.34),
]
ATTACK = 0.006  # 起音 6ms，避免爆音


def note_sample(freq, t, dur, amp):
    if t < 0 or t >= dur:
        return 0.0
    env = t / ATTACK if t < ATTACK else math.exp(-5.2 * (t - ATTACK) / dur)
    tone = (
        math.sin(2 * math.pi * freq * t)
        + 0.30 * math.sin(2 * math.pi * freq * 2 * t)
        + 0.10 * math.sin(2 * math.pi * freq * 3 * t)
    )
    return amp * env * tone / 1.4


def main():
    total = max(start + dur for _, start, dur, _ in NOTES) + 0.05
    n = int(total * SAMPLE_RATE)
    samples = []
    for i in range(n):
        t = i / SAMPLE_RATE
        v = sum(note_sample(f, t - start, dur, amp) for f, start, dur, amp in NOTES)
        samples.append(max(-1.0, min(1.0, v)))

    peak = max(abs(s) for s in samples) or 1.0
    gain = 0.5 / peak  # 峰值约 -6dBFS，音量适中
    frames = b"".join(struct.pack("<h", int(s * gain * 32767)) for s in samples)

    with wave.open(str(OUT_PATH), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames)
    print(f"OK -> {OUT_PATH} ({total:.2f}s, {len(frames)} bytes)")


if __name__ == "__main__":
    main()
