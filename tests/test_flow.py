"""End-to-end tests with a fake NapCat (real WebSocket) and a fake LLM."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from bot import Bot, extract_text, in_quiet_hours, split_reply  # noqa: E402
from config import Config, Friend  # noqa: E402
from llm import LLMError  # noqa: E402
from memory import Memory  # noqa: E402
from datetime import datetime  # noqa: E402

SELF, FRIEND, STRANGER = 111, 222, 333


class FakeLLM:
    def __init__(self, reply="好的\n收到"):
        self.reply, self.calls, self.fail = reply, [], False

    async def chat(self, system, messages):
        self.calls.append((system, messages))
        if self.fail:
            raise LLMError("boom")
        return self.reply


def private(user, text, post="message", target=None):
    ev = {"post_type": post, "message_type": "private", "sub_type": "friend", "self_id": SELF,
          "user_id": user, "message": [{"type": "text", "data": {"text": text}}]}
    if target:
        ev["target_id"] = target
    return ev


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        self.tmp.write("我是测试人设")
        self.tmp.close()
        self.cfg = Config(openrouter_api_key="x", persona_file=self.tmp.name, humanize=False,
                          debounce_seconds=0.1, db_path=":memory:",
                          friends={FRIEND: Friend("小明", "室友")})
        self.llm = FakeLLM()
        self.bot = Bot(self.cfg, self.llm, Memory(":memory:"))
        self.server = await serve(self.bot.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.client = await connect(f"ws://127.0.0.1:{port}")
        self.api: list[dict] = []
        self.reader = asyncio.create_task(self._read())
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.reader.cancel()
        await self.client.close()
        self.server.close()
        os.unlink(self.tmp.name)

    async def _read(self):
        async for raw in self.client:
            req = json.loads(raw)
            self.api.append(req)
            await self.client.send(json.dumps({"status": "ok", "retcode": 0,
                                               "data": {"message_id": 1}, "echo": req["echo"]}))

    async def emit(self, ev):
        await self.client.send(json.dumps(ev))

    def sent_texts(self, peer=FRIEND):
        return [r["params"]["message"][0]["data"]["text"] for r in self.api
                if r["params"]["user_id"] == peer]

    async def test_whitelisted_friend_gets_split_reply(self):
        await self.emit(private(FRIEND, "在吗"))
        await asyncio.sleep(0.5)
        self.assertEqual(self.sent_texts(), ["好的", "收到"])
        system, msgs = self.llm.calls[0]
        self.assertIn("我是测试人设", system)
        self.assertIn("小明", system)
        self.assertEqual(msgs, [{"role": "user", "content": "在吗"}])
        self.assertEqual(self.bot.memory.last_role(FRIEND), "assistant")

    async def test_stranger_ignored(self):
        await self.emit(private(STRANGER, "hi"))
        await asyncio.sleep(0.4)
        self.assertEqual(self.api, [])
        self.assertEqual(self.llm.calls, [])

    async def test_group_ignored(self):
        ev = private(FRIEND, "hi")
        ev["message_type"] = "group"
        await self.emit(ev)
        await asyncio.sleep(0.4)
        self.assertEqual(self.llm.calls, [])

    async def test_debounce_merges_burst(self):
        for t in ("a", "b", "c"):
            await self.emit(private(FRIEND, t))
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual([m["content"] for m in self.llm.calls[0][1]], ["a", "b", "c"])

    async def test_own_reply_pauses_but_bot_echo_does_not(self):
        # bot's own message echoed back as message_sent must NOT pause
        await self.emit(private(FRIEND, "在吗"))
        await asyncio.sleep(0.5)
        for text in self.sent_texts():
            await self.emit(private(SELF, text, post="message_sent", target=FRIEND))
        await asyncio.sleep(0.1)
        self.assertFalse(self.bot._paused(FRIEND))
        # human typing pauses
        await self.emit(private(SELF, "我自己回", post="message_sent", target=FRIEND))
        await asyncio.sleep(0.1)
        self.assertTrue(self.bot._paused(FRIEND))
        n = len(self.llm.calls)
        await self.emit(private(FRIEND, "再问一句"))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.llm.calls), n)
        self.assertIn({"role": "assistant", "content": "我自己回"},
                      self.bot.memory.recent(FRIEND, 10))

    async def test_commands_via_self_chat(self):
        await self.emit(private(SELF, "/ai off", target=SELF))
        await asyncio.sleep(0.2)
        self.assertFalse(self.bot.enabled)
        self.assertIn("AI 已关闭", self.sent_texts(SELF)[0])
        await self.emit(private(FRIEND, "在吗"))
        await asyncio.sleep(0.4)
        self.assertEqual(self.llm.calls, [])
        await self.emit(private(SELF, "/ai on", post="message_sent", target=SELF))
        await asyncio.sleep(0.2)
        self.assertTrue(self.bot.enabled)

    async def test_rate_limit(self):
        self.cfg.rate_per_user_per_min = 1
        await self.emit(private(FRIEND, "1"))
        await asyncio.sleep(0.5)
        await self.emit(private(FRIEND, "2"))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_llm_failure_does_not_loop_or_send(self):
        self.llm.fail = True
        await self.emit(private(FRIEND, "在吗"))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.api, [])

    async def test_message_arriving_during_reply_gets_answered(self):
        slow = asyncio.Event()
        orig = self.llm.chat

        async def chat(system, messages):
            r = await orig(system, messages)
            if len(self.llm.calls) == 1:
                await slow.wait()
            return r

        self.llm.chat = chat
        await self.emit(private(FRIEND, "第一条"))
        await asyncio.sleep(0.3)
        await self.emit(private(FRIEND, "第二条"))
        await asyncio.sleep(0.1)
        slow.set()
        await asyncio.sleep(0.8)
        self.assertEqual(len(self.llm.calls), 2)


class Helpers(unittest.TestCase):
    def test_extract_text(self):
        msg = [{"type": "reply", "data": {}}, {"type": "text", "data": {"text": "看 "}},
               {"type": "image", "data": {}}]
        self.assertEqual(extract_text(msg), "看 [图片]")

    def test_split_reply(self):
        self.assertEqual(split_reply("a\n\nb\nc\nd\ne", 3), ["a", "b", "c d e"])

    def test_quiet_hours(self):
        self.assertTrue(in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 1, 0)))
        self.assertFalse(in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 12, 0)))
        self.assertTrue(in_quiet_hours("12:00-13:00", datetime(2026, 1, 1, 12, 30)))
        self.assertFalse(in_quiet_hours("", datetime(2026, 1, 1, 12, 30)))


if __name__ == "__main__":
    unittest.main()
