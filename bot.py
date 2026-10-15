"""QQ private-chat LLM bot: NapCat (OneBot v11) <-> OpenRouter.

NapCat connects to this process as a WebSocket client. Only whitelisted friends
get replies; a manual message from you pauses the AI for that friend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import deque
from datetime import datetime

from websockets.asyncio.server import serve

import config as config_mod
from config import Config
from llm import LLM, LLMError
from memory import Memory

log = logging.getLogger("bot")

PLACEHOLDERS = {"image": "[图片]", "record": "[语音]", "face": "[表情]", "mface": "[表情]",
                "video": "[视频]", "file": "[文件]", "forward": "[聊天记录]"}
IGNORED_SEGMENTS = {"reply", "at"}


def extract_text(message) -> str:
    """OneBot message (string or segment array) -> plain text with placeholders."""
    if isinstance(message, str):
        return message.strip()
    parts = []
    for seg in message or []:
        t = seg.get("type")
        if t == "text":
            parts.append(seg.get("data", {}).get("text", ""))
        elif t in IGNORED_SEGMENTS:
            continue
        else:
            parts.append(PLACEHOLDERS.get(t, f"[{t}]"))
    return "".join(parts).strip()


def split_reply(text: str, max_segments: int) -> list[str]:
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) > max_segments:
        lines = lines[: max_segments - 1] + [" ".join(lines[max_segments - 1:])]
    return lines


def in_quiet_hours(spec: str, now: datetime) -> bool:
    if not spec:
        return False
    start_s, end_s = spec.split("-")
    s = datetime.strptime(start_s.strip(), "%H:%M").time()
    e = datetime.strptime(end_s.strip(), "%H:%M").time()
    t = now.time()
    return s <= t < e if s <= e else (t >= s or t < e)


class Bot:
    def __init__(self, cfg: Config, llm: LLM, memory: Memory, clock=time.time):
        self.cfg, self.llm, self.memory, self.clock = cfg, llm, memory, clock
        self.ws = None
        self.self_id: int | None = None
        self.enabled = cfg.start_enabled
        self.pause_until: dict[int, float] = {}
        self.last_in: dict[int, float] = {}
        self.unanswered: set[int] = set()
        self.workers: dict[int, asyncio.Task] = {}
        self.recent_sent: deque[tuple[int, str, float]] = deque(maxlen=50)
        self.user_hits: dict[int, deque] = {}
        self.global_hits: deque = deque()
        self.api_waiters: dict[str, asyncio.Future] = {}
        self.ignored_seen: set[int] = set()

    # ---------- transport ----------
    async def handler(self, ws) -> None:
        if self.cfg.onebot_token:
            auth = ws.request.headers.get("Authorization", "")
            if auth.removeprefix("Bearer ").strip() != self.cfg.onebot_token:
                await ws.close(1008, "bad token")
                return
        log.info("NapCat 已连接")
        self.ws = ws
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                echo = data.get("echo")
                if echo in self.api_waiters:
                    fut = self.api_waiters.pop(echo)
                    if not fut.done():
                        fut.set_result(data)
                elif "post_type" in data:
                    # separate task: replying awaits API responses on this same loop
                    asyncio.create_task(self.on_event(data))
        finally:
            if self.ws is ws:
                self.ws = None
            for fut in self.api_waiters.values():
                if not fut.done():
                    fut.cancel()
            self.api_waiters.clear()
            log.warning("NapCat 连接断开")

    async def call_api(self, action: str, params: dict, timeout: float = 15) -> dict:
        if self.ws is None:
            raise ConnectionError("NapCat 未连接")
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.api_waiters[echo] = fut
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.api_waiters.pop(echo, None)

    async def send_text(self, peer: int, text: str) -> None:
        self.recent_sent.append((peer, text, self.clock()))
        await self.call_api(
            "send_private_msg",
            {"user_id": peer, "message": [{"type": "text", "data": {"text": text}}]},
        )

    # ---------- events ----------
    async def on_event(self, ev: dict) -> None:
        try:
            if ev.get("self_id"):
                self.self_id = int(ev["self_id"])
            if ev.get("post_type") not in ("message", "message_sent"):
                return
            if ev.get("message_type") != "private" or ev.get("sub_type") == "group":
                return  # groups and temp sessions are never touched
            text = extract_text(ev.get("message", ev.get("raw_message", "")))
            sender = int(ev["user_id"])
            is_self = ev["post_type"] == "message_sent" or sender == self.self_id
            peer = int(ev.get("target_id") or sender) if is_self else sender
            if is_self and peer == self.self_id:
                await self.on_command(text)
            elif is_self:
                self.on_own_message(peer, text)
            else:
                self.on_friend_message(peer, text, ev.get("sender", {}).get("nickname", ""))
        except Exception:
            log.exception("处理事件出错")

    def on_own_message(self, peer: int, text: str) -> None:
        now = self.clock()
        for item in list(self.recent_sent):
            if item[0] == peer and item[1] == text and now - item[2] < 120:
                self.recent_sent.remove(item)
                return  # echo of something this bot sent
        if peer not in self.cfg.friends or not text:
            return
        self.memory.add(peer, "assistant", text)
        self.pause_until[peer] = now + self.cfg.takeover_minutes * 60
        self.unanswered.discard(peer)
        log.info("你亲自回复了 %s，AI 暂停 %s 分钟", peer, self.cfg.takeover_minutes)

    def on_friend_message(self, peer: int, text: str, nick: str = "") -> None:
        if peer not in self.cfg.friends:
            if peer not in self.ignored_seen:
                self.ignored_seen.add(peer)
                log.info("非白名单好友来消息，已忽略（要放行就把 QQ 号加进 config.yaml）: %s  QQ=%s", nick, peer)
            return
        if not text:
            return
        log.info("收到 %s: %s", self.cfg.friends[peer].name or peer, text)
        self.memory.add(peer, "user", text)
        self.last_in[peer] = self.clock()
        self.unanswered.add(peer)
        if peer not in self.workers:
            self.workers[peer] = asyncio.create_task(self._worker(peer))

    # ---------- reply pipeline ----------
    def _paused(self, peer: int) -> bool:
        return self.pause_until.get(peer, 0) > self.clock()

    def _rate_ok(self, peer: int) -> bool:
        now = self.clock()
        for q in (self.user_hits.setdefault(peer, deque()), self.global_hits):
            while q and now - q[0] > 60:
                q.popleft()
        return (len(self.user_hits[peer]) < self.cfg.rate_per_user_per_min
                and len(self.global_hits) < self.cfg.rate_global_per_min)

    def _can_reply(self, peer: int) -> bool:
        if not self.enabled or self._paused(peer):
            return False
        if in_quiet_hours(self.cfg.quiet_hours, datetime.now()):
            return False
        if peer not in self.unanswered:
            return False
        if not self._rate_ok(peer):
            log.warning("触发限频，暂不回复 %s", peer)
            return False
        return True

    async def _worker(self, peer: int) -> None:
        try:
            while True:
                while (wait := self.last_in[peer] + self.cfg.debounce_seconds - self.clock()) > 0:
                    await asyncio.sleep(wait)
                if not self._can_reply(peer) or not await self._reply_once(peer):
                    return
                if peer not in self.unanswered:
                    return  # nothing new arrived while replying
        finally:
            self.workers.pop(peer, None)

    def _system_prompt(self, peer: int) -> str:
        friend = self.cfg.friends[peer]
        try:
            with open(self.cfg.persona_file, encoding="utf-8") as f:
                persona = f.read().strip()
        except OSError:
            persona = "你是一个普通人，在 QQ 上和朋友闲聊。"
        who = friend.name or str(peer)
        note = f"\n关于对方：{friend.note}" if friend.note else ""
        return (
            f"{persona}\n\n## 当前对话\n你正在 QQ 上和「{who}」私聊。{note}\n"
            f"现在时间：{datetime.now():%Y-%m-%d %H:%M %A}\n\n"
            "## 输出规则\n"
            "- 纯文本，不要 Markdown，不要加引号或前缀。\n"
            f"- 像真人发消息：短句，一条说一件事；想分多条就换行，最多 {self.cfg.max_segments} 条。\n"
            "- 你看不到图片、语音、表情，对方发 [图片] 之类时按常理自然回应。"
        )

    async def _reply_once(self, peer: int) -> bool:
        self.unanswered.discard(peer)
        history = self.memory.recent(peer, self.cfg.history_window)
        try:
            reply = await self.llm.chat(self._system_prompt(peer), history)
        except LLMError as e:
            log.error("%s", e)
            return False
        segments = split_reply(reply, self.cfg.max_segments)
        if not segments:
            return False
        now = self.clock()
        self.user_hits[peer].append(now)
        self.global_hits.append(now)

        sent: list[str] = []
        try:
            if self.cfg.humanize:
                await asyncio.sleep(random.uniform(1.0, 3.0))
            for i, seg in enumerate(segments):
                if i and self.cfg.humanize:
                    await asyncio.sleep(min(len(seg) * 0.12 + random.uniform(0.3, 1.0), 6))
                if not self.enabled or self._paused(peer):
                    log.info("发送途中被中止（你接管或已关闭）")
                    break
                await self.send_text(peer, seg)
                sent.append(seg)
                log.info("回复 %s: %s", self.cfg.friends[peer].name or peer, seg)
        except (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError) as e:
            log.error("发送失败: %r", e)
        finally:
            if sent:
                self.memory.add(peer, "assistant", "\n".join(sent))
        return bool(sent)

    # ---------- commands (send to your own "我的手机/我的电脑" chat, or type in console) ----------
    async def on_command(self, text: str) -> None:
        if not text.lower().startswith("/ai"):
            return
        out = await self.run_command(text[3:].strip())
        if self.ws is not None and self.self_id:
            await self.send_text(self.self_id, out)

    async def list_friends(self, keyword: str = "") -> str:
        try:
            resp = await self.call_api("get_friend_list", {})
        except (ConnectionError, asyncio.TimeoutError) as e:
            return f"获取好友列表失败: {e!r}"
        rows = []
        for f in resp.get("data") or []:
            label = f"{f.get('remark') or ''} {f.get('nickname') or ''}".strip()
            if keyword in label.lower() or keyword in str(f.get("user_id")):
                rows.append(f"{f.get('user_id')}  {label}")
        shown = rows[:40]
        more = f"\n…还有 {len(rows) - 40} 个，请加关键字缩小范围" if len(rows) > 40 else ""
        return (f"共 {len(rows)} 个匹配:\n" + "\n".join(shown) + more) if rows else "没有匹配的好友"

    async def run_command(self, cmd: str) -> str:
        head, *rest = cmd.lower().split() or ["status"]
        if head == "friends":
            return await self.list_friends(rest[0] if rest else "")
        if head == "on":
            self.enabled = True
            return "AI 已开启"
        if head == "off":
            self.enabled = False
            return "AI 已关闭"
        if head == "resume":
            self.pause_until.clear()
            return "已取消所有让位暂停"
        if head == "clear" and rest and rest[0].isdigit():
            self.memory.clear(int(rest[0]))
            return f"已清空 {rest[0]} 的历史"
        paused = [str(p) for p in self.pause_until if self._paused(p)]
        return (f"AI {'开启' if self.enabled else '关闭'}；白名单 {len(self.cfg.friends)} 人；"
                f"暂停中: {','.join(paused) or '无'}\n命令: /ai on|off|status|resume|friends [关键字]|clear <QQ号>")

    async def console(self) -> None:
        loop = asyncio.get_running_loop()
        print("控制台命令: on / off / status / resume / friends [关键字] / clear <QQ号>")
        while True:
            try:
                line = await loop.run_in_executor(None, input)
            except EOFError:
                return
            if line.strip():
                print(await self.run_command(line.strip()))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_mod.load()
    bot = Bot(cfg, LLM(cfg), Memory(cfg.db_path))
    async with serve(bot.handler, cfg.listen_host, cfg.listen_port):
        log.info("等待 NapCat 连接 ws://%s:%s ；白名单 %d 人；AI %s", cfg.listen_host,
                 cfg.listen_port, len(cfg.friends), "开启" if bot.enabled else "关闭")
        await bot.console()
        await asyncio.Future()  # stdin closed: keep serving


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
