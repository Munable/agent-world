"""Checkpoint rejection must identify the actual boundary without supplying IDs.

These controller contracts preserve admission and stored evidence; they do not
establish small-model recovery or live-world completion.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock

from agent_world.grove_connector import EXECUTION_CHECKPOINT_SCHEMA
from agent_world.grove_connector_errors import ConnectorError
from agent_world.grove_execution import ExecutionController, ExecutionPorts


class CheckpointFeedbackContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.invoke = AsyncMock(return_value={"result": {"goal": "Observe a public change", "status": "active",
            "actions_count": 1, "max_actions": 8, "version": 7}})
        self.read = AsyncMock(return_value={"player": {"id": "person", "x": 1, "y": 0}, "entities": []})
        self.persist = Mock()
        self.controller = ExecutionController("person", max_requests=4, max_world_calls=16, max_seconds=150,
            ports=ExecutionPorts(invoke=self.invoke, read=self.read, recover=AsyncMock(),
                unknown_operations=Mock(return_value=[]), native_segment=AsyncMock(),
                settle=AsyncMock(return_value=True), persist=self.persist))
        self.controller.state.update(status="running", goal="Observe a public change", task_id="private-current-task",
            segment_id="private-current-segment", actions_count=1, max_actions=8, delegation_active=True,
            grant_operation_id="original-grant", control_status="active", receipts=[{
                "function": "grove.move", "operation_id": "original-action", "status": "confirmed",
                "task_id": "private-current-task", "segment_id": "private-current-segment", "commit_seq": 3}])
        self.controller.active = True
        self.controller.delegation = "private-current-grant"

    def report(self, **changes):
        return {"task_id": "private-current-task", "execution_segment_id": "private-current-segment",
            "status": "continue", "summary": "One actual move was confirmed.", "next_intent": "Read the result.",
            "evidence_operation_ids": ["original-action"]} | changes

    def protected(self):
        return deepcopy((self.controller.state, self.controller.archive, self.controller.delegation,
            self.controller.active, self.controller.stopped, self.controller.reclaiming))

    async def rejection(self, report, *, contains, absent=()):
        saved, original = self.protected(), deepcopy(report)
        with self.assertRaises(ConnectorError) as caught:
            await self.controller.checkpoint(report, schema=EXECUTION_CHECKPOINT_SCHEMA)
        error = caught.exception
        self.assertEqual((error.code, error.status), ("NoActiveExecution", 409))
        self.assertIn(contains, error.message)
        for text in (*absent, "private-current-task", "private-current-segment", "private-current-grant"):
            self.assertNotIn(text, json.dumps(error.public_payload(), ensure_ascii=False))
        self.assertFalse(error.public_payload()["retryable"])
        self.assertIsNone(error.execution_outcome)
        self.assertEqual(self.protected(), saved)
        self.assertEqual(report, original)
        self.invoke.assert_not_awaited()
        self.read.assert_not_awaited()
        self.persist.assert_not_called()

    async def test_wrong_task_is_named_before_wrong_segment_without_replacing_either(self):
        await self.rejection(self.report(task_id="old-task", execution_segment_id="old-segment"),
            contains="task_id与当前获准任务不一致", absent=("execution_segment_id与", "old-task", "old-segment"))

    async def test_wrong_segment_is_not_reported_as_a_control_change(self):
        await self.rejection(self.report(execution_segment_id="old-segment"),
            contains="execution_segment_id与当前活动段不一致", absent=("本人正在收回", "执行已停止", "old-segment"))

    async def test_stopped_and_reclaiming_boundaries_take_priority_over_wrong_ids(self):
        self.controller.stopped = True
        await self.rejection(self.report(task_id="old-task", execution_segment_id="old-segment"),
            contains="本次执行已停止", absent=("task_id与", "execution_segment_id与"))
        self.controller.reclaiming = True
        await self.rejection(self.report(task_id="old-task", execution_segment_id="old-segment"),
            contains="本人正在收回控制", absent=("本次执行已停止", "task_id与", "execution_segment_id与"))

    async def test_inactive_or_nonrunning_segment_is_not_reopened_by_matching_ids(self):
        self.controller.active = False
        await self.rejection(self.report(), contains="当前执行段未在活动执行状态")
        self.controller.active = True
        self.controller.state["status"] = "paused"
        await self.rejection(self.report(), contains="当前执行段未在活动执行状态")

    async def test_correct_ids_keep_formal_readback_and_close_only_the_current_segment(self):
        report = self.report()
        original = deepcopy(report)
        saved_receipts = deepcopy(self.controller.state["receipts"])
        result = await self.controller.checkpoint(report, schema=EXECUTION_CHECKPOINT_SCHEMA)
        self.assertTrue(result["segment_finished"])
        self.assertFalse(result["world_facts_changed"])
        self.assertEqual(result["execution_readback"]["player"], {"id": "person", "x": 1, "y": 0})
        self.assertEqual(self.controller.state["checkpoint"], original)
        self.assertEqual(self.controller.state["receipts"], saved_receipts)
        self.assertFalse(self.controller.active)
        self.assertEqual(self.controller.state["status"], "running")
        self.assertTrue(self.controller.state["delegation_active"])
        self.assertEqual(self.controller.delegation, "private-current-grant")
        self.assertEqual(report, original)
        self.invoke.assert_awaited_once_with("grove.control_status", {})
        self.read.assert_awaited_once_with("facts", context_entity_id=None)
        self.invoke.reset_mock()
        self.read.reset_mock()
        self.persist.reset_mock()
        await self.rejection(self.report(task_id="old-task", execution_segment_id="old-segment"),
            contains="这一执行段已封闭", absent=("task_id与", "execution_segment_id与"))

    async def test_stop_during_readback_does_not_record_a_late_checkpoint(self):
        for reclaiming in (False, True):
            with self.subTest(reclaiming=reclaiming):
                self.setUp()
                async def stop_during_read(*args, **kwargs):
                    self.controller.stopped = True
                    self.controller.reclaiming = reclaiming
                    return {"player": {"id": "person", "x": 1, "y": 0}, "entities": []}
                self.read.side_effect = stop_during_read
                saved_receipts = deepcopy(self.controller.state["receipts"])
                with self.assertRaises(ConnectorError) as caught:
                    await self.controller.checkpoint(self.report(), schema=EXECUTION_CHECKPOINT_SCHEMA)
                error = caught.exception
                self.assertEqual((error.code, error.status), ("NoActiveExecution", 409))
                self.assertIn("读取期间本人开始收回控制" if reclaiming else "读取期间执行已停止", error.message)
                self.assertNotIn("private-current-segment", error.message)
                self.assertIsNone(self.controller.state["checkpoint"])
                self.assertIsNone(self.controller.state["goal_result"])
                self.assertEqual(self.controller.state["receipts"], saved_receipts)
                self.assertTrue(self.controller.stopped)


if __name__ == "__main__":
    unittest.main()
