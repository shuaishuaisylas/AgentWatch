"""Artificial regressions for outstanding human interactions, never real logs."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.bridge import sanitize as sanitize_claude
from agentwatch.collector import RecordParser
from agentwatch.lifecycle_bridge import sanitize
from agentwatch.store import Store


AT = 1792029600.0
PRIVATE = "ARTIFICIAL_QUESTION_ANSWER_AND_TOOL_CONTENT_MUST_NOT_PERSIST"


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "watch.sqlite"
        self.store = Store(self.path)
        self.parser = RecordParser(self.store)
        self.root = self.session("root")
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, identity, role="main", parent=None, **extra):
        sid = self.store.session("codex", identity)
        self.store.update(sid, {"role": role, "parent_id": parent, "last_event_at": AT,
                                "state": "active", "state_source": "hook", "state_at": AT,
                                "liveness": "online", "attention_covered": 1, **extra})
        return sid

    def snapshot(self, **args):
        with patch("agentwatch.store.now", return_value=AT + 100):
            return self.store.snapshot({"role": "main", "tool": "all", "project": "all", **args}, {})

    def card(self, sid=None):
        return next(card for card in self.snapshot()["sessions"] if card["id"] == (sid or self.root))

    def record(self, payload, sid=None, at=None, kind="response_item"):
        self.sequence += 1
        sid = sid or self.root
        return self.parser.codex({"type": kind, "timestamp": at or AT + self.sequence,
                                  "payload": payload}, {"sid": sid, "turn_id": "turn-artificial"})

    def request(self, call="call-one", count=1, sid=None, name="functions.request_user_input_async", item=None,
                deliver=True):
        asynchronous = name.rsplit(".", 1)[-1] == "request_user_input_async"
        question_field = "title" if asynchronous else "question"
        payload = {"type": "function_call", "call_id": call, "name": name,
                   "arguments": json.dumps({"questions": [
                       {"id": "q" + str(index), "header": "人工样例", question_field: PRIVATE,
                        "options": [{"label": "选项", "description": PRIVATE}]}
                       for index in range(count)]})}
        if item is not None:
            payload["id"] = item
        result = self.record(payload, sid=sid)
        if asynchronous and deliver:
            self.output(call=call, sid=sid, value={"accepted": True})
        return result

    def output(self, call="call-one", sid=None, value=None):
        return self.record({"type": "function_call_output", "call_id": call,
                            "output": json.dumps(value if value is not None else {"accepted": True})}, sid=sid)

    def reply(self, source="call-one", indexes=(0,), sid=None, text=None):
        if text is None:
            values = [{"questionItemId": json.dumps(["request_user_input_async", source, index], separators=(",", ":")),
                       "question": PRIVATE, "answer": PRIVATE} for index in indexes]
            text = "<send_user_message_question_reply>\n" + json.dumps(values) + "\n</send_user_message_question_reply>"
        return self.record({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}, sid=sid)

    def lifecycle(self, event, sid=None, **extra):
        self.sequence += 1
        sid = sid or self.root
        row = sanitize({"session_id": sid.split(":", 1)[1], "hook_event_name": event,
                        "turn_id": "turn-artificial", **extra}, "codex", at=AT + self.sequence)
        return self.parser.lifecycle(row, {})

    def claude_hook(self, event, identity="claude-root", **extra):
        self.sequence += 1
        sid = self.store.session("claude", identity)
        self.store.update(sid, {"role": "main", "liveness": "online"})
        row = sanitize_claude({"session_id": identity, "hook_event_name": event, **extra}, at=AT + self.sequence)
        self.parser.hook(row, {})
        return sid

    def test_async_request_is_pending_without_stopping_confirmed_execution(self):
        self.request()
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertEqual(self.snapshot()["counts"]["active"], 1)
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)

    def test_async_acceptance_and_tool_completion_do_not_mean_a_human_answered(self):
        self.request()
        self.output(value={"accepted": True, "message": PRIVATE})
        self.lifecycle("PostToolUse", tool_use_id="call-one", tool_name="request_user_input_async")
        self.assertTrue(self.card()["attention"]["pending"])
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)

    def test_async_hook_needs_confirmed_delivery_while_execution_remains_confirmed(self):
        self.lifecycle("PreToolUse", tool_use_id="async-hook", tool_name="functions.request_user_input_async")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 1)
        self.assertEqual(snapshot["attention_counts"]["pending"], 0)
        self.assertGreaterEqual(self.card()["attention"]["unknown"], 1)
        self.request(call="async-hook", deliver=False)
        self.output(call="async-hook", value={"accepted": True})
        snapshot = self.snapshot()
        self.assertEqual(snapshot["counts"]["active"], 1)
        self.assertEqual(snapshot["attention_counts"]["pending"], 1)

    def test_async_failed_or_unconfirmed_delivery_never_becomes_human_pending(self):
        cases = [None, {"accepted": False}, {"error": PRIVATE}, {"isError": True}, {"accepted": 1}, "unstructured error"]
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                sid = self.session("unconfirmed-" + str(index))
                self.request(call="undelivered", count=2, sid=sid, deliver=False)
                if value is not None:
                    self.output(call="undelivered", sid=sid, value=value)
                self.assertFalse(self.card(sid)["attention"]["pending"])
                self.assertEqual(self.card(sid)["attention"]["waiting_user"], 0)
                self.assertGreaterEqual(self.card(sid)["attention"]["unknown"], 1)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)

    def test_async_title_schema_is_delivered_then_resolved_by_matching_reply(self):
        self.request(call="title-call", count=2, item="title-message", deliver=False)
        self.assertFalse(self.card()["attention"]["pending"])
        self.output(call="unknown-call", value={"accepted": True})
        self.assertFalse(self.card()["attention"]["pending"])
        self.output(call="title-call", value={"accepted": True})
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)
        self.reply(source="title-message", indexes=(0, 1))
        self.assertFalse(self.card()["attention"]["pending"])

    def test_alias_reply_resolves_only_the_selected_question_index(self):
        self.request(count=2, item="question-message-item")
        self.reply(source="question-message-item", indexes=(0,))
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.reply(source="question-message-item", indexes=(1,))
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)

    def test_plain_user_text_or_new_work_does_not_resolve_an_async_question(self):
        self.request()
        self.reply(text="普通消息 " + PRIVATE)
        self.lifecycle("UserPromptSubmit")
        self.lifecycle("PreToolUse", tool_use_id="another-tool", tool_name="Bash")
        self.assertTrue(self.card()["attention"]["pending"])

    def test_unknown_call_and_invalid_question_index_cannot_resolve_a_request(self):
        self.request(count=2)
        self.reply(source="unknown-call", indexes=(0, 1))
        self.reply(indexes=(-1, 2, 999))
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)
        self.reply(indexes=(0, 1))
        self.assertFalse(self.card()["attention"]["pending"])

    def test_reply_identity_is_scoped_to_the_originating_session(self):
        other = self.session("other")
        self.request(item="shared-looking-item")
        self.reply(source="shared-looking-item", sid=other)
        self.assertTrue(self.card()["attention"]["pending"])
        self.reply(source="shared-looking-item")
        self.assertFalse(self.card()["attention"]["pending"])

    def test_restarts_and_duplicate_records_preserve_closed_request_identity(self):
        self.request(item="message-item")
        self.request(item="message-item")
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.parser = RecordParser(self.store)
        self.assertTrue(self.card()["attention"]["pending"])
        self.reply(source="message-item")
        self.request(item="message-item")
        self.assertFalse(self.card()["attention"]["pending"])

    def test_multiple_questions_and_calls_count_a_main_conversation_once(self):
        self.request(count=3)
        self.request(call="call-two", count=2)
        other = self.session("other")
        self.request(call="another-root-call", sid=other)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["attention_counts"]["pending"], 2)
        self.assertEqual(snapshot["attention_counts"]["waiting_user"], 2)
        self.assertEqual(self.card()["attention"]["waiting_user"], 5)

    def test_child_requests_roll_up_only_along_explicit_visible_relationships(self):
        child = self.session("child", role="child", parent=self.root)
        grandchild = self.session("grandchild", role="child", parent=child)
        self.request(sid=grandchild)
        self.request(sid=child, call="child-second")
        hidden = self.session("hidden", role="child", parent=self.root, internal=1)
        self.request(sid=hidden, call="hidden-call")
        unknown = self.session("unknown", role="unknown", parent=self.root)
        self.request(sid=unknown, call="unknown-call")
        orphan = self.session("under-unknown", role="child", parent=unknown)
        self.request(sid=orphan, call="orphan-call")
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)
        with patch("agentwatch.store.now", return_value=AT + 100):
            subtree = self.store.children(self.root)
        self.assertEqual({item["id"] for item in subtree["children"]}, {child, grandchild})
        self.assertTrue(all(item["attention"]["pending"] for item in subtree["children"]))

    def test_complete_attention_aggregate_does_not_use_the_160_card_limit(self):
        for index in range(170):
            sid = self.session("root-" + str(index))
            self.store.open_interaction(sid, "artificial-" + str(index), 0, "waiting_user", AT)
        snapshot = self.snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(len(snapshot["sessions"]), 160)
        self.assertEqual(snapshot["attention_counts"]["pending"], 170)

    def test_unknown_approval_and_errors_do_not_inflate_human_pending(self):
        self.store.open_interaction(self.root, "unknown-approval", 0, "unknown", AT,
                                    tool_name="Bash", asynchronous=False)
        error = self.session("error", state="error")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["attention_counts"]["pending"], 0)
        self.assertGreaterEqual(snapshot["attention_counts"]["unknown"], 1)
        self.assertEqual(snapshot["attention_counts"]["error"], 1)
        self.assertFalse(self.card(error)["attention"]["pending"])

    def test_permission_request_without_human_reviewer_evidence_is_unknown(self):
        self.lifecycle("PermissionRequest", tool_use_id="approval-call", tool_name="Bash")
        attention = self.card()["attention"]
        self.assertFalse(attention["pending"])
        self.assertEqual(attention["waiting_approval"], 0)
        self.assertGreaterEqual(attention["unknown"], 1)

    def test_claude_untyped_question_notification_preserves_class_and_resolves_with_its_tool(self):
        sid = self.claude_hook("PreToolUse", tool_use_id="claude-question", tool_name="AskUserQuestion")
        self.claude_hook("Notification", notification_type="permission_prompt")
        attention = self.card(sid)["attention"]
        self.assertEqual(attention["waiting_user"], 1)
        self.assertEqual(attention["waiting_approval"], 0)
        self.claude_hook("PostToolUse", tool_use_id="claude-question", tool_name="AskUserQuestion")
        self.assertFalse(self.card(sid)["attention"]["pending"])

    def test_unidentified_notification_without_known_request_stays_unknown(self):
        sid = self.claude_hook("Notification", notification_type="permission_prompt")
        attention = self.card(sid)["attention"]
        self.assertFalse(attention["pending"])
        self.assertGreaterEqual(attention["unknown"], 1)

    def test_notification_can_confirm_a_unique_approval_and_matching_completion_clears_it(self):
        sid = self.claude_hook("PermissionRequest", tool_use_id="manual-call", tool_name="Bash")
        self.assertFalse(self.card(sid)["attention"]["pending"])
        self.claude_hook("Notification", notification_type="permission_prompt", tool_name="Bash")
        self.assertEqual(self.card(sid)["attention"]["waiting_approval"], 1)
        self.claude_hook("PostToolUse", tool_use_id="manual-call", tool_name="Bash")
        self.assertFalse(self.card(sid)["attention"]["pending"])

    def test_confirmed_human_approval_is_separate_from_unknown_review(self):
        self.store.open_interaction(self.root, "manual-approval", 0, "waiting_approval", AT,
                                    tool_name="Bash", asynchronous=False)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertEqual(self.card()["attention"]["waiting_approval"], 1)
        self.store.resolve_interaction(self.root, "manual-approval", AT + 10)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)

    def test_sync_question_requires_an_actual_structured_answer(self):
        self.request(name="functions.request_user_input")
        self.output(value={"accepted": True})
        self.assertTrue(self.card()["attention"]["pending"])
        self.output(value={"answers": {"q0": {"answers": [None]}}})
        self.assertTrue(self.card()["attention"]["pending"])
        self.output(value={"answers": {"q0": {"answers": [PRIVATE]}}})
        self.assertFalse(self.card()["attention"]["pending"])

    def test_embedded_wrapper_or_multiple_text_parts_are_not_a_native_reply(self):
        self.request()
        item = {"questionItemId": json.dumps(["request_user_input_async", "call-one", 0], separators=(",", ":")),
                "question": PRIVATE, "answer": PRIVATE}
        wrapper = "<send_user_message_question_reply>" + json.dumps(item) + "</send_user_message_question_reply>"
        for text in ["解释此格式：" + wrapper, wrapper + "只是一个样例"]:
            self.reply(text=text)
            self.assertTrue(self.card()["attention"]["pending"])
        self.record({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": wrapper}, {"type": "input_text", "text": "额外消息"}]})
        self.assertTrue(self.card()["attention"]["pending"])

    def test_incomplete_reply_schema_cannot_close_a_known_request(self):
        self.request()
        question_id = json.dumps(["request_user_input_async", "call-one", 0], separators=(",", ":"))
        for item in [
                {"questionItemId": question_id},
                {"questionItemId": question_id, "question": PRIVATE},
                {"questionItemId": question_id, "question": PRIVATE, "answer": None},
                {"questionItemId": question_id, "question": None, "answer": PRIVATE},
                {"questionItemId": json.dumps(["functions.request_user_input_async", "call-one", 0]),
                 "question": PRIVATE, "answer": PRIVATE}]:
            self.reply(text="<send_user_message_question_reply>" + json.dumps(item) + "</send_user_message_question_reply>")
            self.assertTrue(self.card()["attention"]["pending"])

    def test_only_exact_supported_question_tool_names_open_requests(self):
        self.request(name="functions.not_request_user_input_async")
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)
        self.request(name="request_user_input_async", call="bare-call")
        self.request(name="functions.request_user_input_async", call="namespaced-call")
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)

    def test_recent_completion_is_visible_main_only_sorted_and_bounded(self):
        expected = []
        for index in range(8):
            sid = self.session("done-" + str(index), state="idle")
            self.store.complete(sid, AT + index)
            expected.append(sid)
        child = self.session("done-child", role="child", parent=self.root)
        self.store.complete(child, AT + 50)
        hidden = self.session("done-hidden", archived=1)
        self.store.complete(hidden, AT + 60)
        unknown = self.session("done-unknown", role="unknown")
        self.store.complete(unknown, AT + 70)
        old = self.session("done-old")
        self.store.complete(old, AT - 86401)
        self.assertEqual([item["id"] for item in self.snapshot()["recent_completed"]], list(reversed(expected))[:5])

    def test_busy_pending_or_unknown_completions_do_not_hide_older_eligible_completion(self):
        eligible = self.session("eligible-completion", state="idle")
        self.store.complete(eligible, AT)
        for index in range(5):
            busy = self.session("busy-completed-" + str(index))
            self.store.complete(busy, AT + 10 + index)
        pending = self.session("pending-completed", state="idle")
        self.store.open_interaction(pending, "still-needs-reply", 0, "waiting_user", AT)
        self.store.complete(pending, AT + 30)
        unknown = self.session("unknown-completed", state="idle", attention_covered=0)
        self.store.complete(unknown, AT + 40)
        self.assertEqual([item["id"] for item in self.snapshot()["recent_completed"]], [eligible])

    def test_abort_and_session_exit_are_not_successful_completion_evidence(self):
        interrupted = self.session("interrupted")
        self.record({"type": "turn_aborted", "turn_id": "aborted-turn"}, sid=interrupted, kind="event_msg")
        exited = self.session("exited")
        self.lifecycle("SessionEnd", sid=exited)
        completed = self.session("completed", state_source="record")
        self.record({"type": "task_complete", "turn_id": "completed-turn"}, sid=completed, kind="event_msg")
        ids = {item["id"] for item in self.snapshot()["recent_completed"]}
        self.assertIn(completed, ids)
        self.assertNotIn(interrupted, ids)
        self.assertNotIn(exited, ids)

    def test_questions_answers_and_tool_outputs_never_enter_persistent_storage(self):
        self.request(count=2, item="privacy-item")
        self.output(value={"accepted": True, "payload": PRIVATE})
        self.reply(source="privacy-item", indexes=(0,))
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))
        self.assertNotIn("send_user_message_question_reply", json.dumps(self.snapshot()))


if __name__ == "__main__":
    unittest.main()
