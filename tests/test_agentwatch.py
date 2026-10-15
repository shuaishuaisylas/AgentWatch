from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from agentwatch.bridge import append_private, sanitize, EVENTS
from agentwatch.collector import Collector, RecordParser
from agentwatch.common import number, timestamp
from agentwatch.configuration import ClaudeConfiguration, ConfigurationError
from agentwatch.navigation import jump, NavigationError
from agentwatch.server import AppServer
from agentwatch.store import Store


SECRET = "PRIVATE_PROMPT_TOOL_RESULT_API_KEY_987654"
AT = timestamp("2026-10-15T10:00:00+08:00")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.desktop = self.root / "desktop"
        self.workbuddy = self.root / "workbuddy"
        for path in [self.data,self.codex,self.claude,self.desktop,self.workbuddy]:
            path.mkdir()
        self.store = Store(self.data / "watch.sqlite")
        self.parser = RecordParser(self.store)
        self.collector = Collector(self.store,self.codex,self.claude,self.data,self.desktop,workbuddy_home=self.workbuddy)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def cx(self, totals, recent=None, at=AT, cursor=None):
        sid = self.store.session("codex","session1")
        self.store.metadata(sid,role="main")
        def usage(pair):
            return dict(input_tokens=pair[0],output_tokens=pair[1],cached_input_tokens=pair[2] if len(pair)>2 else 0,reasoning_output_tokens=0,total_tokens=sum(pair[:2]))
        self.parser.codex_usage(sid,{"total_token_usage":usage(totals),"last_token_usage":usage(recent or totals),"model_context_window":200000},at,cursor if cursor is not None else {})
        return sid

    def cl(self, identity="msg1", usage=None, at=AT, cursor=None, **extra):
        doc = {"type":"assistant","sessionId":"session2","isSidechain":False,"timestamp":at,"cwd":str(self.root / "project"),"message":{"id":identity,"content":SECRET,"usage":usage or {"input_tokens":10,"output_tokens":20,"cache_read_input_tokens":30,"cache_creation_input_tokens":40},"stop_reason":"end_turn"},**extra}
        return self.parser.claude(doc,cursor if cursor is not None else {})

    def total(self):
        return self.store.db.execute("SELECT SUM(total) FROM usage").fetchone()[0]

    def wb(self, identity="request1", usage=None, at=AT, cursor=None, kind="message", **extra):
        doc = {"type":kind,"sessionId":"session-wb","id":"record-"+identity,"timestamp":int(at*1000),
               "cwd":str(self.root / "project"),"content":SECRET,
               "providerData":{"messageId":identity,"conversationRequestId":"one-conversation-turn",
                               "usage":usage if usage is not None else {"inputTokens":100,"outputTokens":20,"totalTokens":120}},**extra}
        return self.parser.workbuddy(doc,cursor if cursor is not None else {})


class UsageTests(Fixture):
    def test_claude_normalizes_and_merges_streamed_request(self):
        self.cl()
        self.cl(usage={"input_tokens":10,"output_tokens":35,"cache_read_input_tokens":30,"cache_creation_input_tokens":40})
        self.cl()
        row = self.store.db.execute("SELECT * FROM usage").fetchone()
        self.assertEqual((row["input"],row["output"],row["total"]),(80,35,115))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0],1)

    def test_missing_cache_is_unknown_and_not_fabricated_zero(self):
        sid = self.cl(usage={"input_tokens":10,"output_tokens":20})
        card = self.store.snapshot({"role":"all"},{},AT)["sessions"][0]
        self.assertIsNone(card["context_percent"])
        self.assertIsNone(card["usage"]["cache_read"])
        self.assertEqual(card["coverage"],"partial_usage")
        stats = self.store.stats({"role":"all","date":"2026-10-15"},AT)
        self.assertIsNone(stats["totals"]["cache_write"])
        empty = self.store.stats({"role":"all","date":"2026-10-14"},AT)
        self.assertIsNone(empty["totals"]["total"])

    def test_codex_delta_replay_and_out_of_order(self):
        cursor = {}
        self.cx((100,10,50),cursor=cursor)
        self.cx((150,30,80),(50,20,30),AT+1,cursor)
        self.cx((150,30,80),(50,20,30),AT+1,cursor)
        self.cx((100,10,50),at=AT,cursor=cursor)
        self.assertEqual(self.total(),180)
        self.assertEqual(self.store.db.execute("SELECT SUM(cache_read) FROM usage").fetchone()[0],80)
        self.assertIsNone(self.store.db.execute("SELECT SUM(cache_write) FROM usage").fetchone()[0])

    def test_codex_missing_baseline_uses_only_latest_request(self):
        sid = self.cx((10000,2000),(100,20))
        self.assertEqual(self.total(),120)
        self.assertEqual(self.store.db.execute("SELECT reported_total,coverage FROM sessions WHERE id=?",(sid,)).fetchone()[:],(12000,"partial_baseline"))

    def test_codex_counter_reset_new_epoch(self):
        cursor = {}
        self.cx((100,10),cursor=cursor)
        self.cx((200,20),(100,10),AT+1,cursor)
        self.cx((100,10),at=AT+2,cursor=cursor)
        self.cx((200,20),(100,10),AT+3,cursor)
        self.assertEqual(self.total(),440)
        self.assertEqual(cursor["epoch"],1)

    def test_subagent_with_same_parent_session_id_is_separate(self):
        parent = self.cl()
        child = self.cl(cursor={"child_file":True,"agent_id":"childA"})
        self.assertNotEqual(parent,child)
        row = self.store.db.execute("SELECT role,parent_id FROM sessions WHERE id=?",(child,)).fetchone()
        self.assertEqual(row[:],("child",parent))
        self.assertEqual(self.total(),200)
        main = self.store.stats({"date":"2026-10-15"},AT)
        self.assertEqual(main["totals"]["total"],100)

    def test_codex_fork_prefix_does_not_count_parent_usage(self):
        sid = self.store.session("codex","child")
        cursor = {}
        self.parser.codex({"type":"session_meta","payload":{"id":"parent","source":"vscode"}},cursor,sid)
        self.parser.codex({"type":"event_msg","timestamp":AT,"payload":{"type":"task_started","turn_id":"parentturn"}},cursor,sid)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0],0)
        self.parser.codex({"type":"session_meta","payload":{"id":"child","source":{"subagent":{"thread_spawn":{"parent_thread_id":"parent"}}}}},cursor,sid)
        self.parser.codex({"type":"event_msg","timestamp":AT+1,"payload":{"type":"task_started","turn_id":"childturn"}},cursor,sid)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT role FROM sessions WHERE id=?",(sid,)).fetchone()[0],"child")

    def test_private_contents_never_reach_database_or_api(self):
        self.cl()
        self.parser.codex({"type":"session_meta","payload":{"id":"private","source":"vscode","originator":"codex_vscode","cwd":str(self.root),"title":SECRET,"first_user_message":SECRET}}, {})
        self.parser.codex({"type":"response_item","timestamp":AT,"payload":{"type":"function_call","arguments":SECRET,"text":SECRET}}, {"sid":"codex:private"})
        self.assertNotIn(SECRET,"\n".join(self.store.db.iterdump()))
        self.assertNotIn(SECRET,json.dumps(self.store.snapshot({"role":"all"},{},AT)))

    def test_statistics_timezone_and_window_dedup(self):
        self.cl(at=timestamp("2026-10-14T15:59:00Z"))
        self.cl(identity="msg2",at=timestamp("2026-10-14T16:01:00Z"))
        self.cl(identity="msg3",at=timestamp("2026-10-15T16:01:00Z"))
        day = self.store.stats({"date":"2026-10-15"},AT)
        self.assertEqual((day["totals"]["total"],day["sessions"]),(100,1))
        for period in ["week","month","year"]:
            result = self.store.stats({"date":"2026-10-15","period":period},AT)
            self.assertEqual((result["totals"]["total"],result["sessions"]),(300,1))
            self.assertIsNone(result["tasks"])

    def test_number_rejects_extremes(self):
        for value in [10**1000,float("nan"),float("inf"),-1,True,"12"]:
            self.assertIsNone(number(value))


class WorkBuddyTests(Fixture):
    def test_request_dedup_uses_message_id_not_conversation_turn_or_record_id(self):
        # A single user turn contains several model requests. Each request can
        # appear in reasoning, tool, and message stream records.
        self.wb("model1",usage={"inputTokens":100,"outputTokens":2},kind="reasoning",id="stream-a")
        self.wb("model1",usage={"inputTokens":100,"outputTokens":20},kind="function_call",id="stream-b")
        self.wb("model1",usage={"inputTokens":100,"outputTokens":10},id="stream-c")
        self.wb("model2",usage={"inputTokens":200,"outputTokens":30},id="stream-d")
        self.wb("model2",usage={"inputTokens":200,"outputTokens":30},id="stream-e")
        rows = self.store.db.execute("SELECT input,output,total FROM usage ORDER BY input").fetchall()
        self.assertEqual([tuple(row) for row in rows],[(100,20,120),(200,30,230)])
        self.assertEqual(self.total(),350)

    def test_input_is_cache_inclusive_and_cache_aliases_are_not_added_twice(self):
        self.wb(providerData={"messageId":"cached-request",
            "usage":{"inputTokens":100,"outputTokens":20,"totalTokens":9999},
            "rawUsage":{"prompt_tokens":100,"completion_tokens":20,
                        "prompt_cache_hit_tokens":25,"cache_read_input_tokens":30,
                        "prompt_tokens_details":{"cached_tokens":30},
                        "cache_creation_input_tokens":8,"prompt_cache_write_tokens":10,
                        "completion_thinking_tokens":4,"completion_tokens_details":{"reasoning_tokens":4}}})
        row = self.store.db.execute("SELECT input,output,cache_read,cache_write,reasoning,total FROM usage").fetchone()
        self.assertEqual(tuple(row),(100,20,30,10,4,120))
        self.assertIsNone(self.store.snapshot({"role":"all"},{},AT)["sessions"][0]["context_percent"])

    def test_missing_cache_remains_unknown_and_record_id_is_request_fallback(self):
        cursor = {}
        data = {"usage":{"inputTokens":12,"outputTokens":3}}
        self.wb("fallback1",providerData=data,cursor=cursor)
        self.wb("fallback1",providerData=data,cursor=cursor)
        self.wb("fallback2",providerData=data,cursor=cursor)
        rows = self.store.db.execute("SELECT cache_read,cache_write,reasoning FROM usage").fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(tuple(row)==(None,None,None) for row in rows))
        self.assertEqual(self.total(),30)

    def test_normalized_detail_lists_merge_aliases_and_bound_subset_counts(self):
        self.wb("detail-lists",providerData={"messageId":"detail-lists",
            "usage":{"inputTokens":100,"outputTokens":20,
                     "inputTokensDetails":[{"cached_tokens":60},{"cached_tokens":55}],
                     "outputTokensDetails":[{"reasoning_tokens":12}]},
            "rawUsage":{"prompt_cache_hit_tokens":40,"cache_read_input_tokens":50,
                        "completion_thinking_tokens":8}})
        self.wb("oversized-details",providerData={"messageId":"oversized-details",
            "usage":{"inputTokens":100,"outputTokens":20,
                     "inputTokensDetails":[{"cached_tokens":1000}],
                     "outputTokensDetails":[{"reasoning_tokens":1000}]},
            "rawUsage":{"cache_creation_input_tokens":1000}})
        rows = self.store.db.execute("SELECT cache_read,cache_write,reasoning,total FROM usage ORDER BY cache_read").fetchall()
        self.assertEqual([tuple(row) for row in rows],[(60,None,12,120),(100,100,20,120)])

    def test_unknown_role_is_explicit_and_statistics_respect_local_days(self):
        self.wb("before-midnight",at=timestamp("2026-10-14T15:59:00Z"))
        self.wb("after-midnight",at=timestamp("2026-10-14T16:01:00Z"))
        self.wb("next-day",at=timestamp("2026-10-15T16:01:00Z"))
        row = self.store.db.execute("SELECT role,surface FROM sessions").fetchone()
        self.assertEqual(tuple(row),("unknown","desktop"))
        main = self.store.stats({"tool":"workbuddy","date":"2026-10-15"},AT)
        self.assertIsNone(main["totals"]["total"])
        day = self.store.stats({"tool":"workbuddy","role":"unknown","date":"2026-10-15"},AT)
        self.assertEqual((day["totals"]["total"],day["sessions"]),(120,1))
        week = self.store.stats({"tool":"workbuddy","role":"all","date":"2026-10-15","period":"week"},AT)
        self.assertEqual(week["totals"]["total"],360)
        self.assertEqual(week["by_tool"],{"workbuddy":360})
        self.assertIsNone(week["tasks"])

    def test_tool_completion_is_not_task_completion_or_context_measurement(self):
        cursor = {}
        self.wb(kind="session-meta",cursor=cursor,providerData={})
        self.wb(kind="reasoning",cursor=cursor)
        self.wb(kind="function_call",cursor=cursor,status="completed",arguments=SECRET)
        self.wb(kind="function_call_result",cursor=cursor,status="completed",result=SECRET)
        row = self.store.db.execute("SELECT state,surface,role,context_used,context_limit FROM sessions").fetchone()
        self.assertEqual(tuple(row),("unknown","desktop","unknown",None,None))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0],0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],0)
        card = self.store.snapshot({"role":"all"},{},AT)["sessions"][0]
        self.assertIsNone(card["context_percent"])
        self.assertNotEqual(card["state"],"idle")

    def test_source_body_titles_tool_results_and_credit_are_discarded(self):
        cursor = {}
        for kind in ["session-meta","ai-title","message","reasoning","function_call","function_call_result"]:
            self.wb(kind=kind,cursor=cursor,title=SECRET,aiTitle=SECRET,text=SECRET,
                arguments={"command":SECRET},result=SECRET,credit=SECRET,
                providerData={"messageId":"private-request","usage":{"inputTokens":10,"outputTokens":2},
                              "toolResult":SECRET,"credit":SECRET,"apiKey":SECRET})
        self.assertNotIn(SECRET,"\n".join(self.store.db.iterdump()))
        self.assertNotIn(SECRET,json.dumps(self.store.snapshot({"role":"all"},{},AT)))
        self.assertNotIn(SECRET,json.dumps(cursor))
        self.assertEqual(self.store.db.execute("SELECT alias FROM sessions").fetchone()[0],"")

    def test_incremental_partial_lines_and_replay_preserve_usage_and_cursor_privacy(self):
        path = self.workbuddy / "projects" / "p" / "s.jsonl"
        path.parent.mkdir(parents=True)
        def record(identity,input_count):
            return json.dumps({"type":"message","sessionId":"session-wb","id":"record-"+identity,
                               "timestamp":int(AT*1000),"content":SECRET,
                               "providerData":{"messageId":identity,"usage":{"inputTokens":input_count,"outputTokens":5}}}).encode()+b"\n"
        first,second = record("request-a",25),record("request-b",35)
        path.write_bytes(first[:-6])
        self.collector.read_file(str(path),"workbuddy",None)
        self.assertIsNone(self.total())
        with path.open("ab") as stream: stream.write(first[-6:]+second[:-6])
        self.collector.read_file(str(path),"workbuddy",None)
        self.assertEqual(self.total(),30)
        self.assertEqual(self.store.file(str(path))["offset"],len(first))
        with path.open("ab") as stream: stream.write(second[-6:])
        self.collector.read_file(str(path),"workbuddy",None)
        self.assertEqual(self.total(),70)
        path.unlink(); path.write_bytes(first+second)
        self.collector.read_file(str(path),"workbuddy",None)
        self.assertEqual(self.total(),70)
        self.assertNotIn(SECRET,self.store.file(str(path))["data"])

    def test_discovery_is_scoped_to_workbuddy_projects_and_rejects_linked_sources(self):
        project = self.workbuddy / "projects" / "p"
        project.mkdir(parents=True)
        valid = project / "s.jsonl"
        valid.write_text('{}\n')
        (self.workbuddy / "settings.jsonl").write_text(SECRET)
        (self.workbuddy / "credentials.jsonl").write_text(SECRET)
        outside = self.root / "outside.jsonl"
        outside.write_text(SECRET)
        (project / "outside.jsonl").symlink_to(outside)
        # Even a root allowed for another adapter is not a WorkBuddy source.
        other_adapter = self.codex / "not-a-session.jsonl"
        other_adapter.write_text(SECRET)
        (project / "another-adapter.jsonl").symlink_to(other_adapter)
        self.collector.discover()
        discovered = {path:source for path,source in self.collector.discovered.items() if source[0]=="workbuddy"}
        self.assertEqual(discovered,{str(valid.resolve()):("workbuddy",None)})

    def test_direct_reads_reject_workbuddy_paths_outside_projects(self):
        doc = json.dumps({"type":"message","sessionId":"do-not-read","id":"outside",
                          "timestamp":int(AT*1000),"content":SECRET,
                          "providerData":{"messageId":"outside","usage":{"inputTokens":10,"outputTokens":2}}})+"\n"
        outside = self.root / "outside.jsonl"
        outside.write_text(doc)
        settings = self.workbuddy / "settings.jsonl"
        settings.write_text(doc)
        project = self.workbuddy / "projects" / "p"
        project.mkdir(parents=True)
        linked = project / "linked.jsonl"
        linked.symlink_to(outside)
        for path in [outside,settings,linked]:
            self.assertEqual(self.collector.read_file(str(path),"workbuddy",None),(0,False))
            self.assertIsNone(self.store.file(str(path)))
        self.assertEqual(self.store.all_sessions(),[])


class StateTests(Fixture):
    def hook(self,event,at=AT,**extra):
        return self.parser.hook({"agentwatch":1,"id":str(event)+str(at),"session_id":"session2","at":at,"event":event,"surface":"terminal",**extra},{})

    def test_context_compaction_invalidates_and_rejects_old_snapshot(self):
        sid = self.hook("Statusline",context_used=5000,context_limit=100000)
        self.hook("PostCompact",AT+10)
        self.store.context(sid,6000,100000,AT+1)
        card = self.store.snapshot({"role":"all"},{},AT+11)["sessions"][0]
        self.assertIsNone(card["context_used"])
        self.assertIsNone(card["context_percent"])
        self.hook("Statusline",AT+12,context_used=1000,context_limit=100000)
        self.assertEqual(self.store.snapshot({"role":"all"},{},AT+13)["sessions"][0]["context_percent"],1)

    def test_online_is_not_executing_and_stale_active_becomes_unknown(self):
        sid = self.hook("UserPromptSubmit")
        self.store.update(sid,{"liveness":"online"})
        snapshot = self.store.snapshot({"role":"all"},{},AT+130)
        self.assertEqual(snapshot["sessions"][0]["state"],"unknown")
        self.assertEqual(snapshot["counts"]["active"],0)
        self.assertEqual(snapshot["counts"]["online"],1)

    def test_async_delayed_permission_does_not_restore_completed_wait(self):
        self.hook("PostToolUse",tool_use_id="tool1")
        self.hook("PermissionRequest",AT+1,tool_use_id="tool1")
        self.assertEqual(self.store.db.execute("SELECT state FROM sessions").fetchone()[0],"active")
        self.hook("Stop",AT+2,prompt_id="prompt1")
        self.hook("UserPromptSubmit",AT+3,prompt_id="prompt1")
        self.assertEqual(self.store.db.execute("SELECT state FROM sessions").fetchone()[0],"idle")

    def test_questions_and_native_tasks_not_codex_turns(self):
        self.hook("PreToolUse",tool_name="AskUserQuestion",tool_use_id="question")
        self.assertEqual(self.store.db.execute("SELECT state FROM sessions").fetchone()[0],"waiting_user")
        self.hook("TaskCreated",AT+1,task_id="task1",subject=SECRET)
        self.hook("TaskCreated",AT+2,task_id="task1")
        self.hook("TaskCompleted",AT+3,task_id="task1")
        result = self.store.stats({"date":"2026-10-15"},AT)
        self.assertEqual(result["tasks"],1)
        self.assertNotIn(SECRET,"\n".join(self.store.db.iterdump()))

    def test_pid_reuse_cannot_mark_session_online(self):
        sid = self.hook("SessionStart",pid=12,process_birth="old")
        inventory = {12:{"tool":"claude","birth":"new","pid":12,"surface":"terminal","tty":"/dev/ttys001"}}
        with patch("agentwatch.collector.process_inventory",return_value=inventory),patch("agentwatch.collector.subprocess.run") as run:
            run.return_value.stdout = ""
            self.collector.probe()
        self.assertEqual(self.store.db.execute("SELECT liveness FROM sessions").fetchone()[0],"offline")


class FileAndBridgeTests(Fixture):
    def test_partial_line_rotation_replay_and_cursor_privacy(self):
        path = self.claude / "projects" / "p" / "s.jsonl"
        path.parent.mkdir(parents=True)
        doc = {"sessionId":"session2","type":"assistant","isSidechain":False,"timestamp":AT,"message":{"id":"msg1","usage":{"input_tokens":10,"output_tokens":20},"content":SECRET}}
        line = json.dumps(doc).encode()
        path.write_bytes(line[:-5])
        self.collector.read_file(str(path),"claude",None)
        self.assertIsNone(self.total())
        with path.open("ab") as f: f.write(line[-5:]+b"\n")
        self.collector.read_file(str(path),"claude",None)
        self.assertEqual(self.total(),30)
        path.unlink(); path.write_bytes(line+b"\n")
        self.collector.read_file(str(path),"claude",None)
        self.assertEqual(self.total(),30)
        self.assertNotIn(SECRET,self.store.file(str(path))["data"])

    def test_bridge_whitelist_and_transport_drained(self):
        payload = {"session_id":"session2","hook_event_name":"PermissionRequest","cwd":str(self.root),"tool_use_id":"tool1","tool_input":SECRET,"prompt":SECRET,"tool_response":SECRET,"env":SECRET}
        clean = sanitize(payload,at=AT)
        self.assertNotIn(SECRET,json.dumps(clean))
        spool = self.data / "claude-events.jsonl"
        append_private(spool,clean)
        self.collector.read_file(str(spool),"hook",None)
        self.assertEqual(spool.stat().st_size,0)
        self.assertEqual(self.store.db.execute("SELECT state FROM sessions").fetchone()[0],"waiting_approval")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0],1)

    def test_registry_deduplicates(self):
        registry = self.data / "transcripts.jsonl"
        doc = {"session_id":"s","path":"/x.jsonl"}
        append_private(registry,doc,unique=True); append_private(registry,doc,unique=True)
        self.assertEqual(len(registry.read_text().splitlines()),1)

    def test_statusline_null_current_usage_is_unknown(self):
        clean = sanitize({"session_id":"s","context_window":{"total_input_tokens":0,"context_window_size":200000,"current_usage":None,"used_percentage":None}},statusline=True,at=AT)
        self.assertFalse(clean["context_valid"])
        self.assertNotIn("context_used",clean)
        sid = self.parser.hook(clean,{})
        self.assertIsNone(self.store.db.execute("SELECT context_used FROM sessions WHERE id=?",(sid,)).fetchone()[0])
        clean = sanitize({"session_id":"s","context_window":{"total_input_tokens":1000,"context_window_size":200000,"current_usage":{"input_tokens":1000}}},statusline=True,at=AT+1)
        self.assertEqual(clean["context_used"],1000)

    def test_hook_script_is_silent_even_malformed_and_keeps_original_statusline(self):
        bridge = Path(__file__).resolve().parents[1] / "agentwatch/bridge.py"
        command = [sys.executable,str(bridge),"--data-dir",str(self.data),"--agentwatch-hook"]
        run = subprocess.run(command,input=b"broken JSON",capture_output=True)
        self.assertEqual((run.returncode,run.stdout,run.stderr),(0,b"",b""))
        (self.data / "claude-installation.json").write_text(json.dumps({"old_statusline":{"command":"printf existing-status"}}))
        run = subprocess.run(command[:-1]+["--agentwatch-statusline"],input=json.dumps({"session_id":"s","context_window":{"total_input_tokens":0,"context_window_size":200000}}).encode(),capture_output=True)
        self.assertEqual(run.stdout,b"existing-status")
        self.assertEqual(run.stderr,b"")

    def test_chrome_external_sessions_are_excluded(self):
        self.parser.codex({"type":"session_meta","payload":{"id":"outside","source":"vscode","originator":"chrome_extension"}}, {})
        self.assertEqual(len(self.store.all_sessions()),0)


class NavigationTests(Fixture):
    def test_workbuddy_navigation_only_opens_application(self):
        sid = self.wb()
        card = self.store.snapshot({"role":"all"},{},AT)["sessions"][0]
        self.assertEqual(card["jump"]["kind"],"workbuddy")
        self.assertEqual(card["jump"]["precision"],"应用")
        commands = []
        def runner(command,**options):
            commands.append(command)
            return SimpleNamespace(returncode=0,stdout="")
        session = dict(self.store.db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone())
        with patch("agentwatch.navigation.sys.platform","darwin"):
            result = jump(session,"workbuddy",runner)
            with self.assertRaises(NavigationError): jump(session,"claude",runner)
            with self.assertRaises(NavigationError): jump(session,"codex",runner)
        self.assertTrue(result["opened"])
        self.assertEqual(commands,[["open","-a","WorkBuddy"]])

    def test_navigation_arguments_and_terminal_never_execute_agent_commands(self):
        commands = []
        def runner(command,**options):
            commands.append(command)
            return SimpleNamespace(returncode=0,stdout="focused")
        session = {"tool":"codex","source_id":"safe-session","liveness":"online","tty":"/dev/ttys123"}
        with patch("agentwatch.navigation.sys.platform","darwin"):
            jump(session,"codex",runner)
            jump(session,"codex_vscode",runner)
            jump(session,"terminal",runner)
        self.assertEqual(commands[0],["open","codex://threads/safe-session"])
        self.assertEqual(commands[1],["open","vscode://openai.chatgpt/local/safe-session"])
        self.assertNotIn("do script",commands[2][-1])
        self.assertIn('/dev/ttys123',commands[2][-1])

    def test_closed_terminal_and_invalid_tty_do_not_open_new_session(self):
        session = {"tool":"claude","source_id":"s","liveness":"online","tty":"/dev/ttys123"}
        with patch("agentwatch.navigation.sys.platform","darwin"):
            with self.assertRaises(NavigationError): jump(session,"terminal",lambda *a,**k:SimpleNamespace(returncode=0,stdout="not-found"))
            session["tty"] = '/dev/ttys123" malicious'
            with self.assertRaises(NavigationError): jump(session,"terminal",lambda *a,**k:self.fail("Unsafe runner called"))

class ConfigurationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = ClaudeConfiguration(self.claude,self.data)

    def test_merge_idempotence_and_restore_preserves_later_changes(self):
        original = {"env":{"TEST":SECRET},"statusLine":{"type":"command","command":"printf original","padding":2},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"prompt","prompt":"leave-existing"}]}]}}
        self.config.settings.write_text(json.dumps(original))
        preview = self.config.preview(True)
        self.assertNotIn(SECRET,json.dumps(preview))
        result = self.config.install(preview["hash"],True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["installed_events"],len(EVENTS))
        merged = json.loads(self.config.settings.read_text())
        self.assertEqual(merged["hooks"]["PreToolUse"][0],original["hooks"]["PreToolUse"][0])
        self.config.install(self.config.preview(True)["hash"],True)
        self.assertEqual(json.loads(self.config.settings.read_text()),merged)
        merged["later_setting"] = 42
        merged["hooks"]["Stop"].append({"hooks":[{"type":"command","command":"printf later"}]})
        self.config.settings.write_text(json.dumps(merged))
        self.config.uninstall()
        restored = json.loads(self.config.settings.read_text())
        self.assertEqual(restored["env"],original["env"])
        self.assertEqual(restored["statusLine"],original["statusLine"])
        self.assertEqual(restored["later_setting"],42)
        self.assertEqual(restored["hooks"]["Stop"],[{"hooks":[{"type":"command","command":"printf later"}]}])
        self.assertFalse(self.config.status()["enabled"])
        backup = Path(self.config.load_manifest()["backup"])
        self.assertEqual(json.loads(backup.read_text()),original)
        self.assertEqual(backup.stat().st_mode & 0o777,0o600)

    def test_changed_since_preview_rejected_and_symlink_not_overwritten(self):
        preview = self.config.preview()
        self.config.settings.write_text('{"later":1}')
        with self.assertRaises(ConfigurationError): self.config.install(preview["hash"])
        self.assertEqual(self.config.settings.read_text(),'{"later":1}')
        self.config.settings.unlink()
        target = self.root / "target.json"; target.write_text("{}")
        self.config.settings.symlink_to(target)
        with self.assertRaises(ConfigurationError): self.config.preview()
        self.assertEqual(target.read_text(),"{}")

    def test_restore_absent_settings_and_preserve_changed_statusline(self):
        self.config.install(self.config.preview(True)["hash"],True)
        self.config.uninstall()
        self.assertFalse(self.config.settings.exists())
        self.config.install(self.config.preview(True)["hash"],True)
        merged = json.loads(self.config.settings.read_text())
        merged["statusLine"] = {"type":"command","command":"printf new-user-line"}
        self.config.settings.write_text(json.dumps(merged))
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text())["statusLine"]["command"],"printf new-user-line")

    def test_malformed_config_remains_unchanged(self):
        self.config.settings.write_text("malformed")
        with self.assertRaises(ConfigurationError): self.config.preview()
        self.assertEqual(self.config.settings.read_text(),"malformed")


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = AppServer(0,self.store,self.collector,ClaudeConfiguration(self.claude,self.data))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        super().tearDown()

    def request(self,path,method="GET",body=None,**headers):
        conn = http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=3)
        conn.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        response = conn.getresponse(); result = (response.status,dict(response.getheaders()),response.read())
        conn.close(); return result

    def test_cookie_csrf_host_and_origin_boundaries(self):
        status,headers,html = self.request("/")
        self.assertEqual(status,200)
        csrf = re.search(b'name="csrf" content="([^"]+)"',html)[1].decode()
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request("/api/snapshot")[0],401)
        self.assertEqual(self.request("/api/subscriptions")[0],401)
        quota_status,_,quota_body = self.request("/api/subscriptions",Cookie=cookie)
        self.assertEqual(quota_status,200)
        self.assertEqual(len(json.loads(quota_body)["platforms"]),3)
        self.assertEqual(self.request("/api/snapshot",Cookie=cookie)[0],200)
        self.assertEqual(self.request("/api/rescan","POST",{},Cookie=cookie)[0],403)
        self.assertEqual(self.request("/api/rescan","POST",{},Cookie=cookie,**{"X-AgentWatch-CSRF":csrf})[0],200)
        self.assertEqual(self.request("/",Host="evil.example")[0],403)
        self.assertEqual(self.request("/api/snapshot",Cookie=cookie,Origin="https://evil.example")[0],403)
        self.assertEqual(self.request("/api/snapshot",Cookie=cookie,**{"Sec-Fetch-Site":"cross-site"})[0],403)
        self.assertEqual(self.request("/../../agentwatch/store.py")[0],404)
        self.assertEqual(self.request("/app.js")[0],200)


if __name__ == "__main__":
    unittest.main()
