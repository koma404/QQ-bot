"""QQ private-chat LLM bot: NapCat (OneBot v11) <-> OpenRouter.

NapCat connects to this process as a WebSocket client. Only whitelisted friends
get replies; a manual message from you pauses the AI for that friend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from collections import deque
from datetime import datetime

from websockets.asyncio.server import serve

import config as config_mod
from config import Config, Friend
from llm import LLM, LLMError
from memory import Memory

log = logging.getLogger("bot")

# settings editable from the UI: key -> (type, min, max)
SETTING_SPEC = {
    "model": (str, None, None),
    "quiet_hours": (str, None, None),
    "takeover_minutes": (float, 0, 1440),
    "debounce_seconds": (float, 0, 60),
    "humanize": (bool, None, None),
    "history_window": (int, 1, 200),
    "max_segments": (int, 1, 10),
    "max_tokens": (int, 50, 4000),
    "temperature": (float, 0, 2),
    "rate_per_user_per_min": (int, 1, 100),
    "rate_global_per_min": (int, 1, 500),
}
INF = float("inf")


def coerce_setting(key: str, value):
    if key not in SETTING_SPEC:
        raise ValueError(f"不支持修改的设置: {key}")
    typ, lo, hi = SETTING_SPEC[key]
    if typ is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{key} 需要是 开/关")
        return value
    if typ is str:
        value = str(value).strip()
        if key == "model" and not value:
            raise ValueError("模型不能为空")
        if key == "quiet_hours" and value:
            try:
                in_quiet_hours(value, datetime.now())
            except ValueError:
                raise ValueError("静默时段格式应为 00:30-08:00，留空表示不启用") from None
        return value
    try:
        num = typ(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 需要是数字") from None
    if not lo <= num <= hi:
        raise ValueError(f"{key} 应在 {lo} 到 {hi} 之间")
    return num

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
        self.typing: set[int] = set()
        self.listeners: set[asyncio.Queue] = set()
        self.nicks: dict[int, str] = memory.contacts()
        self.friend_cache: dict[int, dict] = {}  # NapCat friend list: qq -> {nickname, remark}
        self.friend_cache_at = 0.0
        self.overrides = self._load_overrides()
        self.settings = self._load_settings()

    # ---------- live events for the UI ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.discard(q)

    def publish(self, ev: dict) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def record(self, peer: int, direction: str, source: str, text: str) -> None:
        """Keep a chat line for the UI (the LLM's own history is memory.add)."""
        if not text:
            return
        if source != "manual" and peer not in self.cfg.friends and not self.cfg.log_non_whitelisted:
            return
        self.publish({"type": "message", **self.memory.log_message(peer, direction, source, text)})

    # ---------- settings changed in the UI (stored apart from config.yaml) ----------
    def _load_settings(self) -> dict:
        data = {"enabled": None, "values": {}, "paused": []}
        path = self.cfg.settings_file
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data.update(json.load(f))
            except (OSError, ValueError):
                log.warning("读取 %s 失败，已忽略", path)
        for k, v in list(data["values"].items()):
            try:
                setattr(self.cfg, k, coerce_setting(k, v))
            except ValueError:
                data["values"].pop(k)
        if isinstance(data["enabled"], bool):
            self.enabled = data["enabled"]
        for q in data["paused"]:
            self.pause_until[int(q)] = INF
        return data

    def _save_settings(self) -> None:
        self.settings["paused"] = [q for q, until in self.pause_until.items() if until == INF]
        path = self.cfg.settings_file
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def get_settings(self) -> dict:
        return {k: getattr(self.cfg, k) for k in SETTING_SPEC}

    def update_settings(self, values: dict) -> dict:
        clean = {k: coerce_setting(k, v) for k, v in values.items()}  # all or nothing
        for k, v in clean.items():
            setattr(self.cfg, k, v)
            self.settings["values"][k] = v
        self._save_settings()
        self.publish({"type": "state"})
        return self.get_settings()

    def set_enabled(self, value: bool) -> None:
        self.enabled = value
        self.settings["enabled"] = value
        self._save_settings()
        self.publish({"type": "state"})

    # ---------- whitelist edits made at runtime (stored apart from config.yaml) ----------
    def _load_overrides(self) -> dict:
        data = {"add": {}, "remove": []}
        path = self.cfg.friends_file
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data.update(json.load(f))
            except (OSError, ValueError):
                log.warning("读取 %s 失败，已忽略", path)
        for q, v in data["add"].items():
            self.cfg.friends[int(q)] = Friend(**v)
        for q in data["remove"]:
            self.cfg.friends.pop(int(q), None)
        return data

    def _save_overrides(self) -> None:
        path = self.cfg.friends_file
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.overrides, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def display_name(self, peer: int) -> str:
        f = self.cfg.friends.get(peer)
        c = self.friend_cache.get(peer, {})
        return (f.name if f else "") or c.get("remark") or c.get("nickname") or self.nicks.get(peer) or str(peer)

    def allow_friend(self, peer: int, name: str = "", note: str | None = None) -> Friend:
        old = self.cfg.friends.get(peer)
        friend = Friend(name=name or (old.name if old else "") or self.display_name(peer),
                        note=note if note is not None else (old.note if old else ""))
        self.cfg.friends[peer] = friend
        self.overrides["add"][str(peer)] = {"name": friend.name, "note": friend.note}
        if peer in self.overrides["remove"]:
            self.overrides["remove"].remove(peer)
        self._save_overrides()
        self.publish({"type": "state"})
        return friend

    def deny_friend(self, peer: int) -> bool:
        if peer not in self.cfg.friends:
            return False
        self.cfg.friends.pop(peer)
        self.overrides["add"].pop(str(peer), None)
        if peer not in self.overrides["remove"]:
            self.overrides["remove"].append(peer)
        self._save_overrides()
        self.unanswered.discard(peer)
        self.publish({"type": "state"})
        return True

    # ---------- pause / resume ----------
    def pause_friend(self, peer: int, minutes: float | None = None) -> None:
        """minutes=None: until resumed (survives restarts)."""
        self.pause_until[peer] = INF if minutes is None else self.clock() + minutes * 60
        self._save_settings()
        self.publish({"type": "state"})

    def resume_friend(self, peer: int | None = None) -> tuple[bool, bool]:
        """Resume one friend (or all). -> (was_paused, replying_to_pending)."""
        if peer is None:
            was = bool(self.pause_until)
            self.pause_until.clear()
        else:
            was = self.pause_until.pop(peer, None) is not None
        self._save_settings()
        # catch up on something sent recently while the AI was paused
        pending = (peer is not None and peer in self.unanswered and peer in self.cfg.friends
                   and self.clock() - self.last_in.get(peer, 0) < 600 and peer not in self.workers)
        if pending:
            self.workers[peer] = asyncio.create_task(self._worker(peer))
        self.publish({"type": "state"})
        return was, pending

    # ---------- NapCat friend list (cached) ----------
    async def napcat_friends(self, max_age: float = 60) -> dict[int, dict]:
        if self.clock() - self.friend_cache_at > max_age or not self.friend_cache:
            data = (await self.call_api("get_friend_list", {})).get("data") or []
            self.friend_cache = {int(f["user_id"]): {"nickname": f.get("nickname") or "",
                                                     "remark": f.get("remark") or ""} for f in data}
            self.friend_cache_at = self.clock()
        return self.friend_cache

    async def friend_name(self, qq: int) -> str:
        try:
            c = (await self.napcat_friends()).get(qq)
        except (ConnectionError, asyncio.TimeoutError):
            return ""
        return (c["remark"] or c["nickname"]) if c else ""

    async def cmd_allow(self, key: str, note: str) -> str:
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        name = (self.cfg.friends[peer].name if peer in self.cfg.friends else "") \
            or await self.friend_name(peer) or key
        friend = self.allow_friend(peer, name, note or None)
        return f"已加入白名单: {friend.name}（{peer}），立即生效。可用 ask 试聊"

    async def cmd_deny(self, key: str) -> str:
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        name = self.display_name(peer)
        if not self.deny_friend(peer):
            return f"{peer} 本来就不在白名单里"
        return f"已移出白名单: {name}（{peer}）"

    def cmd_whitelist(self) -> str:
        if not self.cfg.friends:
            return "白名单为空，用 allow <QQ号或名字> 添加"
        return "白名单:\n" + "\n".join(
            f"{q}  {f.name}{'  ' + f.note if f.note else ''}" for q, f in self.cfg.friends.items())

    # ---------- snapshot for the UI ----------
    def snapshot(self) -> dict:
        now = self.clock()
        convs = self.memory.conversations()
        friends = []
        for q in set(self.cfg.friends) | set(convs):
            f = self.cfg.friends.get(q)
            until = self.pause_until.get(q, 0)
            friends.append({
                "qq": q, "name": self.display_name(q), "note": f.note if f else "",
                "whitelisted": f is not None,
                "paused": None if until <= now else ("forever" if until == INF else until),
                "unanswered": q in self.unanswered, "typing": q in self.typing,
                "last": convs.get(q),
            })
        friends.sort(key=lambda x: (x["last"]["ts"] if x["last"] else 0), reverse=True)
        return {
            "now": now, "connected": self.ws is not None, "self_id": self.self_id,
            "enabled": self.enabled, "quiet_hours": self.cfg.quiet_hours,
            "quiet_now": in_quiet_hours(self.cfg.quiet_hours, datetime.now()),
            "model": self.cfg.model, "has_api_key": bool(self.cfg.openrouter_api_key),
            "friends": friends,
        }

    # ---------- transport ----------
    async def handler(self, ws) -> None:
        if self.cfg.onebot_token:
            auth = ws.request.headers.get("Authorization", "")
            if auth.removeprefix("Bearer ").strip() != self.cfg.onebot_token:
                await ws.close(1008, "bad token")
                return
        log.info("NapCat 已连接")
        self.ws = ws
        self.publish({"type": "state"})
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
            self.publish({"type": "state"})

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

    async def send_text(self, peer: int, text: str, source: str = "ai") -> None:
        """source: ai | manual (shown differently in the UI)."""
        self.recent_sent.append((peer, text, self.clock()))
        resp = await self.call_api(
            "send_private_msg",
            {"user_id": peer, "message": [{"type": "text", "data": {"text": text}}]},
        )
        if resp.get("status") != "ok":
            raise ConnectionError(f"QQ 拒绝发送: {resp.get('wording') or resp.get('message') or resp}")
        if peer != self.self_id:
            self.record(peer, "out", source, text)

    async def send_manual(self, peer: int, text: str) -> None:
        """You typing (UI or console): no takeover pause, but the AI sees it as its own line."""
        await self.send_text(peer, text, source="manual")
        if peer in self.cfg.friends:
            self.memory.add(peer, "assistant", text)

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
        if not text:
            return
        self.record(peer, "out", "device", text)  # you, from another device
        if peer not in self.cfg.friends:
            return
        self.memory.add(peer, "assistant", text)
        self.unanswered.discard(peer)
        if self.cfg.takeover_minutes > 0:
            self.pause_until[peer] = now + self.cfg.takeover_minutes * 60
            log.info("你在别处亲自回复了 %s，AI 暂停 %s 分钟（resume 可恢复）", peer, self.cfg.takeover_minutes)
        self.publish({"type": "state"})

    def on_friend_message(self, peer: int, text: str, nick: str = "") -> None:
        if nick and self.nicks.get(peer) != nick:
            self.nicks[peer] = nick
            self.memory.set_contact(peer, nick)
        if text:
            self.record(peer, "in", "friend", text)
        if peer not in self.cfg.friends:
            if peer not in self.ignored_seen:
                self.ignored_seen.add(peer)
                log.info("非白名单好友来消息，AI 不会回复（可在控制面板里加入名单）: %s  QQ=%s", nick, peer)
            return
        if not text:
            return
        log.info("收到 %s: %s", self.display_name(peer), text)
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

    def _history_for(self, peer: int) -> list[dict]:
        if peer in self.cfg.friends:
            return self.memory.recent(peer, self.cfg.history_window)
        return [{"role": "user" if m["direction"] == "in" else "assistant", "content": m["content"]}
                for m in self.memory.chat(peer, self.cfg.history_window)]

    def _system_prompt(self, peer: int) -> str:
        friend = self.cfg.friends.get(peer) or Friend()
        try:
            with open(self.cfg.persona_file, encoding="utf-8") as f:
                persona = f.read().strip()
        except OSError:
            persona = "你是一个普通人，在 QQ 上和朋友闲聊。"
        who = friend.name or self.display_name(peer)
        note = f"\n关于对方：{friend.note}" if friend.note else ""
        return (
            f"{persona}\n\n## 当前对话\n你正在 QQ 上和「{who}」私聊。{note}\n"
            f"现在时间：{datetime.now():%Y-%m-%d %H:%M %A}\n\n"
            "## 输出规则\n"
            "- 纯文本，不要 Markdown，不要加引号或前缀。\n"
            f"- 像真人发消息：短句，一条说一件事；想分多条就换行，最多 {self.cfg.max_segments} 条。\n"
            "- 你看不到图片、语音、表情，对方发 [图片] 之类时按常理自然回应。"
        )

    async def preview(self, peer: int, text: str = "") -> list[str]:
        """Dry run: what the AI would say. Nothing is sent or saved. text = what the friend 'says' (optional)."""
        history = self._history_for(peer)
        if text:
            history.append({"role": "user", "content": text})
        elif not history:
            raise ValueError("还没有聊天记录，请在输入框写一句“对方说的话”再试聊")
        elif history[-1]["role"] == "assistant":
            history.append({"role": "user", "content": "[系统提示：对方暂时没有新消息。请你自然地接着聊；"
                                                        "如果实在没必要开口，只输出一个字：无]"})
        reply = await self.llm.chat(self._system_prompt(peer), history)
        segs = split_reply(reply, self.cfg.max_segments)
        return [] if segs == ["无"] else segs

    async def _reply_once(self, peer: int) -> bool:
        self.unanswered.discard(peer)
        history = self.memory.recent(peer, self.cfg.history_window)
        self.typing.add(peer)
        self.publish({"type": "state"})
        try:
            reply = await self.llm.chat(self._system_prompt(peer), history)
        except LLMError as e:
            log.error("%s", e)
            self.publish({"type": "toast", "level": "error", "text": f"AI 回复失败: {e}"})
            return False
        finally:
            self.typing.discard(peer)
            self.publish({"type": "state"})
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
                log.info("回复 %s: %s", self.display_name(peer), seg)
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

    async def resolve_friend(self, key: str) -> tuple[int | None, str]:
        """QQ number, or a name/remark keyword -> (qq, error)."""
        if key.isdigit():
            return int(key), ""
        k = key.lower()
        hits = [(q, f.name) for q, f in self.cfg.friends.items() if k in f.name.lower()]
        if not hits:
            try:
                data = (await self.call_api("get_friend_list", {})).get("data") or []
            except (ConnectionError, asyncio.TimeoutError) as e:
                return None, f"获取好友列表失败: {e!r}"
            hits = [(f["user_id"], f.get("remark") or f.get("nickname") or "") for f in data
                    if k in f"{f.get('remark') or ''}{f.get('nickname') or ''}".lower()]
        if len(hits) == 1:
            return int(hits[0][0]), ""
        if not hits:
            return None, f"找不到好友「{key}」"
        return None, "匹配到多个，请改用 QQ 号:\n" + "\n".join(f"{q}  {n}" for q, n in hits[:10])

    async def cmd_say(self, key: str, text: str) -> str:
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        try:
            await self.send_manual(peer, text)
        except (ConnectionError, asyncio.TimeoutError) as e:
            return f"发送失败: {e!r}"
        return f"已发送给 {peer}"

    async def cmd_pause(self, key: str, minutes: str) -> str:
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        if peer not in self.cfg.friends:
            return f"{peer} 不在白名单里，AI 本来就不会回他"
        name = self.display_name(peer)
        if minutes:
            try:
                mins = float(minutes)
            except ValueError:
                return "用法: pause <QQ号或名字> [分钟]（不写分钟则一直暂停，直到 resume）"
            self.pause_friend(peer, mins)
            return f"已暂停 {name} 的 AI 回复 {mins:g} 分钟"
        self.pause_friend(peer)
        return f"已暂停 {name} 的 AI 回复，用 resume 恢复"

    async def cmd_resume(self, key: str) -> str:
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        was, pending = self.resume_friend(peer)
        name = self.display_name(peer)
        return (f"已恢复 {name} 的 AI 回复" if was else f"{name} 本来就没有被暂停") \
            + ("；正在回复他刚发来的消息" if pending else "")

    async def cmd_ask(self, key: str, text: str) -> str:
        """Dry run: show what the AI would say to this friend. Nothing is sent or saved."""
        peer, err = await self.resolve_friend(key)
        if peer is None:
            return err
        if peer not in self.cfg.friends:
            return f"{peer} 不在白名单里，先加进白名单"
        try:
            segs = await self.preview(peer, text)
        except (LLMError, ValueError) as e:
            return str(e)
        return "（试聊，未发送）\n" + "\n".join(segs)

    async def run_command(self, cmd: str) -> str:
        parts = cmd.split(maxsplit=2)
        head = parts[0].lower() if parts else "status"
        rest = [p.lower() for p in parts[1:]]
        if head in ("say", "ask"):
            if len(parts) < 3:
                return f"用法: {head} <QQ号或名字> <内容>"
            return await (self.cmd_say if head == "say" else self.cmd_ask)(parts[1], parts[2])
        if head == "pause":
            if len(parts) < 2:
                return "用法: pause <QQ号或名字> [分钟]（不写分钟则一直暂停，直到 resume）"
            return await self.cmd_pause(parts[1], parts[2] if len(parts) > 2 else "")
        if head in ("resume", "start") and len(parts) > 1:
            return await self.cmd_resume(parts[1])
        if head == "allow":
            if len(parts) < 2:
                return "用法: allow <QQ号或名字> [备注，如 室友，说话随意]"
            return await self.cmd_allow(parts[1], parts[2] if len(parts) > 2 else "")
        if head == "deny":
            if len(parts) < 2:
                return "用法: deny <QQ号或名字>"
            return await self.cmd_deny(parts[1])
        if head in ("whitelist", "list"):
            return self.cmd_whitelist()
        if head == "friends":
            return await self.list_friends(rest[0] if rest else "")
        if head == "on":
            self.set_enabled(True)
            return "AI 已开启"
        if head == "off":
            self.set_enabled(False)
            return "AI 已关闭"
        if head in ("resume", "start"):
            self.resume_friend()
            return "已恢复所有人的 AI 回复"
        if head == "clear" and rest and rest[0].isdigit():
            self.memory.clear(int(rest[0]))
            return f"已清空 {rest[0]} 的 AI 记忆（聊天记录仍保留）"
        paused = [str(p) for p in self.pause_until if self._paused(p)]
        return (f"AI {'开启' if self.enabled else '关闭'}；白名单 {len(self.cfg.friends)} 人；"
                f"暂停中: {','.join(paused) or '无'}\n命令: /ai on|off|status|resume|pause <谁> [分钟]|resume [谁]|friends [关键字]|whitelist|allow <谁> [备注]|deny <谁>|say <谁> <话>|ask <谁> <话>|clear <QQ号>")

    async def console(self) -> None:
        """Legacy terminal commands (console_input: true). The control panel replaces these."""
        loop = asyncio.get_running_loop()
        print("控制台命令: on / off / status / pause <谁> [分钟] / resume [谁] / friends [关键字] / whitelist / allow <谁> [备注] / deny <谁> / say <谁> <话> / ask <谁> <话> / clear <QQ号>")
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
        if cfg.ui_enabled:
            import webbrowser

            import web

            token = web.load_or_create_token(cfg)
            logs = web.LogBuffer(bot)
            logging.getLogger().addHandler(logs)
            try:
                await web.start_web(bot, token, logs)
            except OSError as e:
                raise SystemExit(f"控制面板端口 {cfg.ui_port} 无法使用（{e}）。"
                                 "请在 config.yaml 里改 ui_port，或关闭占用它的程序。") from e
            url = f"http://{cfg.ui_host}:{cfg.ui_port}/?token={token}"
            log.info("控制面板: %s", url)
            if cfg.ui_open_browser:
                webbrowser.open(url)
        if cfg.console_input:
            await bot.console()
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
