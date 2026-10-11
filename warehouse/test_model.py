# -*- coding: utf-8 -*-
"""Focused regression tests for all case 2.4 warehouse models."""
# 本文件是 2.4 三个选题的回归测试。
# 回归测试的含义是：以后修改代码时，只要这些业务规则被破坏，测试就会失败。
import unittest
from collections import defaultdict

import data
import integration
import managed
import model
import run as warehouse_run


def _overlaps(intervals):
    """返回一组时间区间中是否存在重叠，用于验证资源不能同时开工。"""
    intervals = sorted(intervals)
    return any(
        current[0] < previous[1] - 1e-6
        for previous, current in zip(intervals, intervals[1:])
    )


class WarehouseModelTest(unittest.TestCase):
    """集中验证 2.4.4(1)、2.4.4(2)、2.4.4(3) 的关键可行性规则。"""
    @classmethod
    def setUpClass(cls):
        # 三个选题共用同一套货位、批次和订单，避免各组结果放在不同数据上比较。
        cls.slots = model.build_slots()
        cls.batches = model.generate_batches()
        cls.orders = model.generate_orders()
        cls.results = {
            name: warehouse_run.strategy_result(
                name, cls.slots, cls.batches, cls.orders)
            for name in ("experience", "data_driven")
        }
        cls.merchants = managed.generate_merchants()
        cls.categories = managed.generate_categories()
        cls.managed_batches = managed.generate_batches(
            cls.merchants, cls.categories)
        cls.managed_results = {
            name: managed.run_strategy(
                name, cls.slots, cls.managed_batches,
                cls.merchants, cls.categories)
            for name in ("random", "optimized")
        }

    def test_data_quality_checks_pass(self):
        """基础数据应能通过容量、数量和时间范围的完整性检查。"""
        self.assertTrue(
            all(passed for _name, passed in data.data_quality_checks()))

    def test_pilot_sizing_covers_peak_and_stays_within_pilot_scope(self):
        """试点货位数要覆盖峰值需求，又不能超出案例规定的试点范围。"""
        sizing = data.required_slots()
        self.assertEqual(sizing["daily_pieces"], 190)
        self.assertEqual(sizing["required_slots"], 2037)
        self.assertGreaterEqual(data.layout_slots(), sizing["required_slots"])
        self.assertLessEqual(
            data.layout_slots(), 0.15 * data.CASE_FACTS["stereoscopic_slots"])
        self.assertEqual(data.pilot_area_m2(), 3000)

    def test_layout_has_coordinates_and_2400_unique_slots(self):
        """每个货位要有唯一编号和三维位置，且堆垛机、提升机时间可计算。"""
        self.assertEqual(len(self.slots), 2400)
        self.assertEqual(len({slot.slot_id for slot in self.slots}), 2400)
        self.assertTrue(all(slot.x_m >= 0 for slot in self.slots))
        self.assertTrue(all(slot.stacker_seconds > 0 for slot in self.slots))
        self.assertTrue(all(slot.lift_seconds > 0 for slot in self.slots))

    def test_scenario_covers_three_days_and_all_equipment_tasks(self):
        """场景要覆盖三天出库周期，并生成全部 760 个托盘设备任务。"""
        self.assertEqual(sum(batch.quantity for batch in self.batches), 2090)
        self.assertEqual(len(self.orders), 33)
        for name, result in self.results.items():
            self.assertEqual(len(result["tasks"]), 760, name)
            days = {task.order_day for task in result["tasks"]}
            self.assertEqual(days, {0, 1, 2})

    def test_assignments_are_unique_complete_and_inside_layout(self):
        """所有件数都必须分到货位，货位不能重复，也不能引用不存在的库位。"""
        known_slots = {slot.slot_id for slot in self.slots}
        for name, result in self.results.items():
            assigned_ids = []
            for batch in self.batches:
                assigned = result["allocation"].get(batch.batch_id, [])
                self.assertEqual(len(assigned), batch.quantity, name)
                assigned_ids.extend(slot.slot_id for slot in assigned)
            self.assertEqual(len(assigned_ids), len(set(assigned_ids)))
            self.assertTrue(set(assigned_ids).issubset(known_slots))

    def test_data_driven_strategy_improves_fifo_and_distance(self):
        """数据驱动策略要实现 100% FIFO，并降低平均拣选时间。"""
        experience = self.results["experience"]["picking"]
        optimized = self.results["data_driven"]["picking"]
        self.assertEqual(optimized["fifo_compliance_pct"], 100.0)
        self.assertGreater(
            optimized["fifo_compliance_pct"],
            experience["fifo_compliance_pct"])
        self.assertLess(
            optimized["average_retrieval_seconds"],
            experience["average_retrieval_seconds"])

    def test_equipment_chain_dependencies_and_resource_exclusion(self):
        """设备链必须按堆垛机、提升机、AGV、分拣顺序交接，且同一设备不能重叠。"""
        for name, result in self.results.items():
            by_resource = {
                "stacker": defaultdict(list),
                "lift": defaultdict(list),
                "agv": defaultdict(list),
                "sorter": defaultdict(list),
            }
            for row in result["equipment"]["detail"]:
                # 前一道工序结束时间，是后一道工序最早可开始的时间。
                self.assertGreaterEqual(
                    row["stacker_start"], row["release_min"] - 1e-6)
                self.assertGreaterEqual(
                    row["lift_start"], row["stacker_end"] - 1e-6)
                self.assertGreaterEqual(
                    row["agv_start"], row["lift_end"] - 1e-6)
                if row["sorter_id"]:
                    self.assertGreaterEqual(
                        row["sorter_start"], row["agv_end"] - 1e-6)
                    by_resource["sorter"][row["sorter_id"]].append(
                        (row["sorter_start"], row["sorter_end"]))
                by_resource["stacker"][row["stacker_id"]].append(
                    (row["stacker_start"], row["stacker_end"]))
                by_resource["lift"][row["lift_id"]].append(
                    (row["lift_start"], row["lift_end"]))
                by_resource["agv"][row["agv_id"]].append(
                    (row["agv_start"], row["agv_end"]))
            for resource, assignments in by_resource.items():
                for intervals in assignments.values():
                    self.assertFalse(
                        _overlaps(intervals),
                        "%s %s resource overlap" % (name, resource))

    def test_cold_work_limit_is_enforced(self):
        """同一低温班组的连续作业不能超过 120 分钟，且必须实际产生强制休息。"""
        for name, result in self.results.items():
            self.assertLessEqual(
                result["equipment"]["max_continuous_work_min"],
                data.ASSUMPTIONS["cold_work_limit_min"] + 1e-6,
                name,
            )
            self.assertGreater(result["equipment"]["cold_breaks"], 0)

    def test_dock_schedule_uses_independent_eta_and_correct_wait(self):
        """预约时间不能被直接当成实际到达；装货要等车到、货齐，等待公式要一致。"""
        experience = self.results["experience"]["docks"]
        optimized = self.results["data_driven"]["docks"]
        for name, docks in (("experience", experience), ("data_driven", optimized)):
            by_dock = defaultdict(list)
            for row in docks["detail"]:
                self.assertGreaterEqual(
                    row["start_min"], row["arrival_min"] - 1e-6, name)
                self.assertGreaterEqual(
                    row["start_min"], row["ready_min"] - 1e-6, name)
                self.assertAlmostEqual(
                    row["wait_min"], row["start_min"] - row["arrival_min"],
                    places=6,
                )
                by_dock[row["dock_id"]].append(
                    (row["start_min"], row["end_min"]))
            for intervals in by_dock.values():
                self.assertFalse(_overlaps(intervals), name)
        different = [
            row for row in optimized["detail"]
            if abs(row["arrival_min"] - row["appointment_min"]) > 1e-6
        ]
        self.assertGreater(len(different), 0.6 * len(optimized["detail"]))
        self.assertLessEqual(optimized["average_wait_min"], 30.0)
        self.assertLess(
            optimized["average_wait_min"],
            experience["average_wait_min"])

    def test_optimized_equipment_schedule_is_not_materially_worse(self):
        """优化策略不能以大幅延长设备作业跨度为代价换取局部指标。"""
        experience = self.results["experience"]["equipment"]["makespan_min"]
        optimized = self.results["data_driven"]["equipment"]["makespan_min"]
        self.assertLessEqual(optimized, experience * 1.02)

    def test_managed_allocation_keeps_full_traceability(self):
        """代管仓的每一条拣选记录都能回查商家、品类、批次和具体货位。"""
        batch_map = {
            batch.batch_id: batch for batch in self.managed_batches}
        for strategy, result in self.managed_results.items():
            assigned = []
            for batch_id, slots in result["allocation"].items():
                assigned.extend(slot.slot_id for slot in slots)
                self.assertEqual(len(slots), batch_map[batch_id].quantity)
            self.assertEqual(len(assigned), len(set(assigned)))
            for line in result["lines"]:
                batch = batch_map[line["batch_id"]]
                self.assertEqual(line["merchant_id"], batch.merchant_id)
                self.assertEqual(line["category_id"], batch.category_id)
                self.assertTrue(
                    set(line["slot_ids"]).issubset(
                        {slot.slot_id for slot in result["allocation"][line["batch_id"]]}
                    )
                )

    def test_managed_slotting_reduces_flip_rate_and_merchant_span(self):
        """优化货位算法要降低双深位翻箱率和同一商家的跨货架分散度。"""
        random_metrics = self.managed_results["random"]["metrics"]
        optimized_metrics = self.managed_results["optimized"]["metrics"]
        self.assertLessEqual(
            optimized_metrics["flip_rate_pct"],
            random_metrics["flip_rate_pct"])
        self.assertLess(
            optimized_metrics["average_merchant_rack_span"],
            random_metrics["average_merchant_rack_span"])
        self.assertEqual(optimized_metrics["fifo_compliance_pct"], 100.0)

    def test_integration_lifecycle_and_wait_improvement(self):
        """协同方案要保留完整状态机、让 ETA 独立，并降低平均月台等待。"""
        orders, ready_by_order, _equipment = integration._warehouse_context()
        manual = integration.build_linked_vehicles(
            orders, ready_by_order, "manual")
        integrated = integration.build_linked_vehicles(
            orders, ready_by_order, "integrated")
        integration._assign_docks(manual, use_forecast=False)
        integration._assign_docks(integrated, use_forecast=True)
        appointment_differences = 0
        for row in integrated:
            if abs(row.actual_arrival_min - row.appointment_min) > 1e-6:
                appointment_differences += 1
            integration.build_state_history(row)
            self.assertGreaterEqual(
                row.start_min, row.actual_arrival_min - 1e-6)
            self.assertGreaterEqual(row.start_min, row.ready_min - 1e-6)
            states = [event["state"] for event in row.transitions]
            self.assertIn("DEPARTED", states)
            self.assertIn("APPOINTMENT_CONFIRMED", states)
        self.assertGreater(
            appointment_differences, 0.6 * len(integrated))
        manual_wait = integration.summarize(manual)["average_wait_min"]
        integrated_wait = integration.summarize(integrated)["average_wait_min"]
        self.assertLess(integrated_wait, manual_wait)
        self.assertGreaterEqual(len(integration.interface_contracts()), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
