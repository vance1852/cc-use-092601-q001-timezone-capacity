from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from compute_fabric import acceptance
from compute_fabric.api import JsonApplication
from compute_fabric.clock import FrozenClock
from compute_fabric.errors import Conflict, Forbidden, ValidationFailed
from compute_fabric.planning import AllocationRequest, PricePoint, allocate_capacity, latest_streak
from compute_fabric.service import SupplyService
from compute_fabric.risk import DemandBucket, inventory_coverage, mark_to_market, supply_gap


ROOT = Path(__file__).resolve().parents[1]


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("102")),
            PricePoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["nomination_id"], "first")
        self.assertEqual(rows[0]["allocated_gpu_hours"], "70.000")
        self.assertEqual(rows[1]["allocated_gpu_hours"], "30.000")

    def test_inventory_coverage_and_supply_gap(self) -> None:
        coverage = inventory_coverage(
            [{"facility_id": "inference-pool", "product": "gpu-a100", "available_gpu_hours": "250"}],
            [DemandBucket("inference-pool", "gpu-a100", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = supply_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["supply_gap"], "30.000")

    def test_mark_to_market_groups_deterministically(self) -> None:
        result = mark_to_market(
            [{"position_id": "p1", "market_index": "PEAK_VALLEY", "quantity_gpu_hours": "100", "entry_price_cny": "105"}],
            {"PEAK_VALLEY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class SupplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部数据中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_gpu_hours": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_gpu_hours": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "gpu-h100", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{day}", "close_cny": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_quote_revisions_preserve_history(self) -> None:
        first = self.quote(23, "98")
        second = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-23", "close_cny": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        rows = self.connection.execute("SELECT * FROM market_index_quotes ORDER BY quote_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_quote_id"], rows[0]["quote_id"])

    def test_nomination_replay_and_payload_conflict(self) -> None:
        payload = {"nomination_id": "nom-1", "route_id": "fabric-a-b", "shipper_id": "tenant", "service_date": "2026-09-25", "requested_gpu_hours": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_nomination("dispatch", payload)
        self.assertEqual(first, self.service.submit_nomination("dispatch", payload))
        changed = dict(payload, requested_gpu_hours="81000")
        with self.assertRaises(Conflict):
            self.service.submit_nomination("dispatch", changed)

    def test_outage_reduces_allocation_and_transfer_consumes_inventory(self) -> None:
        # 覆盖上海时区服务日 2026-09-25 全天（当地 00:00 到 24:00）的 50% 降容
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_gpu_hours": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_gpu_hours"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "gpu-h100", "grade": "PEAK_VALLEY", "quantity_gpu_hours": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["loaded_gpu_hours"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_gpu_hours"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.quote(23, "98")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "gpu-h100", "grade": "PEAK_VALLEY", "quantity_gpu_hours": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "机组检修恢复", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:gpu-h100": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE supply_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_service_day_follows_origin_timezone_and_splits_cross_midnight_outage(self) -> None:
        self.service.create_facility("plan", {"facility_id": "cluster-wlmq", "name": "乌鲁木齐数据中心", "kind": "storage", "timezone": "Asia/Urumqi", "capacity_gpu_hours": "600000"})
        self.service.create_route("plan", {"route_id": "fabric-wlmq-sh", "origin_id": "cluster-wlmq", "destination_id": "pool-b", "product": "gpu-h100", "daily_capacity": "24000", "loss_basis_points": 0, "transit_hours": 48})
        # 乌鲁木齐当地 2026-09-24 23:30 至 2026-09-25 00:30（UTC 17:30-18:30）的 50% 降容
        self.service.announce_outage("risk", "fabric-wlmq-sh", "2026-09-24T17:30:00Z", "2026-09-24T18:30:00Z", "50", "线路维护")
        day_before = self.service.service_day_report("fabric-wlmq-sh", "2026-09-24")
        day_of = self.service.service_day_report("fabric-wlmq-sh", "2026-09-25")
        day_after = self.service.service_day_report("fabric-wlmq-sh", "2026-09-26")
        # 服务日边界与机房时区一致：当地午夜对应 18:00Z，而不是 00:00Z
        self.assertEqual(day_of["timezone"], "Asia/Urumqi")
        self.assertEqual(day_of["window_start_utc"], "2026-09-24T18:00:00Z")
        self.assertEqual(day_of["window_end_utc"], "2026-09-25T18:00:00Z")
        self.assertEqual(day_of["nominal_gpu_hours"], "24000.000")
        # 跨午夜的 1 小时维护只在相邻两个服务日各计真正重叠的 30 分钟
        self.assertEqual(day_before["effective_gpu_hours"], "23750.000")
        self.assertEqual(day_of["effective_gpu_hours"], "23750.000")
        self.assertEqual(day_after["effective_gpu_hours"], "24000.000")
        self.assertEqual(day_of["lost_gpu_hours"], "250.000")
        self.assertEqual(len(day_of["outages"]), 1)
        self.assertEqual(day_of["outages"][0]["overlap_hours"], "0.500")
        self.assertEqual(day_of["outages"][0]["lost_gpu_hours"], "250.000")

    def test_open_ended_outage_remains_active(self) -> None:
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-27T00:00:00Z", None, "75", "开放式检修")
        before = self.service.service_day_report("fabric-a-b", "2026-09-26")
        after = self.service.service_day_report("fabric-a-b", "2026-09-28")
        later = self.service.service_day_report("fabric-a-b", "2026-10-05")
        self.assertEqual(before["effective_gpu_hours"], "100000.000")
        self.assertEqual(after["effective_gpu_hours"], "75000.000")
        self.assertEqual(later["effective_gpu_hours"], "75000.000")

    def test_overlapping_outages_compound_and_report_is_stable(self) -> None:
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-24T20:00:00Z", "2026-09-24T22:00:00Z", "50", "检修一")
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-24T21:00:00Z", "2026-09-24T23:00:00Z", "50", "检修二")
        first = self.service.service_day_report("fabric-a-b", "2026-09-25")
        second = self.service.service_day_report("fabric-a-b", "2026-09-25")
        self.assertEqual(first, second)
        # 20:00-21:00 与 22:00-23:00 各按 50% 计，21:00-22:00 两段连乘为 25%
        self.assertEqual(first["effective_gpu_hours"], "92708.333")
        self.assertEqual([item["overlap_hours"] for item in first["outages"]], ["2.000", "2.000"])
        self.assertEqual([item["lost_gpu_hours"] for item in first["outages"]], ["3645.833", "3645.833"])

    def test_allocate_replay_returns_stored_result_without_rewriting_history(self) -> None:
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_gpu_hours": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        first = self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.assertFalse(first["replayed"])
        nominations_before = [dict(row) for row in self.connection.execute("SELECT * FROM nominations ORDER BY nomination_id").fetchall()]
        audit_events_before = self.service.audit_chain("audit")["events"]
        second = self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["allocation_id"], first["allocation_id"])
        self.assertEqual(second["allocations"], first["allocations"])
        self.assertEqual(second["available_capacity"], first["available_capacity"])
        nominations_after = [dict(row) for row in self.connection.execute("SELECT * FROM nominations ORDER BY nomination_id").fetchall()]
        self.assertEqual(nominations_after, nominations_before)
        self.assertEqual(self.service.audit_chain("audit")["events"], audit_events_before)
        runs = self.connection.execute("SELECT * FROM allocation_runs WHERE route_id='fabric-a-b' AND service_date='2026-09-25'").fetchall()
        self.assertEqual(len(runs), 1)

    def test_facility_rejects_unknown_timezone(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_facility("plan", {"facility_id": "bad-tz", "name": "未知时区机房", "kind": "storage", "timezone": "Mars/Olympus", "capacity_gpu_hours": "1"})

    def test_api_service_day_report(self) -> None:
        app = JsonApplication(self.service)
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-24T20:00:00Z", "2026-09-24T22:00:00Z", "50", "检修")
        response = app.handle("GET", "/routes/fabric-a-b/capacity?service_date=2026-09-25", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        body = response.body
        self.assertEqual(body["timezone"], "Asia/Shanghai")
        self.assertEqual(body["window_start_utc"], "2026-09-24T16:00:00Z")
        self.assertEqual(body["window_end_utc"], "2026-09-25T16:00:00Z")
        self.assertEqual(body["nominal_gpu_hours"], "100000.000")
        self.assertEqual(len(body["outages"]), 1)
        self.assertTrue(body["segments"])
        missing = app.handle("GET", "/routes/fabric-a-b/capacity", {"X-Actor-Id": "audit"})
        self.assertEqual(missing.status, 422)
        unknown = app.handle("GET", "/routes/nope/capacity?service_date=2026-09-25", {"X-Actor-Id": "audit"})
        self.assertEqual(unknown.status, 404)

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/quotes/summary/PEAK_VALLEY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


class AcceptanceFlowTests(unittest.TestCase):
    def test_offline_acceptance_explains_service_days(self) -> None:
        result = acceptance.run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["audit"]["valid"])
        day_of = result["service_days"]["wlmq_2026-09-25"]
        self.assertEqual(day_of["window_start_utc"], "2026-09-24T18:00:00Z")
        self.assertEqual(day_of["effective_gpu_hours"], "23750.000")
        self.assertEqual(day_of["outages"][0]["overlap_hours"], "0.500")
        open_ended = result["service_days"]["shanghai_open_ended_2026-09-28"]
        self.assertEqual(open_ended["effective_gpu_hours"], "80000.000")
        self.assertEqual(result["wlmq_allocation"]["available_capacity"], "23750.000")
        self.assertTrue(result["wlmq_replay"]["replayed"])
        self.assertEqual(result["wlmq_replay"]["allocation_id"], result["wlmq_allocation"]["allocation_id"])


if __name__ == "__main__":
    unittest.main()
