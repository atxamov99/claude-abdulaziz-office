"""Offline tests only: no Claude process and no Telegram/network requests."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import telemetry
import worker

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=telemetry.TurnStore(Path(self.tmp.name)/"turns.json")
    def tearDown(self): self.tmp.cleanup()
    def test_dedup_and_child_usage(self):
        t=self.store.create("hq",{"task_id":"task-1"})
        m={"message":{"id":"m1","usage":{"input_tokens":2,"cache_read_input_tokens":100,"output_tokens":5},"content":[{"type":"tool_use","id":"tool1","name":"Bash"}]}}
        self.store.message(t,m);self.store.message(t,m)
        m["message"]["usage"]["output_tokens"]=9
        self.store.message(t,m)
        self.store.message(t,{**m,"parent_tool_use_id":"parent"})
        v=self.store.turns[t]
        self.assertEqual((v["usage"]["output_tokens"],v["requests"],v["tools"]),(9,1,1))
        self.assertEqual(v["task_id"],"task-1")
    def test_restart_marks_unfinished_unknown_and_preserves_complete(self):
        a=self.store.create("hq",{});b=self.store.create("hq",{})
        self.store.finish(b,{"session_id":"s"})
        next_store=telemetry.TurnStore(self.store.path)
        next_store.load()
        self.assertEqual(next_store.turns[a]["status"],"interrupted")
        self.assertEqual(next_store.turns[b]["status"],"finished")
    def test_corrupt_file_is_not_overwritten(self):
        self.store.path.write_text("broken")
        self.store.create("hq",{})
        self.assertEqual(self.store.path.read_text(),"broken")
        self.assertTrue(self.store.error)
    def test_failure_is_nonfatal(self):
        saved=telemetry.STORE
        try:
            telemetry.STORE=self.store
            self.store.path=Path(self.tmp.name) # directory cannot be a JSON file
            self.assertIsNotNone(telemetry.record("create","hq",{}))
            self.assertTrue(self.store.error)
        finally: telemetry.STORE=saved

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original=telemetry.STORE
        telemetry.STORE=telemetry.TurnStore(Path(self.tmp.name)/"turns.json")
        class Input:
            def write(self,data): pass
            async def drain(self): pass
        self.w=worker.ClaudeWorker("hq","/tmp")
        self.w.proc=SimpleNamespace(returncode=None,stdin=Input())
        self.w.session_id="session"
        self.w._save_session_id=lambda sid:None
    async def asyncTearDown(self):
        telemetry.STORE=self.original
        self.tmp.cleanup()
    async def finish_turn(self,text):
        await self.w._handle({"type":"user","isReplay":True,"message":{"content":text}})
        await self.w._handle({"type":"assistant","message":{"id":text,"usage":{"output_tokens":7},"content":[{"type":"text","text":"ok"}]}})
        await self.w._handle({"type":"result","session_id":"session","subtype":"success"})
    async def test_queue_foreign_result_and_completed_accounting(self):
        a=asyncio.create_task(self.w.ask([{"type":"text","text":"first"}],worker.TurnEvents()))
        await asyncio.sleep(0)
        b=asyncio.create_task(self.w.ask([{"type":"text","text":"second"}],worker.TurnEvents()))
        await asyncio.sleep(0)
        self.assertEqual(sorted(t["status"] for t in telemetry.STORE.turns.values()),["queued","running"])
        # Do not let the test write the real foreign-result log.
        original=worker._log_foreign;worker._log_foreign=lambda *args:None
        try: await self.w._handle({"type":"result","subtype":"success"})
        finally: worker._log_foreign=original
        self.assertFalse(a.done())
        await self.finish_turn("first");await a
        await asyncio.sleep(0)
        await self.finish_turn("second");await b
        self.assertEqual([t["status"] for t in telemetry.STORE.turns.values()],["finished","finished"])
        self.assertEqual([t["usage"]["output_tokens"] for t in telemetry.STORE.turns.values()],[7,7])
    async def test_queued_cancellation_does_not_touch_active_turn(self):
        a=asyncio.create_task(self.w.ask([{"type":"text","text":"first"}],worker.TurnEvents()))
        await asyncio.sleep(0)
        active=self.w._office_turn
        b=asyncio.create_task(self.w.ask([],worker.TurnEvents()))
        await asyncio.sleep(0);b.cancel()
        with self.assertRaises(asyncio.CancelledError):await b
        self.assertEqual(self.w._office_turn,active)
        await self.finish_turn("first");await a
        self.assertEqual(sorted(t["status"] for t in telemetry.STORE.turns.values()),["failed","finished"])
    async def test_timeout_recorded(self):
        await self.w.ask([],worker.TurnEvents(),timeout=0.001)
        self.assertEqual(next(iter(telemetry.STORE.turns.values()))["status"],"failed")
    async def test_preallocated_telegram_turn_is_not_duplicated(self):
        tid=telemetry.STORE.create("hq",{"source":"telegram","prompt":"first"})
        a=asyncio.create_task(self.w.ask([{"type":"text","text":"first"}],worker.TurnEvents(trace={"turn_id":tid})))
        await asyncio.sleep(0)
        await self.finish_turn("first");await a
        self.assertEqual(len(telemetry.STORE.turns),1)
        self.assertEqual(telemetry.STORE.turns[tid]["source"],"telegram")
    async def test_mixed_debounce_source_and_queue_visible(self):
        import bot
        old_pending,old_delay=bot.PENDING,bot.DEBOUNCE
        bot.PENDING={};bot.DEBOUNCE=60
        try:
            bot.enqueue("hq",[],123,None,"",source="telegram")
            bot.enqueue("hq",[],123,None,"",source="wake")
            self.assertEqual(next(iter(bot.PENDING.values()))["source"],"mixed")
            self.assertEqual(json.loads((await bot.http_office(None)).text)["buffering"][0]["messages"],2)
        finally:
            tasks=[e["task"] for e in bot.PENDING.values()]
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            bot.PENDING=old_pending;bot.DEBOUNCE=old_delay
    async def test_api_includes_waiting_buffering_and_background(self):
        import bot
        old=bot.PENDING
        try:
            bot.PENDING={"test":{"worker":"hq","count":2}}
            telemetry.STORE.create("hq",{})
            telemetry.STORE.task_event("hq",{"task_id":"bg1","type":"system","subtype":"task_started"})
            response=await bot.http_office(None)
            data=json.loads(response.text)
            self.assertEqual(data["waiting"],1)
            self.assertEqual(data["buffering"],[{"project":"hq","messages":2}])
            self.assertEqual(data["background"][0]["id"],"bg1")
        finally:bot.PENDING=old

if __name__=="__main__":unittest.main()
