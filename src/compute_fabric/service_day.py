"""服务日容量计算。

线路维护窗口（route_outages，绝对 UTC 时刻）、机房时区（facilities.timezone）
与租户服务日（nominations.service_date）必须共用同一套边界：

- 服务日窗口取线路起点机房当地日历日的午夜，再换算成 UTC 的半开区间；
- 降容只扣除与该窗口真正重叠的时段，按重叠时长加权，跨午夜检修自然拆分到两个服务日；
- 开放式降容（ends_at 为空）从生效时刻起持续覆盖后续每一个服务日；
- 同一时刻叠加多段降容时容量百分比相乘，时间段切分顺序固定，重复计算结果稳定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .clock import utc_text


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MICROSECONDS_PER_HOUR = Decimal("3600000000")
VOLUME_QUANTUM = Decimal("0.001")
HOURS_QUANTUM = Decimal("0.001")
RATIO_QUANTUM = Decimal("0.0001")

EFFECTIVE_OUTAGE_STATES = frozenset({"announced", "active"})


def _volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)


def _hours_text(value: Decimal) -> str:
    return format(value.quantize(HOURS_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _ratio_text(value: Decimal) -> str:
    return format(value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _factor_text(value: Decimal) -> str:
    return format(value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _elapsed_hours(start: datetime, end: datetime) -> Decimal:
    """精确的小时差，避免 float 二进制误差进入重复计算。"""
    delta = end - start
    microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Decimal(microseconds) / MICROSECONDS_PER_HOUR


def resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:  # ZoneInfoNotFoundError 是 KeyError 子类
        raise ValueError(f"{timezone_name} 不是有效的 IANA 时区") from exc


def service_day_window(service_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    """返回服务日在 UTC 下的半开区间 [start, end)。"""
    day = date.fromisoformat(service_date)
    zone = resolve_timezone(timezone_name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ServiceDayOutage:
    """一段降容登记；starts_at/ends_at 为带时区的绝对时刻，ends_at 为空表示开放式。"""

    outage_id: int
    starts_at: datetime
    ends_at: datetime | None
    capacity_percent: Decimal
    state: str

    @property
    def effective(self) -> bool:
        return self.state in EFFECTIVE_OUTAGE_STATES


@dataclass(frozen=True, slots=True)
class OutageOverlap:
    outage_id: int
    state: str
    capacity_percent: Decimal
    outage_starts_at: datetime
    outage_ends_at: datetime | None
    window_starts_at: datetime
    window_ends_at: datetime
    overlap_hours: Decimal
    overlap_ratio: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "outage_id": self.outage_id,
            "state": self.state,
            "capacity_percent": format(self.capacity_percent, "f"),
            "open_ended": self.outage_ends_at is None,
            "outage_starts_at": utc_text(self.outage_starts_at),
            "outage_ends_at": None if self.outage_ends_at is None else utc_text(self.outage_ends_at),
            "window_starts_at": utc_text(self.window_starts_at),
            "window_ends_at": utc_text(self.window_ends_at),
            "overlap_hours": _hours_text(self.overlap_hours),
            "overlap_ratio": _ratio_text(self.overlap_ratio),
        }


@dataclass(frozen=True, slots=True)
class CapacitySegment:
    """服务日内容量因子保持不变的一个时间片。"""

    starts_at: datetime
    ends_at: datetime
    duration_hours: Decimal
    capacity_factor: Decimal
    effective_gpu_hours: Decimal
    outage_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "starts_at": utc_text(self.starts_at),
            "ends_at": utc_text(self.ends_at),
            "duration_hours": _hours_text(self.duration_hours),
            "capacity_factor": _factor_text(self.capacity_factor),
            "effective_gpu_hours": format(_volume(self.effective_gpu_hours), "f"),
            "outage_ids": list(self.outage_ids),
        }


@dataclass(frozen=True, slots=True)
class ServiceDayCapacity:
    service_date: str
    timezone: str
    window_starts_at: datetime
    window_ends_at: datetime
    service_day_hours: Decimal
    raw_capacity: Decimal
    available_capacity: Decimal
    segments: Sequence[CapacitySegment]
    overlaps: Sequence[OutageOverlap]

    @property
    def derated_gpu_hours(self) -> Decimal:
        return _volume(self.raw_capacity - self.available_capacity)

    def as_dict(self) -> dict[str, object]:
        return {
            "service_date": self.service_date,
            "timezone": self.timezone,
            "window_starts_at": utc_text(self.window_starts_at),
            "window_ends_at": utc_text(self.window_ends_at),
            "service_day_hours": _hours_text(self.service_day_hours),
            "raw_capacity": format(_volume(self.raw_capacity), "f"),
            "derated_gpu_hours": format(self.derated_gpu_hours, "f"),
            "available_capacity": format(self.available_capacity, "f"),
            "segments": [segment.as_dict() for segment in self.segments],
            "outages": [overlap.as_dict() for overlap in self.overlaps],
        }


def explain_service_day(
    raw_capacity: Decimal,
    service_date: str,
    timezone_name: str,
    outages: Iterable[ServiceDayOutage],
) -> ServiceDayCapacity:
    """计算并解释某个服务日的原始容量、每段降容的重叠贡献与最终可分配量。"""
    window_start, window_end = service_day_window(service_date, timezone_name)
    service_day_hours = _elapsed_hours(window_start, window_end)

    # 先把每段降容裁剪到服务日窗口，零重叠直接丢弃；跨日维护只留下真正重叠的部分。
    clipped: list[tuple[ServiceDayOutage, datetime, datetime]] = []
    boundaries: set[datetime] = {window_start, window_end}
    for outage in sorted(outages, key=lambda item: item.outage_id):
        if not outage.effective:
            continue
        overlap_start = max(outage.starts_at, window_start)
        overlap_end = window_end if outage.ends_at is None else min(outage.ends_at, window_end)
        if overlap_end <= overlap_start:
            continue
        clipped.append((outage, overlap_start, overlap_end))
        boundaries.add(overlap_start)
        boundaries.add(overlap_end)

    overlaps = [
        OutageOverlap(
            outage_id=outage.outage_id,
            state=outage.state,
            capacity_percent=outage.capacity_percent,
            outage_starts_at=outage.starts_at,
            outage_ends_at=outage.ends_at,
            window_starts_at=overlap_start,
            window_ends_at=overlap_end,
            overlap_hours=_elapsed_hours(overlap_start, overlap_end),
            overlap_ratio=_elapsed_hours(overlap_start, overlap_end) / service_day_hours,
        )
        for outage, overlap_start, overlap_end in clipped
    ]

    # 时间轴切分：每个时间片内生效的降容集合不变，容量因子为各百分比的乘积。
    timeline = sorted(boundaries)
    segments: list[CapacitySegment] = []
    weighted_hours = ZERO
    for left, right in zip(timeline, timeline[1:]):
        duration_hours = _elapsed_hours(left, right)
        if duration_hours == ZERO:
            continue
        covering = sorted(
            (
                (outage, start, end)
                for outage, start, end in clipped
                if start <= left and right <= end
            ),
            key=lambda item: item[0].outage_id,
        )
        factor = Decimal(1)
        for outage, _, _ in covering:
            factor *= outage.capacity_percent / HUNDRED
        weighted_hours += factor * duration_hours
        segments.append(
            CapacitySegment(
                starts_at=left,
                ends_at=right,
                duration_hours=duration_hours,
                capacity_factor=factor,
                effective_gpu_hours=raw_capacity * factor * duration_hours / service_day_hours,
                outage_ids=tuple(outage.outage_id for outage, _, _ in covering),
            )
        )

    day_factor = weighted_hours / service_day_hours
    available = _volume(raw_capacity * day_factor)
    return ServiceDayCapacity(
        service_date=service_date,
        timezone=timezone_name,
        window_starts_at=window_start,
        window_ends_at=window_end,
        service_day_hours=service_day_hours,
        raw_capacity=raw_capacity,
        available_capacity=available,
        segments=segments,
        overlaps=overlaps,
    )
