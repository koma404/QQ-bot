"""Control-panel API tests: real Bot + aiohttp app + fake NapCat over a real WebSocket."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

import web  # noqa: E402
from bot import Bot  # noqa: E402
from config import Config, Friend  # noqa: E402
from memory import Memory  # noqa: E402
from test_flow import FRIEND, SELF, STRANGER, FakeLLM, private  # noqa: E402

TOKEN = "tok"
H = {"X-Token": TOKEN}


class Panel(unittest.IsolatedAsyncioTestCase):
    def make_cfg(self, **kw):
        base = dict(openrouter_api_key="x", persona_file=os.path.join(self.dir, "persona.md"), humanize=False,
                    debounce_seconds=0.1, db_path=":memory:",
                    friends_file=os.path.join(self.dir, "friends.json"),
                    settings_file=os.path.join(self.dir, "settings.json"),
                    friends={FRIEND: Friend("小明", "室友")})
        base.update(kw)
        return Config(**base)

    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "persona.md"), "w", encoding="utf-8") as f:
            f.write("我是测试人设")
        self.llm = FakeLLM()
        self.bot = Bot(self.make_cfg(), self.llm, Memory(":memory:"))
        self.ws_server = await serve(self.bot.handler, "127.0.0.1", 0)
        self.napcat = await connect(f"ws://127.0.0.1:{self.ws_server.sockets[0].getsockname()[1]}")
        self.api: list[dict] = []
        self.reader = asyncio.create_task(self._napcat())
        self.client = TestClient(TestServer(web.make_app(self.bot, TOKEN)))
        await self.client.start_server()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.reader.cancel()
        await self.client.close()
        await self.napcat.close()
        self.ws_server.close()
        self.bot.memory.db.close()

    async def _napcat(self):
        async for raw in self.napcat:
            req = json.loads(raw)
            self.api.append(req)
            data = ([{"user_id": FRIEND, "nickname": "xm", "remark": "小明"},
                     {"user_id": STRANGER, "nickname": "路人", "remark": ""}]
                    if req["action"] == "get_friend_list" else {"message_id": 1})
            await self.napcat.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": req["echo"]}))

    async def emit(self, ev):
        await self.napcat.send(json.dumps(ev))

    async def get(self, path, **kw):
        return await self.client.get(path, headers=H, **kw)

    async def post(self, path, data=None, method="post"):
        return await getattr(self.client, method)(path, json=data or {}, headers=H)

    def sent(self, peer=FRIEND):
        return [r["params"]["message"][0]["data"]["text"] for r in self.api
                if r["action"] == "send_private_msg" and r["params"]["user_id"] == peer]

    # ---------- security ----------
    async def test_auth_and_host_checks(self):
        self.assertEqual((await self.client.get("/api/state")).status, 401)
        self.assertEqual((await self.client.get("/api/state", headers={"X-Token": "wrong"})).status, 401)
        self.assertEqual((await self.client.get("/api/state?token=tok")).status, 200)  # query token (SSE)
        self.assertEqual((await self.get("/api/state")).status, 200)
        self.assertEqual((await self.client.get("/")).status, 200)  # page itself holds no secrets
        self.assertEqual((await self.client.get("/api/state", headers={**H, "Host": "evil.example"})).status, 403)
        r = await self.client.post("/api/enabled", json={"enabled": False},
                                   headers={**H, "Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertTrue(self.bot.enabled)

    # ---------- everything is logged, only whitelisted get answers ----------
    async def test_non_whitelisted_messages_are_visible_but_never_answered(self):
        await self.emit({**private(STRANGER, "你好"), "sender": {"nickname": "路人"}})
        await asyncio.sleep(0.4)
        self.assertEqual(self.llm.calls, [])
        state = await (await self.get("/api/state")).json()
        stranger = next(f for f in state["friends"] if f["qq"] == STRANGER)
        self.assertFalse(stranger["whitelisted"])
        self.assertEqual(stranger["name"], "路人")
        self.assertEqual(stranger["last"]["text"], "你好")
        chat = (await (await self.get(f"/api/chat/{STRANGER}")).json())["messages"]
        self.assertEqual([(m["direction"], m["source"], m["content"]) for m in chat], [("in", "friend", "你好")])

    async def test_logging_non_whitelisted_can_be_turned_off(self):
        self.bot.cfg.log_non_whitelisted = False
        await self.emit(private(STRANGER, "你好"))
        await asyncio.sleep(0.2)
        self.assertEqual(self.bot.memory.chat(STRANGER), [])

    async def test_ai_reply_is_logged_and_streamed_live(self):
        resp = await self.client.get("/api/events?token=tok")
        await self.emit(private(FRIEND, "在吗"))
        seen = []
        async def read():
            while sum(e.get("type") == "message" for e in seen) < 3:
                line = (await resp.content.readline()).decode()
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))
        await asyncio.wait_for(read(), 5)
        msgs = [e for e in seen if e["type"] == "message"]
        self.assertEqual([(m["direction"], m["source"], m["content"]) for m in msgs],
                         [("in", "friend", "在吗"), ("out", "ai", "好的"), ("out", "ai", "收到")])
        self.assertTrue(any(e["type"] == "state" for e in seen))  # typing / state changes
        resp.close()

    # ---------- manual sending ----------
    async def test_send_is_manual_no_pause_no_duplicate_on_echo(self):
        r = await self.post("/api/send", {"qq": FRIEND, "text": "我到了"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.sent(), ["我到了"])
        await self.emit(private(SELF, "我到了", post="message_sent", target=FRIEND))  # NapCat echo
        await asyncio.sleep(0.2)
        chat = (await (await self.get(f"/api/chat/{FRIEND}")).json())["messages"]
        self.assertEqual([(m["source"], m["content"]) for m in chat], [("manual", "我到了")])
        self.assertFalse(self.bot._paused(FRIEND))
        self.assertEqual(self.bot.memory.recent(FRIEND, 5), [{"role": "assistant", "content": "我到了"}])
        self.assertEqual((await self.post("/api/send", {"qq": FRIEND, "text": "  "})).status, 400)

    async def test_message_from_another_device_is_logged_for_anyone(self):
        await self.emit(private(SELF, "手机上发的", post="message_sent", target=STRANGER))
        await asyncio.sleep(0.2)
        chat = self.bot.memory.chat(STRANGER)
        self.assertEqual([(m["direction"], m["source"]) for m in chat], [("out", "device")])
        self.assertFalse(self.bot._paused(STRANGER))  # not whitelisted: nothing to pause

    # ---------- preview ----------
    async def test_preview_works_for_non_whitelisted_using_log_history(self):
        await self.emit(private(STRANGER, "在吗"))
        await asyncio.sleep(0.2)
        r = await self.post("/api/preview", {"qq": STRANGER, "text": ""})
        self.assertEqual((await r.json())["segments"], ["好的", "收到"])
        self.assertEqual(self.llm.calls[-1][1], [{"role": "user", "content": "在吗"}])
        self.assertEqual(self.sent(STRANGER), [])  # dry run
        r = await self.post("/api/preview", {"qq": 999, "text": ""})
        self.assertEqual(r.status, 400)  # no history, no text

    async def test_preview_draft_when_last_message_is_ours_can_decline(self):
        self.bot.memory.add(FRIEND, "assistant", "晚安")
        self.llm.reply = "无"
        r = await self.post("/api/preview", {"qq": FRIEND})
        self.assertEqual((await r.json())["segments"], [])
        self.assertIn("暂时没有新消息", self.llm.calls[-1][1][-1]["content"])

    # ---------- controls + persistence ----------
    async def test_pause_resume_allow_deny(self):
        self.assertEqual((await self.post(f"/api/friend/{STRANGER}/pause", {})).status, 400)  # not whitelisted
        await self.post(f"/api/friend/{FRIEND}/pause", {})
        st = await (await self.get("/api/state")).json()
        self.assertEqual(next(f for f in st["friends"] if f["qq"] == FRIEND)["paused"], "forever")
        await self.post(f"/api/friend/{FRIEND}/resume", {})
        await self.post(f"/api/friend/{STRANGER}/allow", {"name": "路人", "note": "活动认识"})
        self.assertEqual(self.bot.cfg.friends[STRANGER].note, "活动认识")
        await self.post(f"/api/friend/{FRIEND}/deny", {})
        self.assertNotIn(FRIEND, self.bot.cfg.friends)

    async def test_settings_validation_and_everything_survives_restart(self):
        r = await self.post("/api/settings", {"max_segments": 99}, method="put")
        self.assertEqual(r.status, 400)
        self.assertEqual((await self.post("/api/settings", {"quiet_hours": "bad"}, method="put")).status, 400)
        self.assertEqual((await self.post("/api/settings", {"openrouter_api_key": "x"}, method="put")).status, 400)
        r = await self.post("/api/settings", {"max_segments": 2, "quiet_hours": "01:00-02:00", "humanize": True}, method="put")
        self.assertEqual(r.status, 200)
        await self.post("/api/enabled", {"enabled": False})
        await self.post(f"/api/friend/{FRIEND}/pause", {})
        await self.post(f"/api/friend/{STRANGER}/allow", {"name": "路人"})

        again = Bot(self.make_cfg(), self.llm, Memory(":memory:"))  # "restart"
        self.assertEqual(again.cfg.max_segments, 2)
        self.assertEqual(again.cfg.quiet_hours, "01:00-02:00")
        self.assertTrue(again.cfg.humanize)
        self.assertFalse(again.enabled)  # AI stays off after a restart
        self.assertTrue(again._paused(FRIEND))  # indefinite pause stays too
        self.assertIn(STRANGER, again.cfg.friends)
        await self.post("/api/resume-all")
        third = Bot(self.make_cfg(), self.llm, Memory(":memory:"))
        self.assertFalse(third._paused(FRIEND))
        again.memory.db.close()
        third.memory.db.close()

    async def test_persona_roundtrip(self):
        self.assertEqual((await (await self.get("/api/persona")).json())["text"], "我是测试人设")
        self.assertEqual((await self.post("/api/persona", {"text": "新的人设\n第二行"}, method="put")).status, 200)
        self.assertEqual((await (await self.get("/api/persona")).json())["text"], "新的人设\n第二行")
        self.assertIn("新的人设", self.bot._system_prompt(FRIEND))  # used without restart
        self.assertEqual((await self.post("/api/persona", {"text": "x" * 30000}, method="put")).status, 400)

    async def test_napcat_friend_list_marks_whitelisted(self):
        rows = (await (await self.get("/api/napcat-friends")).json())["friends"]
        self.assertEqual({r["qq"]: r["whitelisted"] for r in rows}, {FRIEND: True, STRANGER: False})

    async def test_send_failure_from_qq_is_reported(self):
        async def failing(raw_unused=None):
            pass
        orig = self.bot.call_api
        async def call_api(action, params, timeout=15):
            if action == "send_private_msg":
                return {"status": "failed", "retcode": 1200, "wording": "不是好友"}
            return await orig(action, params, timeout)
        self.bot.call_api = call_api
        r = await self.post("/api/send", {"qq": FRIEND, "text": "hi"})
        self.assertEqual(r.status, 503)
        self.assertIn("不是好友", (await r.json())["error"])
        self.assertEqual(self.bot.memory.chat(FRIEND), [])  # not recorded as sent


class Upgrade(unittest.TestCase):
    def test_old_history_shows_up_in_the_panel_once(self):
        path = os.path.join(tempfile.mkdtemp(), "chat.db")
        old = Memory(path)
        old.db.execute("DELETE FROM log")
        old.add(FRIEND, "user", "在吗")
        old.add(FRIEND, "assistant", "在的")
        old.db.execute("DELETE FROM log")  # as if the log table did not exist yet
        old.db.commit()
        old.db.close()
        new = Memory(path)
        self.assertEqual([(m["direction"], m["source"], m["content"]) for m in new.chat(FRIEND)],
                         [("in", "friend", "在吗"), ("out", "ai", "在的")])
        new.db.close()
        again = Memory(path)  # not imported twice
        self.assertEqual(len(again.chat(FRIEND)), 2)
        again.db.close()


if __name__ == "__main__":
    unittest.main()
