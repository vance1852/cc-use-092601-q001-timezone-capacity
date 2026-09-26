"""服务日容量边界测试：机房时区、跨日维护、开放式降容、稳定性与历史不可变。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from compute_fabric.api import JsonApplication
from compute_fabric.clock import FrozenClock
from compute_fabric.errors import NotFound
from compute_fabric.service import SupplyService
from compute_fabric.service_day import (
    ServiceDayOutage,
    explain_service_day,
    service_day_window,
)


def outage(
    outage_id: int,
    starts_at: str,
    ends_at: str | None,
    capacity_percent: str = "50",
    state: str = "announced",
) -> ServiceDayOutage:
    from compute_fabric.clock import parse_utc

    return ServiceDayOutage(
        outage_id=outage_id,
        starts_at=parse_utc(starts_at, "starts_at"),
        ends_at=None if ends_at is None else parse_utc(ends_at, "ends_at"),
        capacity_percent=Decimal(capacity_percent),
        state=state,
    )


class ServiceDayWindowTests(unittest.TestCase):
    def test_window_uses_facility_local_midnight(self) -> None:
        start, end = service_day_window("2026-09-25", "Asia/Urumqi")
        # 乌鲁木齐当地午夜对应 UTC 前一日 18:00，长度为完整 24 小时
        self.assertEqual(start, datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc))

    def test_shanghai_window_spans_two_utc_calendar_days(self) -> None:
        start, end = service_day_window("2026-09-25", "Asia/Shanghai")
        self.assertEqual(start, datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc))


class ExplainServiceDayTests(unittest.TestCase):
    def test_midnight_outage_counted_in_local_service_day_not_utc_day(self) -> None:
        # 乌鲁木齐当地 9/25 午夜 00:00-08:00（UTC 9/24 18:00 - 9/25 02:00），
        # 降容 50%，跨 UTC 自然日但完全落在乌鲁木齐服务日 9/25 内。
        explanation = explain_service_day(
            Decimal("240"),
            "2026-09-25",
            "Asia/Urumqi",
            [outage(1, "2026-09-24T18:00:00Z", "2026-09-25T02:00:00Z")],
        )
        self.assertEqual(explanation.available_capacity, Decimal("200.000"))
        self.assertEqual(explanation.derated_gpu_hours, Decimal("40.000"))
        overlap = explanation.overlaps[0]
        self.assertEqual(overlap.overlap_hours, Decimal("8"))
        self.assertEqual(overlap.overlap_ratio, Decimal(1) / Decimal(3))
        self.assertEqual(overlap.as_dict()["overlap_ratio"], "0.3333")
        factors = [(s.duration_hours, s.capacity_factor) for s in explanation.segments]
        self.assertEqual(factors, [(Decimal("8"), Decimal("0.5")), (Decimal("16"), Decimal("1"))])

    def test_same_outage_does_not_leak_into_previous_utc_calendar_day(self) -> None:
        # 旧实现按 UTC 9/24 取窗口会错误命中该降容；新实现按乌鲁木齐服务日取窗口则不重叠。
        explanation = explain_service_day(
            Decimal("240"),
            "2026-09-24",
            "Asia/Urumqi",
            [outage(1, "2026-09-24T18:00:00Z", "2026-09-25T02:00:00Z")],
        )
        self.assertEqual(explanation.available_capacity, Decimal("240.000"))
        self.assertEqual(explanation.overlaps, [])

    def test_cross_day_maintenance_is_split_by_overlap_only(self) -> None:
        # 上海服务日窗口为 UTC 16:00 到次日 16:00；
        # 检修 UTC 12:00-20:00 与 9/25、9/26 各重叠 4 小时。
        record = [outage(1, "2026-09-25T12:00:00Z", "2026-09-25T20:00:00Z")]
        for day in ("2026-09-25", "2026-09-26"):
            explanation = explain_service_day(Decimal("240"), day, "Asia/Shanghai", record)
            self.assertEqual(explanation.available_capacity, Decimal("220.000"))
            self.assertEqual(explanation.overlaps[0].overlap_hours, Decimal("4"))
            self.assertEqual(explanation.overlaps[0].overlap_ratio, Decimal(1) / Decimal(6))
            self.assertEqual(explanation.overlaps[0].as_dict()["overlap_ratio"], "0.1667")

    def test_open_ended_outage_keeps_derating_every_service_day(self) -> None:
        record = [outage(1, "2026-09-24T16:00:00Z", None)]
        first = explain_service_day(Decimal("240"), "2026-09-25", "Asia/Shanghai", record)
        second = explain_service_day(Decimal("240"), "2026-09-26", "Asia/Shanghai", record)
        self.assertEqual(first.available_capacity, Decimal("120.000"))
        self.assertTrue(first.overlaps[0].as_dict()["open_ended"])
        self.assertEqual(second.available_capacity, Decimal("120.000"))
        # 开放式降容若在服务日开始后才登记，只扣重叠部分（窗口内剩余 8 小时）
        partial = explain_service_day(
            Decimal("240"),
            "2026-09-25",
            "Asia/Shanghai",
            [outage(2, "2026-09-25T08:00:00Z", None)],
        )
        self.assertEqual(partial.available_capacity, Decimal("200.000"))
        self.assertEqual(partial.overlaps[0].overlap_hours, Decimal("8"))

    def test_simultaneous_outages_multiply_within_overlapping_segments(self) -> None:
        record = [
            outage(1, "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", "50"),
            outage(2, "2026-09-24T16:00:00Z", "2026-09-25T04:00:00Z", "50"),
        ]
        explanation = explain_service_day(Decimal("240"), "2026-09-25", "Asia/Shanghai", record)
        # 前 12 小时两段叠加：25%；后 12 小时单段：50% → 全天因子 0.375
        self.assertEqual(explanation.available_capacity, Decimal("90.000"))
        self.assertEqual(
            [set(ids) for ids in (s.outage_ids for s in explanation.segments)],
            [{1, 2}, {1}],
        )

    def test_half_open_boundaries_touch_but_do_not_overlap(self) -> None:
        base = [outage(1, "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z")]
        # 在窗口开始时刻结束：不重叠
        ending_at_start = [outage(2, "2026-09-24T12:00:00Z", "2026-09-24T16:00:00Z")]
        # 在窗口结束时刻开始：不重叠
        starting_at_end = [outage(3, "2026-09-25T16:00:00Z", "2026-09-25T20:00:00Z")]
        for record in (ending_at_start, starting_at_end):
            explanation = explain_service_day(
                Decimal("240"), "2026-09-25", "Asia/Shanghai", record
            )
            self.assertEqual(explanation.available_capacity, Decimal("240.000"))
            self.assertEqual(explanation.overlaps, [])
        full = explain_service_day(Decimal("240"), "2026-09-25", "Asia/Shanghai", base)
        self.assertEqual(full.available_capacity, Decimal("120.000"))

    def test_cancelled_or_closed_outages_are_ignored(self) -> None:
        explanation = explain_service_day(
            Decimal("240"),
            "2026-09-25",
            "Asia/Shanghai",
            [outage(1, "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", state="cancelled")],
        )
        self.assertEqual(explanation.available_capacity, Decimal("240.000"))

    def test_repeated_calculation_is_stable(self) -> None:
        record = [
            outage(2, "2026-09-25T04:00:00Z", "2026-09-25T10:00:00Z", "25"),
            outage(1, "2026-09-24T18:00:00Z", "2026-09-25T06:00:00Z", "50"),
        ]
        first = explain_service_day(Decimal("360"), "2026-09-25", "Asia/Shanghai", record)
        second = explain_service_day(Decimal("360"), "2026-09-25", "Asia/Shanghai", list(reversed(record)))
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(explain_service_day(Decimal("360"), "2026-09-25", "Asia/Shanghai", record).as_dict(), first.as_dict())
        # 降容总量不超过原始容量
        self.assertLessEqual(first.available_capacity, Decimal("360"))


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(
            self.connection,
            FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)),
        )
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "dc-urumqi", "name": "乌鲁木齐数据中心", "kind": "edge-site", "timezone": "Asia/Urumqi", "capacity_gpu_hours": "300000"})
        self.service.create_facility("plan", {"facility_id": "dc-shanghai", "name": "上海数据中心", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_gpu_hours": "500000"})
        self.service.create_route("plan", {"route_id": "west-east", "origin_id": "dc-urumqi", "destination_id": "dc-shanghai", "product": "gpu-h100", "daily_capacity": "240", "loss_basis_points": 0, "transit_hours": 12})

    def tearDown(self) -> None:
        self.connection.close()

    def nominate(self, number: int) -> None:
        self.service.submit_nomination("dispatch", {
            "nomination_id": f"nom-{number}",
            "route_id": "west-east",
            "shipper_id": f"tenant-{number}",
            "service_date": "2026-09-25",
            "requested_gpu_hours": "200",
            "priority": 10 * number,
            "idempotency_key": f"nom-key-{number}",
        })

    def test_explain_endpoint_and_allocation_share_local_day_boundary(self) -> None:
        self.service.announce_outage("risk", "west-east", "2026-09-24T18:00:00Z", "2026-09-25T02:00:00Z", "50", "乌鲁木齐午夜检修")
        explanation = self.service.explain_capacity("dispatch", "west-east", "2026-09-25")
        self.assertEqual(explanation["timezone"], "Asia/Urumqi")
        self.assertEqual(explanation["window_starts_at"], "2026-09-24T18:00:00Z")
        self.assertEqual(explanation["raw_capacity"], "240.000")
        self.assertEqual(explanation["available_capacity"], "200.000")
        self.assertEqual(explanation["outages"][0]["overlap_hours"], "8.000")
        self.nominate(1)
        allocation = self.service.allocate("dispatch", "west-east", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "200.000")
        self.assertEqual(allocation["capacity"]["window_starts_at"], "2026-09-24T18:00:00Z")
        self.assertEqual(allocation["allocations"][0]["allocated_gpu_hours"], "200.000")

    def test_open_ended_outage_persists_across_service_days(self) -> None:
        self.service.announce_outage("risk", "west-east", "2026-09-24T18:00:00Z", None, "50", "开放降容")
        for day in ("2026-09-25", "2026-09-26", "2026-10-01"):
            explanation = self.service.explain_capacity("audit", "west-east", day)
            self.assertEqual(explanation["available_capacity"], "120.000", day)

    def test_historical_allocation_is_not_silently_recomputed(self) -> None:
        self.nominate(1)
        first = self.service.allocate("dispatch", "west-east", "2026-09-25")
        self.assertEqual(first["available_capacity"], "240.000")
        stored = self.connection.execute(
            "SELECT available_capacity,result_json,explanation_json FROM allocation_runs WHERE allocation_id=?",
            (first["allocation_id"],),
        ).fetchone()
        self.assertEqual(stored["available_capacity"], "240.000")
        # 事后补登记同一天的午夜降容：解释口径立即变化
        self.service.announce_outage("risk", "west-east", "2026-09-24T18:00:00Z", "2026-09-25T02:00:00Z", "50", "补登记")
        explanation = self.service.explain_capacity("dispatch", "west-east", "2026-09-25")
        self.assertEqual(explanation["available_capacity"], "200.000")
        # 但历史分配结果与已落库提名不得被静默改写
        stored_after = self.connection.execute(
            "SELECT available_capacity,result_json,explanation_json FROM allocation_runs WHERE allocation_id=?",
            (first["allocation_id"],),
        ).fetchone()
        self.assertEqual(stored_after["available_capacity"], stored["available_capacity"])
        self.assertEqual(stored_after["result_json"], stored["result_json"])
        self.assertEqual(stored_after["explanation_json"], stored["explanation_json"])
        nomination = self.connection.execute(
            "SELECT state,allocated_gpu_hours,revision FROM nominations WHERE nomination_id='nom-1'"
        ).fetchone()
        self.assertEqual(nomination["state"], "allocated")
        self.assertEqual(nomination["allocated_gpu_hours"], "200.000")
        self.assertEqual(nomination["revision"], 2)

    def test_api_serves_capacity_explanation(self) -> None:
        self.service.announce_outage("risk", "west-east", "2026-09-24T18:00:00Z", "2026-09-25T02:00:00Z", "50", "检修")
        app = JsonApplication(self.service)
        response = app.handle(
            "GET",
            "/routes/west-east/capacity?service_date=2026-09-25",
            {"X-Actor-Id": "audit"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["available_capacity"], "200.000")
        self.assertEqual(len(response.body["segments"]), 2)
        missing = app.handle("GET", "/routes/west-east/capacity?service_date=2026-09-25")
        self.assertEqual(missing.status, 422)
        with self.assertRaises(NotFound):
            self.service.explain_capacity("audit", "missing-route", "2026-09-25")


if __name__ == "__main__":
    unittest.main()
