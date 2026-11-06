"""动图调速。

GIF 帧延时精度是 10ms，浏览器和 QQ 会把 ≤10ms 的延时当作 100ms 播放，这里按实际播放效果计算。
帧率上限（默认 50 FPS，即每帧至少 20ms）有两种处理方式：
- 默认：倍率自动回退到「最短的那一帧刚好不超限」的最高倍率（原插件按平均帧率计算，
  对帧延时不均匀的动图会算错，这里改为按最短帧计算）。
- 允许丢帧：保持目标倍率，把连续的短帧合并成一帧（保留每组第一帧），总时长保持准确。
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from PIL import Image

from .frames import MAX_GIF_DURATION, Animation

SPEED_MIN, SPEED_MAX = 0.1, 10.0


@dataclass(slots=True)
class SpeedPlan:
    indices: list[int]  # 保留的源帧下标
    durations: list[int]
    actual: float
    note: str | None = None


def effective(duration: int) -> int:
    return 100 if duration <= 10 else int(duration)


def min_frame_ms(max_fps: int) -> int:
    """帧率上限对应的最短帧延时，向上取整到 10ms（GIF 精度）。"""
    raw = math.ceil(1000 / max(1, max_fps))
    return max(10, int(math.ceil(raw / 10) * 10))


def _round10(value: float) -> int:
    return int(round(value / 10.0)) * 10


def plan_speed(durations: list[int], factor: float, max_fps: int = 50, allow_drop: bool = False) -> SpeedPlan:
    if not durations:
        raise ValueError("没有帧")
    base = [effective(d) for d in durations]
    floor_ms = min_frame_ms(max_fps)
    indices = list(range(len(base)))
    fps_cap = 1000 // floor_ms

    def scaled(f: float) -> list[int]:
        return [min(MAX_GIF_DURATION, max(floor_ms, _round10(d / f))) for d in base]

    if min(base) / factor >= floor_ms - 1e-9:
        return SpeedPlan(indices, scaled(factor), factor)

    if not allow_drop:
        best = math.floor(min(base) / floor_ms * 10) / 10
        best = max(SPEED_MIN, min(best, factor))
        if best <= 1.0 and factor > 1.0:
            return SpeedPlan(
                indices, scaled(1.0), 1.0,
                f"原动图最短帧已是 {min(base)}ms（{1000 / min(base):.0f} FPS），再加速会超过 {fps_cap} FPS 上限，"
                "已保持原速。可在配置中开启「GIF 调速允许丢帧」。",
            )
        return SpeedPlan(
            indices, scaled(best), best,
            f"×{factor:g} 会让最短帧超过 {fps_cap} FPS 上限，已自动改为 ×{best:g}。"
            "可在配置中开启「GIF 调速允许丢帧」以达到目标倍率。",
        )

    # 丢帧：按目标时间轴累积，每组时长至少 floor_ms，组内只保留第一帧
    keep: list[int] = []
    out: list[int] = []
    cumulative = 0.0
    group_start = 0
    current = None
    for index, duration in enumerate(base):
        if current is None:
            current = index
        cumulative += duration / factor
        end = _round10(cumulative)
        if end - group_start >= floor_ms:
            keep.append(current)
            out.append(end - group_start)
            group_start = end
            current = None
    if current is not None:
        if out:
            out[-1] += max(0, _round10(cumulative) - group_start)
        else:
            keep.append(current)
            out.append(max(floor_ms, _round10(cumulative)))
    total_before = sum(base)
    actual = total_before / max(1, sum(out))
    note = f"已均匀丢帧：{len(base)} 帧 → {len(keep)} 帧，实际倍率约 ×{actual:.2f}。"
    return SpeedPlan(keep, out, round(actual, 2), note)


def apply_speed(anim: Animation, factor: float, max_fps: int, allow_drop: bool) -> tuple[Animation, SpeedPlan]:
    plan = plan_speed(anim.durations, factor, max_fps, allow_drop)
    frames: list[Image.Image] = [anim.frames[i] for i in plan.indices]
    return Animation(frames, plan.durations, anim.loop, anim.format, anim.source_frames, dict(anim.info)), plan
