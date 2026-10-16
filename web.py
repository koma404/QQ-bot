"""Local control panel: a small REST + SSE API on top of Bot, plus the single-page UI in ui/."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from collections import deque
from urllib.parse import urlsplit

from aiohttp import web

from llm import LLMError

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_PERSONA = 20_000


class LogBuffer(logging.Handler):
    """Keeps recent log lines for the UI and streams new ones to it."""

    def __init__(self, bot, size: int = 500):
        super().__init__(level=logging.INFO)
        self.bot, self.lines = bot, deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        line = {"ts": record.created, "level": record.levelname, "text": record.getMessage()}
        self.lines.append(line)
        self.bot.publish({"type": "log", **line})


def load_or_create_token(cfg) -> str:
    if cfg.ui_token:
        return cfg.ui_token
    path = os.path.join(os.path.dirname(cfg.db_path) or ".", "ui_token.txt")
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
            if token:
                return token
    except OSError:
        pass
    token = secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def make_app(bot, token: str, logs: LogBuffer | None = None) -> web.Application:
    strict_host = bot.cfg.ui_host in LOCAL_HOSTS

    @web.middleware
    async def guard(request: web.Request, handler):
        # DNS-rebinding / cross-site protection: this page can speak as your QQ account
        if strict_host and urlsplit("//" + request.host).hostname not in LOCAL_HOSTS:
            return web.json_response({"error": "bad host"}, status=403)
        origin = request.headers.get("Origin")
        if origin and request.method != "GET" and urlsplit(origin).netloc != request.host:
            return web.json_response({"error": "bad origin"}, status=403)
        if request.path.startswith("/api/"):
            given = request.headers.get("X-Token") or request.query.get("token", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except LLMError as e:
            return web.json_response({"error": str(e)}, status=502)
        except (ConnectionError, asyncio.TimeoutError) as e:
            return web.json_response({"error": f"NapCat 没有响应: {e}"}, status=503)

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def qq_of(request: web.Request) -> int:
        try:
            return int(request.match_info["qq"])
        except ValueError:
            raise web.HTTPBadRequest(text="bad qq") from None

    def ok(**extra):
        return web.json_response({"ok": True, **extra})

    # ---------- pages ----------
    async def index(_):
        return web.FileResponse(os.path.join(UI_DIR, "index.html"),
                                headers={"Cache-Control": "no-store"})

    # ---------- read ----------
    async def state(_):
        try:
            await bot.napcat_friends(max_age=300)  # fills in names for the sidebar
        except (ConnectionError, asyncio.TimeoutError):
            pass
        return web.json_response(bot.snapshot())

    async def chat(request):
        qq = qq_of(request)
        limit = min(int(request.query.get("limit", 300)), 1000)
        before = request.query.get("before")
        return web.json_response(
            {"messages": bot.memory.chat(qq, limit, int(before) if before else None)})

    async def napcat_friends(_):
        friends = await bot.napcat_friends(max_age=30)
        rows = [{"qq": q, "nickname": f["nickname"], "remark": f["remark"],
                 "whitelisted": q in bot.cfg.friends} for q, f in friends.items()]
        rows.sort(key=lambda r: (r["remark"] or r["nickname"]).lower())
        return web.json_response({"friends": rows})

    async def get_logs(_):
        return web.json_response({"lines": list(logs.lines) if logs else []})

    # ---------- act ----------
    async def send(request):
        data = await body(request)
        text = str(data.get("text", "")).strip()
        if not text:
            raise ValueError("消息不能为空")
        await bot.send_manual(int(data["qq"]), text)
        return ok()

    async def preview(request):
        data = await body(request)
        segs = await bot.preview(int(data["qq"]), str(data.get("text", "")).strip())
        return web.json_response({"segments": segs})

    async def enabled(request):
        data = await body(request)
        if not isinstance(data.get("enabled"), bool):
            raise ValueError("enabled 需要是 true/false")
        bot.set_enabled(data["enabled"])
        return ok(enabled=bot.enabled)

    async def pause(request):
        data = await body(request)
        minutes = data.get("minutes")
        qq = qq_of(request)
        if qq not in bot.cfg.friends:
            raise ValueError("这位好友不在 AI 名单里，AI 本来就不会回复他")
        bot.pause_friend(qq, float(minutes) if minutes else None)
        return ok()

    async def resume(request):
        was, pending = bot.resume_friend(qq_of(request))
        return ok(was_paused=was, replying=pending)

    async def resume_all(_):
        bot.resume_friend()
        return ok()

    async def allow(request):
        data = await body(request)
        f = bot.allow_friend(qq_of(request), str(data.get("name", "")).strip(),
                             data["note"].strip() if isinstance(data.get("note"), str) else None)
        return ok(name=f.name, note=f.note)

    async def deny(request):
        return ok(removed=bot.deny_friend(qq_of(request)))

    async def clear_memory(request):
        bot.memory.clear(qq_of(request))
        return ok()

    async def get_persona(_):
        try:
            with open(bot.cfg.persona_file, encoding="utf-8") as f:
                return web.json_response({"text": f.read()})
        except OSError:
            return web.json_response({"text": ""})

    async def put_persona(request):
        text = str((await body(request)).get("text", ""))
        if len(text) > MAX_PERSONA:
            raise ValueError(f"人设太长了（上限 {MAX_PERSONA} 字）")
        path = bot.cfg.persona_file
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
        return ok()

    async def get_settings(_):
        return web.json_response({"settings": bot.get_settings()})

    async def put_settings(request):
        return web.json_response({"settings": bot.update_settings(await body(request))})

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = bot.subscribe()
        try:
            await resp.write(b": hello\n\n")
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    await resp.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            bot.unsubscribe(q)
        return resp

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/chat/{qq}", chat),
        web.get("/api/napcat-friends", napcat_friends),
        web.get("/api/logs", get_logs),
        web.get("/api/events", events),
        web.post("/api/send", send),
        web.post("/api/preview", preview),
        web.post("/api/enabled", enabled),
        web.post("/api/resume-all", resume_all),
        web.post("/api/friend/{qq}/pause", pause),
        web.post("/api/friend/{qq}/resume", resume),
        web.post("/api/friend/{qq}/allow", allow),
        web.post("/api/friend/{qq}/deny", deny),
        web.post("/api/friend/{qq}/clear-memory", clear_memory),
        web.get("/api/persona", get_persona),
        web.put("/api/persona", put_persona),
        web.get("/api/settings", get_settings),
        web.put("/api/settings", put_settings),
    ])
    return app


async def start_web(bot, token: str, logs: LogBuffer | None = None) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot, token, logs), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, bot.cfg.ui_host, bot.cfg.ui_port).start()
    return runner
