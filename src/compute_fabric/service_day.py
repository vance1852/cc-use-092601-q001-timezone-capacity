"""服务日容量计算：线路维护窗口、机房时区与租户服务日共用同一套边界。

服务日定义为机房（线路起点设施）当地时区的自然日 [00:00, 24:00)，
换算成 UTC 半开区间后与维护窗口求交。维护只按真正重叠的时长折算，
跨日维护因此只影响重叠的那一段；开放式维护（ends_at 为空）持续生效。
同一时间片内多段维护按 capacity_percent 连乘，结果与维护登记顺序无关，
重复计算得到稳定结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from .clock import load_timezone
from .planning import ZERO, decimal_text, quantize_volume


HUNDRED = Decimal("100")
SECONDS_PER_DAY = Decimal(86400)


def service_day_window_utc(service_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    """返回服务日在机房时区下的 UTC 半开区间 [start, end)。"""
    try:
        day = date.fromisoformat(service_date)
    except ValueError as exc:
        raise ValueError("service_date 必须是 YYYY-MM-DD 日期") from exc
    zone = load_timezone(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    return start, start + _day_length(day, zone)


def _day_length(day: date, zone) -> timedelta:
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return end.astimezone(timezone.utc) - start.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OutageWindow:
    """参与服务日计算的维护窗口（UTC 时刻，ends_at 为空表示开放式）。"""

    outage_id: int
    starts_at: datetime
    ends_at: datetime | None
    capacity_percent: Decimal


@dataclass(frozen=True, slots=True)
class OutageContribution:
    outage_id: int
    capacity_percent: Decimal
    overlap_seconds: Decimal
    overlap_hours: Decimal
    lost_gpu_hours: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "outage_id": self.outage_id,
            "capacity_percent": decimal_text(self.capacity_percent),
            "overlap_seconds": decimal_text(self.overlap_seconds),
            "overlap_hours": decimal_text(self.overlap_hours),
            "lost_gpu_hours": decimal_text(self.lost_gpu_hours),
        }


@dataclass(frozen=True, slots=True)
class CapacitySegment:
    starts_at: datetime
    ends_at: datetime
    hours: Decimal
    capacity_factor: Decimal
    capacity_gpu_hours: Decimal
    outage_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "starts_at": _utc_text(self.starts_at),
            "ends_at": _utc_text(self.ends_at),
            "hours": decimal_text(quantize_volume(self.hours)),
            "capacity_factor": decimal_text(self.capacity_factor),
            "capacity_gpu_hours": decimal_text(quantize_volume(self.capacity_gpu_hours)),
            "outage_ids": list(self.outage_ids),
        }


@dataclass(frozen=True, slots=True)
class ServiceDayCapacity:
    service_date: str
    timezone_name: str
    window_start_utc: datetime
    window_end_utc: datetime
    nominal_gpu_hours: Decimal
    effective_gpu_hours: Decimal
    lost_gpu_hours: Decimal
    outages: tuple[OutageContribution, ...]
    segments: tuple[CapacitySegment, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "service_date": self.service_date,
            "timezone": self.timezone_name,
            "window_start_utc": _utc_text(self.window_start_utc),
            "window_end_utc": _utc_text(self.window_end_utc),
            "nominal_gpu_hours": decimal_text(self.nominal_gpu_hours),
            "effective_gpu_hours": decimal_text(self.effective_gpu_hours),
            "lost_gpu_hours": decimal_text(self.lost_gpu_hours),
            "outages": [item.as_dict() for item in self.outages],
            "segments": [item.as_dict() for item in self.segments],
        }


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def service_day_capacity(
    nominal_gpu_hours: Decimal,
    service_date: str,
    timezone_name: str,
    outages: Sequence[OutageWindow],
) -> ServiceDayCapacity:
    """计算机房时区服务日的可分配算力，并给出每段维护的重叠贡献。"""
    if nominal_gpu_hours < ZERO:
        raise ValueError("名义容量不能为负数")
    window_start, window_end = service_day_window_utc(service_date, timezone_name)
    window_seconds = Decimal((window_end - window_start).total_seconds())
    window_hours = window_seconds / Decimal(3600)
    # 名义容量按服务日实际时长折算后的容量速率（GPU 小时/小时）
    hourly_rate = nominal_gpu_hours * (window_seconds / SECONDS_PER_DAY) / window_hours

    clipped: list[tuple[int, datetime, datetime, Decimal]] = []
    for outage in outages:
        percent = max(ZERO, min(HUNDRED, outage.capacity_percent))
        start = max(outage.starts_at, window_start)
        end = window_end if outage.ends_at is None else min(outage.ends_at, window_end)
        if start < end:
            clipped.append((outage.outage_id, start, end, percent))
    clipped.sort(key=lambda item: (item[1], item[2], item[0]))

    boundaries = {window_start, window_end}
    for _, start, end, _ in clipped:
        boundaries.add(start)
        boundaries.add(end)
    ordered = sorted(boundaries)

    segments: list[CapacitySegment] = []
    lost_by_outage: dict[int, Decimal] = {item[0]: ZERO for item in clipped}
    overlap_by_outage: dict[int, Decimal] = {item[0]: ZERO for item in clipped}
    percent_by_outage: dict[int, Decimal] = {item[0]: item[3] for item in clipped}
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        midpoint = left + (right - left) / 2
        active = [item for item in clipped if item[1] <= midpoint < item[2]]
        factor = Decimal(1)
        for _, _, _, percent in active:
            factor *= percent / HUNDRED
        seconds = Decimal((right - left).total_seconds())
        hours = seconds / Decimal(3600)
        segment_capacity = hourly_rate * hours * factor
        lost = hourly_rate * hours * (Decimal(1) - factor)
        if active:
            share = lost / Decimal(len(active))
            for outage_id, _, _, _ in active:
                lost_by_outage[outage_id] += share
                overlap_by_outage[outage_id] += seconds
        segments.append(
            CapacitySegment(
                starts_at=left,
                ends_at=right,
                hours=hours,
                capacity_factor=factor,
                capacity_gpu_hours=segment_capacity,
                outage_ids=tuple(outage_id for outage_id, _, _, _ in active),
            )
        )

    effective = quantize_volume(sum((item.capacity_gpu_hours for item in segments), ZERO))
    contributions = tuple(
        OutageContribution(
            outage_id=outage_id,
            capacity_percent=percent_by_outage[outage_id],
            overlap_seconds=overlap_by_outage[outage_id],
            overlap_hours=quantize_volume(overlap_by_outage[outage_id] / Decimal(3600)),
            lost_gpu_hours=quantize_volume(lost_by_outage[outage_id]),
        )
        for outage_id in sorted(lost_by_outage)
    )
    return ServiceDayCapacity(
        service_date=service_date,
        timezone_name=timezone_name,
        window_start_utc=window_start,
        window_end_utc=window_end,
        nominal_gpu_hours=quantize_volume(nominal_gpu_hours),
        effective_gpu_hours=effective,
        lost_gpu_hours=quantize_volume(nominal_gpu_hours - effective),
        outages=contributions,
        segments=tuple(segments),
    )
