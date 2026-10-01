"""TerminalWorker — то же, что ClaudeWorker, но вместо `claude -p` (headless
подпроцесс со stream-json) держит РЕАЛЬНУЮ интерактивную `claude` TUI внутри
tmux-сессии, видимую в открытом окне Terminal.app.

Интерфейс намеренно идентичен ClaudeWorker (worker.py): start/stop/restart/
alive/ask/interrupt, те же атрибуты. bot.py создаёт нужный класс по флагу
HQ_WORKER, остальной код бота не меняется.

Зонд (2026-09-22, до первой строчки кода) показал:
  - Свежая tmux-сессия сначала печатает диалог "Do you trust this folder?"
    (по умолчанию выделено "No, exit") — start() должен сам нажать Down+Enter.
  - Конец хода виден по строке "✻ <verb> for Ns · done H:MM" (verb —
    случайное слово вроде Cooked/Thought, не фиксировано) — это надёжнее
    чистого тайм-аута простоя, но обе проверки используются вместе как
    подстраховка.

Известное ограничение против -p: тут нет чистого JSON-потока событий, ответ
достаётся скрейпингом экрана (tmux capture-pane). on_tool() не эмитится (со
скриншота сложно надёжно вычленить вызовы инструментов) — прогресс уходит
только через on_text_delta по мере того как виден новый хвост ответа.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from worker import TurnEvents  # тот же контракт колбэков, что у ClaudeWorker

HOME = Path.home()
HQ = Path(__file__).resolve().parent
STATE = HQ / "state"
LOGS = HQ / "logs"

_DONE_RE = re.compile(r"✻ .+ for \d+s? · done \d{1,2}:\d{2}")
_TRUST_PROMPT_RE = re.compile(r"trust this folder", re.IGNORECASE)
_PROMPT_LINE_RE = re.compile(r"^❯\s*$", re.MULTILINE)


def _sh(cmd: list[str]) -> tuple[int, str, str]:
    """Синхронный subprocess.run для коротких tmux/osascript-команд."""
    import subprocess
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


@dataclass
class TerminalWorker:
    name: str
    cwd: str
    model: str = "opus"
    effort: str = "high"
    extra_dirs: list[str] = field(default_factory=list)
    mcp_config: Optional[str] = None
    system_append: Optional[str] = None
    # У TerminalWorker разрешений в TUI спрашивать некому (никто не сидит и не
    # жмёт "разрешить" на каждый вызов), поэтому здесь всегда --dangerously-skip-permissions,
    # это поле только для интерфейсной совместимости с ClaudeWorker/Manager.get().
    permission_mode: str = "bypassPermissions"
    extra_args: list[str] = field(default_factory=list)

    session_id: Optional[str] = None
    busy: bool = False
    turns: int = 0
    last_meta: dict = field(default_factory=dict)
    cost: float = 0.0
    _started_at: float = 0.0
    _last_activity: float = 0.0
    # Не используется TerminalWorker'ом (нет отдельного office-хода со своим
    # id), но bot.py:468 (/projects) читает это поле у любого воркера в
    # MGR.workers для статус-панели Office — без него запрос падает 500-й.
    _office_turn: Optional[str] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------- имя tmux-сессии ----------

    @property
    def _session(self) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", self.name)
        return f"hq-{safe}"

    # ---------- сохранение session_id (тот же файл, что у ClaudeWorker) ----------

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

    def _discover_session_id(self) -> Optional[str]:
        """Сессия не печатает свой UUID в TUI, поэтому берём самый свежий
        .jsonl в ~/.claude/projects/<этот-cwd>/ — та же идея, что уже
        используется в office/src-tauri/src/hq.rs для usage-статистики."""
        encoded = re.sub(r"[/ ]", "-", self.cwd)
        pattern = str(HOME / ".claude" / "projects" / encoded / "*.jsonl")
        files = glob.glob(pattern)
        if not files:
            return None
        newest = max(files, key=os.path.getmtime)
        return Path(newest).stem

    # ---------- жизненный цикл ----------

    def _build_launch_cmd(self, resume: Optional[str]) -> str:
        parts = ["claude", "--dangerously-skip-permissions",
                 "--model", self.model, "--effort", self.effort,
                 "--setting-sources", "project,local"]
        if resume:
            parts += ["--resume", resume]
        else:
            parts += ["--session-id", str(uuid.uuid4())]
        for d in self.extra_dirs:
            parts += ["--add-dir", d]
        if self.mcp_config:
            parts += ["--mcp-config", self.mcp_config]
        if self.system_append:
            parts += ["--append-system-prompt", self.system_append]
        parts += self.extra_args
        return shlex.join(parts)

    async def start(self, fresh: bool = False) -> None:
        if self.alive():
            return
        resume = None if fresh else self._load_session_id()
        cmd = self._build_launch_cmd(resume)
        LOGS.mkdir(parents=True, exist_ok=True)
        rc, out, err = await asyncio.to_thread(
            _sh, ["tmux", "new-session", "-d", "-s", self._session, "-c", self.cwd, cmd])
        if rc != 0:
            raise RuntimeError(f"tmux new-session провалился: {err.strip() or out.strip()}")
        self._started_at = time.time()
        self._last_activity = time.time()

        # Открыть видимое окно Terminal.app, привязанное к этой tmux-сессии
        # (QADAM 3). Абдулазиз должен видеть и мочь печатать сам.
        #
        # У Office теперь есть вкладка "Terminal" (office/src/components/TerminalView.tsx),
        # которая подключается к ЭТОЙ ЖЕ сессии через настоящий PTY, так что отдельное
        # окно бывает лишним: два клиента tmux делят размер по меньшему из них.
        # HQ_TERMINAL_WINDOW=0 выключает окно; по умолчанию поведение прежнее.
        if os.environ.get("HQ_TERMINAL_WINDOW", "1") != "0":
            script = f'tell application "Terminal" to do script "tmux attach -t {self._session}"'
            await asyncio.to_thread(_sh, ["osascript", "-e", script])

        # Свежая сессия почти всегда сначала спрашивает "Do you trust this
        # folder?" (зонд 2026-09-22) — по умолчанию выделено "No, exit".
        await asyncio.sleep(2.5)
        pane = await self._capture()
        if _TRUST_PROMPT_RE.search(pane):
            await self._send_keys(["Down"])
            await asyncio.sleep(0.3)
            await self._send_keys(["Enter"])
            await asyncio.sleep(2.5)

        self.session_id = resume

    async def stop(self) -> None:
        await asyncio.to_thread(_sh, ["tmux", "kill-session", "-t", self._session])
        self.busy = False

    async def restart(self, fresh: bool = False) -> None:
        await self.stop()
        await self.start(fresh=fresh)

    def alive(self) -> bool:
        rc, _, _ = _sh(["tmux", "has-session", "-t", self._session])
        return rc == 0

    # ---------- tmux ввод/вывод ----------

    async def _capture(self) -> str:
        rc, out, _ = await asyncio.to_thread(
            _sh, ["tmux", "capture-pane", "-p", "-t", self._session, "-S", "-200"])
        return out if rc == 0 else ""

    async def _send_keys(self, keys: list[str]) -> None:
        await asyncio.to_thread(_sh, ["tmux", "send-keys", "-t", self._session, *keys])

    async def _send_text(self, text: str) -> None:
        """Длинный/многострочный текст — через load-buffer+paste-buffer, а не
        send-keys -l посимвольно (см. QADAM 1 в задаче: перевод строки в
        send-keys может отправить сообщение раньше времени, а символы вида
        `;`/`$` могут быть прочитаны tmux как часть команды)."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp_path = f.name
        try:
            await asyncio.to_thread(_sh, ["tmux", "load-buffer", "-b", "hq-msg", tmp_path])
            await asyncio.to_thread(_sh, ["tmux", "paste-buffer", "-b", "hq-msg", "-t", self._session])
        finally:
            os.unlink(tmp_path)
        await asyncio.sleep(0.2)
        await self._send_keys(["Enter"])

    @staticmethod
    def _extract_reply(pane: str, sent_text: str) -> str:
        """Находит СВЕЖЕЕ эхо отправленного сообщения (ищем с конца экрана,
        чтобы не зацепить такой же текст из прошлого хода) и берёт всё после
        него до маркера "✻ ... done"/статус-бара.

        Диффом "что было/стало" тут не пользуемся — на втором ходе экран
        перерисовывается не построчно-стабильно (баннер и т.п. сдвигаются),
        так что сравнение by-index ловит весь экран, а не только ответ.
        Якорь на содержимое, которое мы сами только что отправили, устойчив
        к этому."""
        anchor = sent_text.strip().splitlines()[0][:60] if sent_text.strip() else ""
        lines = pane.splitlines()
        start_idx = None
        if anchor:
            for idx in range(len(lines) - 1, -1, -1):
                if anchor in lines[idx]:
                    start_idx = idx
                    break
        if start_idx is None:
            return ""
        cleaned: list[str] = []
        for line in lines[start_idx + 1:]:
            if _DONE_RE.search(line):
                break
            if line.strip().startswith("tmux detected"):
                break
            if re.match(r"^\s*\[Sonnet|^\s*⏵⏵ bypass", line):
                break
            cleaned.append(re.sub(r"^⏺\s?", "", line))
        return "\n".join(cleaned).strip()

    # ---------- отправка хода ----------

    async def ask(self, blocks: list[dict], events: TurnEvents, timeout: float = 3600) -> str:
        text = _first_text(blocks)
        async with self._lock:
            if not self.alive():
                await self.start()
            self.busy = True
            self.last_meta = {"died": True}
            self._last_activity = time.time()
            if events.on_start:
                await events.on_start()

            await self._send_text(text)

            deadline = time.time() + timeout
            stable_since: Optional[float] = None
            last_seen = ""
            reply = ""
            try:
                while time.time() < deadline:
                    await asyncio.sleep(0.7)
                    pane_now = await self._capture()
                    self._last_activity = time.time()
                    if pane_now != last_seen:
                        partial = self._extract_reply(pane_now, text)
                        if partial and partial != reply and events.on_text_delta:
                            delta = partial[len(reply):] if partial.startswith(reply) else partial
                            if delta:
                                await events.on_text_delta(delta)
                        reply = partial or reply
                        last_seen = pane_now
                        stable_since = None
                    else:
                        stable_since = stable_since or time.time()

                    done_marker = _DONE_RE.search(pane_now) is not None
                    prompt_idle = bool(_PROMPT_LINE_RE.search(pane_now.splitlines()[-1] if pane_now.splitlines() else ""))
                    screen_stable = stable_since is not None and (time.time() - stable_since) > 4
                    if done_marker and (prompt_idle or screen_stable):
                        reply = self._extract_reply(pane_now, text) or reply
                        break
                    if screen_stable and (time.time() - stable_since) > 20:
                        # Подстраховка: маркер "done" мог не попасть в капчур
                        # (проскочил между двумя опросами), но экран давно не
                        # меняется — считаем ход законченным.
                        reply = self._extract_reply(pane_now, text) or reply
                        break
                else:
                    self.last_meta = {"timeout": True}
                    if events.on_error:
                        await events.on_error(f"таймаут {int(timeout)}с — ход прерван")
                    return reply
            finally:
                self.busy = False

            self.turns += 1
            sid = self._discover_session_id()
            if sid:
                self.session_id = sid
                self._save_session_id(sid)
            self.last_meta = {"is_error": False, "session_id": sid, "died": False}
            if events.on_result:
                await events.on_result(reply, self.last_meta)
            return reply

    async def interrupt(self) -> None:
        """В TUI это не убийство процесса, а просто Esc — прерывает генерацию
        на месте, контекст остаётся (в отличие от restart() у ClaudeWorker,
        здесь даже перезапуск процесса не требуется)."""
        if self.busy:
            await self._send_keys(["Escape"])


def _first_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip():
            return c["text"].strip()
    return ""
