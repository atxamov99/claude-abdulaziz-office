"""ClaudeWorker — один долгоживущий процесс `claude` на проект.

Держит `claude -p --input-format stream-json --output-format stream-json` открытым,
поэтому контекст, MCP-серверы и прогретый кеш живут между сообщениями из Telegram.
Ходы сериализуются: следующий уходит только после `result` предыдущего.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from telemetry import record

HOME = Path.home()
HQ = Path(__file__).resolve().parent
STATE = HQ / "state"
LOGS = HQ / "logs"


@dataclass
class TurnEvents:
    """Колбэки, через которые воркер отдаёт происходящее наружу (в бота)."""
    on_start: Optional[Callable[[], Awaitable[None]]] = None
    on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None
    on_tool: Optional[Callable[[str, str], Awaitable[None]]] = None      # (tool_name, краткий аргумент)
    on_result: Optional[Callable[[str, dict], Awaitable[None]]] = None   # (полный текст, метаданные)
    on_error: Optional[Callable[[str], Awaitable[None]]] = None
    trace: dict = field(default_factory=dict)


@dataclass
class ClaudeWorker:
    name: str
    cwd: str
    model: str = "opus"
    effort: str = "high"
    extra_dirs: list[str] = field(default_factory=list)
    mcp_config: Optional[str] = None
    system_append: Optional[str] = None
    # Агентам клиентов bypass не даём: у них свой режим и список инструментов.
    permission_mode: str = "dontAsk"
    extra_args: list[str] = field(default_factory=list)

    proc: Optional[asyncio.subprocess.Process] = None
    session_id: Optional[str] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reader_task: Optional[asyncio.Task] = None
    _turn_done: Optional[asyncio.Future] = None
    _events: Optional[TurnEvents] = None
    _text_buf: list[str] = field(default_factory=list)
    _started_at: float = 0.0
    _last_activity: float = 0.0
    busy: bool = False
    turns: int = 0
    # Итог последнего хода: is_error/subtype из события result, либо died — процесс
    # умер, не прислав result. Без этого пустой сбойный ход выглядел как «готово».
    last_meta: dict = field(default_factory=dict)
    # Сопоставление result с нашим ходом. 15.09 после перезапуска CLI сначала
    # обработал висевшее уведомление о прерванных фоновых задачах — отдельным
    # ходом со своим result, — и бот принял этот чужой result за ответ на задачу:
    # «завершена, 0 шагов», хотя воркер только начинал работу.
    _mark: str = ""
    _echo_seen: bool = True
    cost: float = 0.0
    _office_turn: Optional[str] = None

    # ---------- жизненный цикл ----------

    def _sessions_file(self) -> Path:
        return STATE / "sessions.json"

    def _load_session_id(self) -> Optional[str]:
        f = self._sessions_file()
        if f.exists():
            try:
                return json.loads(f.read_text()).get(self.name)
            except Exception:
                return None
        return None

    def _save_session_id(self, sid: str) -> None:
        f = self._sessions_file()
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text())
            except Exception:
                data = {}
        data[self.name] = sid
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2))

    def _build_argv(self, resume: Optional[str]) -> list[str]:
        argv = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            # Эхо входящих сообщений: по нему видно, что CLI взялся именно за наш ход.
            "--replay-user-messages",
            "--permission-mode", self.permission_mode,
            "--model", self.model,
            "--effort", self.effort,
            # Пользовательские settings не грузим: там Stop-хук, который на каждом
            # ходе добавляет лишний блокирующий проход и мусорит в поток.
            "--setting-sources", "project,local",
        ]
        if resume:
            argv += ["--resume", resume]
        else:
            argv += ["--session-id", str(uuid.uuid4())]
        for d in self.extra_dirs:
            argv += ["--add-dir", d]
        if self.mcp_config:
            argv += ["--mcp-config", self.mcp_config]
        if self.system_append:
            argv += ["--append-system-prompt", self.system_append]
        # В конце: --allowedTools и подобные вариативны и съели бы следующий аргумент.
        argv += self.extra_args
        return argv

    async def start(self, fresh: bool = False) -> None:
        if self.proc and self.proc.returncode is None:
            return
        resume = None if fresh else self._load_session_id()
        argv = self._build_argv(resume)
        env = dict(os.environ)
        env["CLAUDE_HQ_WORKER"] = self.name
        LOGS.mkdir(parents=True, exist_ok=True)
        self._stderr_log = open(LOGS / f"worker-{self.name}.log", "ab", buffering=0)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.cwd, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr_log,
            )
        except FileNotFoundError:
            raise RuntimeError("не найден бинарь `claude` в PATH")
        self.session_id = resume
        self._started_at = time.time()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.busy = False
        if self._turn_done and not self._turn_done.done():
            self._turn_done.set_result(None)

    async def restart(self, fresh: bool = False) -> None:
        await self.stop()
        await self.start(fresh=fresh)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)

    # ---------- отправка хода ----------

    async def ask(self, blocks: list[dict], events: TurnEvents, timeout: float = 3600) -> str:
        tid = events.trace.get("turn_id") or record("create", self.name, {**events.trace,"prompt":_first_text(blocks)[:500]})
        try:
            return await self._ask_tracked(blocks, events, timeout, tid)
        except BaseException as e:
            record("finish", tid, {}, type(e).__name__)
            raise

    async def _ask_tracked(self, blocks: list[dict], events: TurnEvents, timeout: float, tid) -> str:
        """Отправить сообщение и дождаться конца хода. Возвращает полный текст ответа."""
        async with self._lock:
            if not self.alive():
                await self.start()
            self._office_turn = tid
            record("update", tid, status="running", started=time.time(), session=self.session_id)
            self._events = events
            self._text_buf = []
            self.last_meta = {"died": True}
            self._mark = _first_text(blocks)
            self._echo_seen = not self._mark
            self._turn_done = asyncio.get_running_loop().create_future()
            self.busy = True
            self._last_activity = time.time()

            payload = {"type": "user", "message": {"role": "user", "content": blocks}}
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            try:
                self.proc.stdin.write(line.encode())
                await self.proc.stdin.drain()
            except Exception as e:
                self.busy = False
                self._office_turn = None
                raise RuntimeError(f"воркер {self.name} не принял сообщение: {e}")

            try:
                await asyncio.wait_for(self._turn_done, timeout=timeout)
            except asyncio.TimeoutError:
                self.last_meta = {"timeout": True}
                if events.on_error:
                    await events.on_error(f"таймаут {int(timeout)}с — ход прерван")
            finally:
                record("finish", tid, self.last_meta)
                self._office_turn = None
                self.busy = False
                self._events = None
            return "".join(self._text_buf)

    async def interrupt(self) -> None:
        """Прервать текущий ход. Процесс перезапускается с сохранением контекста."""
        if self.busy:
            await self.restart(fresh=False)

    # ---------- чтение потока ----------

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        buf = b""
        while True:
            try:
                chunk = await self.proc.stdout.read(65536)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                try:
                    await self._handle(msg)
                except Exception:
                    pass
        # процесс умер
        if self._turn_done and not self._turn_done.done():
            if self._events and self._events.on_error:
                await self._events.on_error("процесс claude завершился")
            self._turn_done.set_result(None)
        self.busy = False

    async def _handle(self, msg: dict) -> None:
        t = msg.get("type")
        ev = self._events
        self._last_activity = time.time()
        if t == "system" and msg.get("subtype") in ("task_started", "task_progress", "task_notification"):
            record("task_event", self.name, msg)

        if t == "system" and msg.get("subtype") == "init":
            sid = msg.get("session_id")
            if sid and sid != self.session_id:
                self.session_id = sid
                self._save_session_id(sid)
            if ev and ev.on_start:
                await ev.on_start()

        elif t == "user" and msg.get("isReplay"):
            if self._mark and _first_text((msg.get("message") or {}).get("content")) == self._mark:
                self._echo_seen = True

        elif not self._echo_seen and t in ("stream_event", "assistant", "result"):
            # Ход, который CLI начал сам (уведомление фоновой задачи и т.п.), — не наш.
            if t == "result":
                self.cost += float(msg.get("total_cost_usd") or 0)
                _log_foreign(self.name, msg)

        elif t == "stream_event":
            e = msg.get("event", {})
            if e.get("type") == "content_block_delta":
                d = e.get("delta", {})
                if d.get("type") == "text_delta" and ev and ev.on_text_delta:
                    await ev.on_text_delta(d.get("text", ""))

        elif t == "assistant":
            record("message", self._office_turn, msg)
            for c in msg.get("message", {}).get("content", []):
                if c.get("type") == "text":
                    self._text_buf.append(c.get("text", ""))
                elif c.get("type") == "tool_use" and ev and ev.on_tool:
                    await ev.on_tool(c.get("name", "?"), _brief(c.get("input") or {}))

        elif t == "result":
            self.turns += 1
            self.cost += float(msg.get("total_cost_usd") or 0)
            sid = msg.get("session_id")
            if sid:
                self.session_id = sid
                self._save_session_id(sid)
            text = "".join(self._text_buf).strip()
            if not text:
                text = (msg.get("result") or "").strip()
            meta = {
                "is_error": bool(msg.get("is_error")),
                "subtype": msg.get("subtype"),
                "cost": msg.get("total_cost_usd"),
                "duration_ms": msg.get("duration_ms"),
                "session_id": sid,
                "result": (msg.get("result") or "")[:500] if msg.get("is_error") else None,
            }
            self.last_meta = meta
            if ev and ev.on_result:
                await ev.on_result(text, meta)
            if self._turn_done and not self._turn_done.done():
                self._turn_done.set_result(None)


def _first_text(content: Any) -> str:
    """Первый текстовый блок сообщения — метка, по которой узнаём своё эхо."""
    if isinstance(content, str):
        return content.strip()[:300]
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip():
            return c["text"].strip()[:300]
    return ""


def _log_foreign(name: str, msg: dict) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"worker-{name}.log", "a") as f:
            f.write(f"{time.strftime('%F %T')} пропущен result чужого хода: "
                    f"{msg.get('subtype')} {str(msg.get('result') or '')[:200]!r}\n")
    except Exception:
        pass


def _brief(inp: dict, limit: int = 90) -> str:
    """Короткая подпись к вызову инструмента — то, что видно в Telegram."""
    for key in ("command", "file_path", "pattern", "path", "url", "query", "prompt", "description"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            return v[:limit] + ("…" if len(v) > limit else "")
    try:
        s = json.dumps(inp, ensure_ascii=False)
    except Exception:
        s = str(inp)
    return s[:limit] + ("…" if len(s) > limit else "")
