"""Агенты клиентов: свой бот в группе клиента ↔ урезанный воркер ↔ главный.

Клиент пишет в группу → бот клиента (отдельный токен) → воркер без Bash и без
bypass, пишет только в свою папку → ответ уходит в группу тем же ботом.

Контроль у главного и владельца:
- копия каждого сообщения группы и каждого ответа — в тему форума hq;
- эта же тема привязана к воркеру: написал туда — говоришь с агентом напрямую;
- hq_run("example-client", …) — задача агенту из главного;
- /clients pause — агент молчит, переписка по-прежнему копируется.

Бот не отвечает нигде, кроме групп из <PREFIX>_GROUP_IDS: добавили в чужую
группу — владельцу приходит уведомление, агент не запускается.
"""
from __future__ import annotations
import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional
from telethon import TelegramClient, events
from telethon.tl.functions.messages import CreateForumTopicRequest
from telethon.tl.types import UpdateNewChannelMessage
from worker import ClaudeWorker, TurnEvents
H = None
MONO = None
SPECS = {}
SILENT = '[молчу]'
DEBOUNCE = 6.0
TURN_TIMEOUT = 900.0
MCP_TOOLS = ['mcp__hq__tg_send_message', 'mcp__hq__tg_send_photo', 'mcp__hq__tg_send_document', 'mcp__hq__hq_notify']
SYSTEM = 'Ты клиентский агент. Следуй CLAUDE.md своей рабочей папки. Не обещай сроки и цены; для решения команды используй hq_notify. Если сообщение не к тебе, ответь [молчу].'

def ids(value: Optional[str]) -> set[int]:
    return {int(x) for x in re.findall('-?\\d+', value or '')}

def is_silent(text: Optional[str]) -> bool:
    t = (text or '').strip()
    return not t or SILENT in t
SERVICE_TEXT_RE = re.compile('hit your (session|usage|weekly) limit|rate limit|usage limit|credit balance|API Error|overloaded_error|Request timed out|invalid_request_error|Prompt is too long|authentication_error|Please run /login', re.I)

def is_service_failure(text: Optional[str], meta: Optional[dict]=None) -> bool:
    meta = meta or {}
    if meta.get('is_error') or meta.get('died') or meta.get('timeout'):
        return True
    return bool(SERVICE_TEXT_RE.search(text or ''))

def is_client(name: Optional[str]) -> bool:
    return bool(name) and name in SPECS

def set_env(key: str, value: str) -> None:
    f = H.HQ / '.env'
    lines = f.read_text().splitlines() if f.exists() else []
    lines = [l for l in lines if not l.startswith(key + '=')]
    lines.append(f'{key}={value}')
    f.write_text('\n'.join(lines) + '\n')
    H.CFG[key] = value

def make_worker(name: str, model: str, effort: str) -> ClaudeWorker:
    spec = SPECS[name]
    d = H.HQ / spec['dir']
    allowed = ['Read', 'Grep', 'Glob', 'Edit(./**)', *MCP_TOOLS]
    return ClaudeWorker(name=name, cwd=str(d), model=model, effort=effort, extra_dirs=[str(p) for p in spec['read_dirs'] if p.exists()], mcp_config=str(d / 'mcp.json'), system_append=SYSTEM, permission_mode='dontAsk', extra_args=['--restricted', '--tools', 'Read,Grep,Glob,Edit,Write', '--allowedTools', ','.join(allowed), '--disallowedTools', 'Read(**/.env*)', '--strict-mcp-config'])
AUDIO_EXT = {'.ogg', '.oga', '.opus', '.m4a', '.mp3', '.wav', '.aac', '.flac'}
VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v'}
TEXT_EXT = {'.txt', '.md', '.csv', '.json', '.log', '.xml', '.html'}
DOC_EXT = {'.docx', '.doc', '.rtf', '.odt', '.pages'}
SHEET_EXT = {'.xlsx', '.xlsm', '.xls'}
INLINE_LIMIT = 6000
_WHISPER = None

def _run(*cmd, timeout=300) -> bool:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode == 0
    except Exception:
        return False

def transcribe(path: Path) -> tuple[str, str]:
    """Речь → текст. Второе значение — пометка, если язык распознан неуверенно."""
    global _WHISPER
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return ('', ' (распознавание не установлено)')
    if _WHISPER is None:
        _WHISPER = WhisperModel(H.WHISPER_MODEL, device='cpu', compute_type='int8')
    segs, info = _WHISPER.transcribe(str(path), beam_size=1, vad_filter=True)
    text = ' '.join((x.text.strip() for x in segs)).strip()
    lang, prob = (info.language, info.language_probability or 0)
    note = '' if lang in ('ru', 'uz', 'en') and prob >= 0.6 else f' (распознано неуверенно: {lang} {prob:.0%}; возможно узбекский — при сомнении переспроси)'
    return (text, note)

def _small_image(path: Path) -> Path:
    """Картинки больше 4.5 МБ и HEIC модель не примет — ужимаем в jpg."""
    if path.suffix.lower() in H.IMAGE_EXT and path.stat().st_size < 4500000:
        return path
    out = path.with_suffix('.view.jpg')
    if _run('sips', '-s', 'format', 'jpeg', '-Z', '2000', str(path), '--out', str(out)) and out.exists():
        return out
    return path

def digest_media(path: Path, kind: str) -> tuple[str, list[Path]]:
    """Вложение → (текст для агента, картинки для контекста)."""
    ext = path.suffix.lower()
    try:
        if kind == 'photo' or ext in H.IMAGE_EXT or ext in ('.heic', '.heif'):
            img = _small_image(path)
            return (f'[картинка: {path}]', [img] if img.suffix.lower() in H.IMAGE_EXT else [])
        if kind in ('voice', 'audio', 'video', 'video_note') or ext in AUDIO_EXT | VIDEO_EXT:
            label = {'voice': 'голосовое', 'video_note': 'кружок', 'video': 'видео'}.get(kind, 'аудио')
            wav = path.with_suffix('.16k.wav')
            if not _run('ffmpeg', '-y', '-i', str(path), '-vn', '-ac', '1', '-ar', '16000', str(wav)):
                return (f'[{label}: {path} — не смог извлечь звук]', [])
            text, note = transcribe(wav)
            wav.unlink(missing_ok=True)
            frames: list[Path] = []
            if kind in ('video', 'video_note') or ext in VIDEO_EXT:
                for i, t in enumerate(('00:00:01', '00:00:05')):
                    fr = path.with_suffix(f'.frame{i}.jpg')
                    if _run('ffmpeg', '-y', '-ss', t, '-i', str(path), '-frames:v', '1', '-vf', 'scale=720:-2', str(fr)) and fr.exists():
                        frames.append(fr)
            body = text or '(речи не слышно)'
            return (f'[{label}, расшифровка{note}]: {body}', frames)
        if ext == '.pdf':
            return (f'[PDF: {path} — открой через Read]', [])
        if ext in DOC_EXT:
            txt = path.with_suffix('.txt')
            if _run('textutil', '-convert', 'txt', '-output', str(txt), str(path)) and txt.exists():
                return (_inline(path, txt.read_text(errors='replace')), [])
            return (f'[документ: {path} — не смог прочитать]', [])
        if ext in SHEET_EXT:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets[:10]:
                rows = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 300:
                        rows.append('…')
                        break
                    if any((c is not None for c in row)):
                        rows.append(' | '.join(('' if c is None else str(c) for c in row)))
                parts.append(f'## лист «{ws.title}»\n' + '\n'.join(rows))
            txt = path.with_suffix('.txt')
            txt.write_text('\n\n'.join(parts))
            return (_inline(path, txt.read_text()), [])
        if ext in TEXT_EXT:
            return (_inline(path, path.read_text(errors='replace')), [])
    except Exception as e:
        return (f'[файл: {path} — ошибка чтения: {type(e).__name__}: {e}]', [])
    return (f"[файл: {path} — формат {ext or '?'} не разбираю; попроси прислать скриншотом или PDF]", [])

def _inline(path: Path, text: str) -> str:
    text = text.strip()
    if len(text) <= INLINE_LIMIT:
        return f'[файл {path.name}, текст]:\n{text}'
    full = path.with_suffix(path.suffix + '.full.txt')
    full.write_text(text)
    return f'[файл {path.name}, начало текста; целиком — {full}]:\n{text[:INLINE_LIMIT]}…'

def media_kind(msg) -> str:
    for k in ('voice', 'video_note', 'audio', 'video', 'photo'):
        if getattr(msg, k, None):
            return k
    return 'document'

def display(sender) -> str:
    if sender is None:
        return '?'
    name = ' '.join((x for x in (getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)) if x))
    name = name or getattr(sender, 'title', None) or '?'
    user = getattr(sender, 'username', None)
    return f'{name} (@{user})' if user else name

class Agent:

    def __init__(self, name: str, spec: dict):
        self.name, self.spec = (name, spec)
        self.dir: Path = H.HQ / spec['dir']
        p = spec['prefix']
        self.k_token, self.k_groups, self.k_team, self.k_mirror, self.k_paused = (f'{p}_{s}' for s in ('BOT_TOKEN', 'GROUP_IDS', 'TEAM_IDS', 'MIRROR', 'PAUSED'))
        self.client: Optional[TelegramClient] = None
        self.me = None
        self.pending: list[dict] = []
        self.pending_task: Optional[asyncio.Task] = None
        self.last_msg: dict[int, int] = {}
        self.active_chat: Optional[int] = None
        self.announced: set[int] = set()

    @property
    def groups(self) -> set[int]:
        return ids(H.CFG.get(self.k_groups))

    @property
    def team(self) -> set[int]:
        return ids(H.CFG.get(self.k_team)) | set(H.OWNERS)

    @property
    def paused(self) -> bool:
        return H.CFG.get(self.k_paused) == '1'

    def mirror_target(self) -> tuple[Optional[int], Optional[int]]:
        m = re.fullmatch('(-?\\d+):(\\d+)?', H.CFG.get(self.k_mirror) or '')
        if m:
            return (int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        return H.target_for('hq')

    async def start(self) -> bool:
        token = H.CFG.get(self.k_token)
        if not token:
            return False
        self.client = TelegramClient(str(H.STATE / f'{self.name}-bot'), H.API_ID, H.API_HASH, connection_retries=None, retry_delay=5, catch_up=True)
        await self.client.start(bot_token=token)
        self.me = await self.client.get_me()
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_action, events.ChatAction())
        if H.MGR.reg['projects'].get(self.name) != str(self.dir):
            H.MGR.reg['projects'][self.name] = str(self.dir)
            H.save_projects(H.MGR.reg)
        await self.ensure_mirror()
        H.log(f"агент клиентов {self.name}: @{self.me.username} · группы {sorted(self.groups) or 'нет'}{(' · ПАУЗА' if self.paused else '')}")
        return True

    async def ensure_mirror(self) -> None:
        """Тема в форуме hq: сюда копия переписки, и она же — прямой канал к агенту."""
        if H.CFG.get(self.k_mirror):
            return
        forum = H.CFG.get('HQ_FORUM_CHAT')
        if not forum:
            return
        topics = H.MGR.reg.setdefault('topics', {})
        tid = next((int(t) for t, n in topics.items() if n == self.name), None)
        if not tid:
            try:
                peer = await H.BOT.get_entity(int(forum))
                r = await H.BOT(CreateForumTopicRequest(peer=peer, title=self.spec['title'], icon_color=9367192, random_id=int.from_bytes(os.urandom(7), 'big')))
                tid = next((u.message.id for u in getattr(r, 'updates', []) if isinstance(u, UpdateNewChannelMessage)), None)
            except Exception as e:
                H.log(f'  ⨯ {self.name}: тему не создал ({type(e).__name__}: {e}) — копия в личку')
                return
            if not tid:
                return
            topics[str(tid)] = self.name
            H.save_projects(H.MGR.reg)
        set_env(self.k_mirror, f'{forum}:{tid}')

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def mirror(self, text: str) -> None:
        chat, topic = self.mirror_target()
        if not chat:
            return
        try:
            for part in H.chunks(text):
                await H.safe_send(chat, part, reply_to=topic)
        except Exception as e:
            H.log(f'  ⨯ {self.name}: копия не ушла: {e}')

    def log_chat(self, line: str) -> None:
        f = self.dir / 'chat-log' / f"{time.strftime('%Y-%m-%d')}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open('a') as fh:
            fh.write(f"- {time.strftime('%H:%M')} {line}\n")

    async def on_action(self, event) -> None:
        try:
            if not (event.user_added or event.user_joined) or not self.me:
                return
            if self.me.id not in (event.user_ids or []):
                return
            chat = await event.get_chat()
            await self.announce(event.chat_id, getattr(chat, 'title', '?'))
        except Exception as e:
            H.log(f'  ⨯ {self.name}.on_action: {type(e).__name__}: {e}')

    async def announce(self, chat_id: int, title: str) -> None:
        if chat_id in self.groups:
            await self.mirror(f'ℹ️ бот снова в группе «{title}»')
            return
        if chat_id in self.announced:
            return
        self.announced.add(chat_id)
        H.log(f'  ⚠ {self.name}: неизвестная группа «{title}» {chat_id}')
        await self.mirror(f'🆕 @{self.me.username} добавлен в «{title}» (`{chat_id}`). Пока молчит.\nРазрешить работу: `/clients allow {chat_id}`')

    async def on_message(self, event) -> None:
        try:
            await self._on_message(event)
        except Exception as e:
            import traceback
            H.log(f'  ⨯ {self.name}.on_message: {type(e).__name__}: {e}')
            traceback.print_exc()

    async def _on_message(self, event) -> None:
        if event.is_private:
            if event.sender_id in H.OWNERS:
                await event.reply(f"Я работаю в группе с клиентом. Мне напрямую — тема «{self.spec['title']}» в hq.")
            return
        chat_id = event.chat_id
        chat = await event.get_chat()
        title = getattr(chat, 'title', '?')
        if chat_id not in self.groups:
            await self.announce(chat_id, title)
            return
        sender = await event.get_sender()
        who = display(sender)
        team = event.sender_id in self.team
        role = 'команда TezCode' if team else 'клиент'
        text = (event.message.message or '').strip()
        media: Optional[Path] = None
        images: list[Path] = []
        if event.message.media and (not getattr(event.message, 'web_preview', None)):
            try:
                day = self.dir / 'inbox' / time.strftime('%Y-%m-%d')
                day.mkdir(parents=True, exist_ok=True)
                async with self.client.action(chat_id, 'typing'):
                    got = await event.download_media(file=str(day) + '/')
                media = Path(got) if got else None
            except Exception as e:
                H.log(f'  ⚠ {self.name}: медиа не скачалось: {e}')
        if media:
            digest, images = await asyncio.to_thread(digest_media, media, media_kind(event.message))
            text = (text + '\n' if text else '') + digest
        line = f'[{role}] {who}: {text}'
        self.log_chat(line)
        await self.mirror(f"👥 {('🟢 ' if team else '')}{who} `{event.sender_id}`\n{(text or '—')[:1500]}{(' 📎' if media else '')}")
        if self.paused or (not text and (not media)):
            return
        self.pending.append({'chat': chat_id, 'title': title, 'line': line, 'images': images})
        self.last_msg[chat_id] = event.id
        if self.pending_task and (not self.pending_task.done()):
            self.pending_task.cancel()
        self.pending_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(DEBOUNCE)
        except asyncio.CancelledError:
            return
        items, self.pending = (self.pending, [])
        if not items:
            return
        chat_id, title = (items[-1]['chat'], items[-1]['title'])
        blocks: list[dict] = []
        for it in items:
            for img in it['images'][:6]:
                blocks += [b for b in H.blocks_for('', img) if b['type'] == 'image']
        body = f"[группа «{title}», {time.strftime('%d.%m %H:%M')}]\n" + '\n'.join((it['line'] for it in items))
        blocks.append({'type': 'text', 'text': body})
        self.active_chat = chat_id
        w = H.MGR.get(self.name)
        try:
            async with self.client.action(chat_id, 'typing'):
                text = await w.ask(blocks, TurnEvents(trace={'source': 'client'}), timeout=TURN_TIMEOUT)
        except Exception as e:
            await self.mirror(f'⚠️ агент упал на сообщении: `{e}`')
            return
        if is_service_failure(text, getattr(w, 'last_meta', None)):
            await self.mirror(f"⚠️ ход агента провалился, в группу НЕ отправлено:\n{(text or '').strip()[:800]}\nmeta: {getattr(w, 'last_meta', {})}\nСообщения клиента остались без ответа.")
            return
        await self.deliver(chat_id, text, reply_to=self.last_msg.get(chat_id))

    async def send_text(self, chat_id: int, text: str, reply_to=None):
        m = None
        for part in H.chunks(text):
            try:
                m = await self.client.send_message(chat_id, part, parse_mode='md', link_preview=False, reply_to=reply_to)
            except Exception:
                m = await self.client.send_message(chat_id, part, parse_mode=None, link_preview=False, reply_to=reply_to)
            reply_to = None
            if m:
                H.log(f'  → группа {chat_id} id={m.id} ({len(part)} симв.)')
        return m

    async def deliver(self, chat_id: int, text: str, reply_to=None) -> None:
        if is_silent(text):
            await self.mirror('🤫 агент промолчал')
            return
        if self.paused:
            await self.mirror(f'⏸ пауза — в группу НЕ ушло:\n{text.strip()}')
            return
        await self.send_text(chat_id, text.strip(), reply_to=reply_to)
        self.log_chat(f'[бот] {text.strip()}')
        await self.mirror(f'🤖 → группа:\n{text.strip()}')

    async def http_send(self, kind: str, data: dict) -> dict:
        chat = self.active_chat or next(iter(sorted(self.groups)), None)
        if not self.client or not chat:
            return {'ok': False, 'error': 'группа клиента не подключена'}
        if self.paused:
            return {'ok': False, 'error': 'агент на паузе — в группу не отправляю'}
        if kind == 'message':
            text = (data.get('text') or '').strip()
            if is_silent(text):
                return {'ok': False, 'error': 'пустое сообщение'}
            if is_service_failure(text):
                return {'ok': False, 'error': 'похоже на служебную ошибку CLI — в группу не отправляю'}
            m = await self.send_text(chat, text)
            self.log_chat(f'[бот] {text}')
            await self.mirror(f'🤖 → группа:\n{text}')
            return {'ok': True, 'message_id': m.id if m else None}
        if kind not in ('photo', 'document'):
            return {'ok': False, 'error': f'агенту клиента доступны только photo/document'}
        path = Path(os.path.expanduser(data.get('path') or ''))
        if not path.exists():
            return {'ok': False, 'error': f'нет файла {path}'}
        caption = (data.get('caption') or '')[:1024]
        m = await self.client.send_file(chat, str(path), caption=caption, force_document=kind == 'document')
        self.log_chat(f'[бот] файл {path.name} {caption}')
        await self.mirror(f'🤖 → группа: файл `{path.name}` {caption}')
        return {'ok': True, 'message_id': m.id, 'size': path.stat().st_size}
AGENTS: dict[str, Agent] = {}

def init(host) -> None:
    global H
    H = host
    for name, spec in SPECS.items():
        AGENTS[name] = Agent(name, spec)

async def start_all() -> None:
    for a in AGENTS.values():
        try:
            await a.start()
        except Exception as e:
            H.log(f'  ⨯ агент клиентов {a.name} не стартовал: {type(e).__name__}: {e}')

async def stop_all() -> None:
    await asyncio.gather(*(a.stop() for a in AGENTS.values()), return_exceptions=True)

async def http_send(worker: str, kind: str, data: dict) -> dict:
    a = AGENTS.get(worker)
    if not a:
        return {'ok': False, 'error': 'нет такого агента клиентов'}
    return await a.http_send(kind, data)
CLIENTS_HELP = '`/clients` — статус · `/clients allow <chat_id>` / `deny <chat_id>` — группа · `/clients team <user_id>` — свой в группе · `/clients pause` / `resume`'

async def command(arg: str, rest: str) -> str:
    a = AGENTS.get('example-client')
    if not a:
        return 'агентов клиентов нет'
    sub = (arg or 'status').lower()
    rid = next(iter(ids(rest)), None)
    if sub == 'allow' and rid is not None:
        set_env(a.k_groups, ','.join((str(x) for x in sorted(a.groups | {rid}))))
        return f'✅ группа `{rid}` разрешена — агент отвечает там'
    if sub == 'deny' and rid is not None:
        set_env(a.k_groups, ','.join((str(x) for x in sorted(a.groups - {rid}))))
        return f'⛔ группа `{rid}` отключена'
    if sub == 'team' and rid is not None:
        set_env(a.k_team, ','.join((str(x) for x in sorted(ids(H.CFG.get(a.k_team)) | {rid}))))
        return f'✅ `{rid}` — команда TezCode'
    if sub in ('pause', 'resume'):
        set_env(a.k_paused, '1' if sub == 'pause' else '0')
        return '⏸ агент на паузе: читает и копирует, в группу не пишет' if sub == 'pause' else '▶️ агент снова отвечает'
    if sub != 'status':
        return CLIENTS_HELP
    w = H.MGR.workers.get(a.name)
    backlog = a.dir / 'backlog.md'
    n = len(re.findall('^## CG-', backlog.read_text(), re.M)) if backlog.exists() else 0
    return '\n'.join([f"**{a.spec['title']}**", f"бот: {('@' + a.me.username if a.me else 'не запущен')}{(' · ⏸ ПАУЗА' if a.paused else '')}", f"группы: {', '.join((f'`{g}`' for g in sorted(a.groups))) or 'нет — жду /clients allow'}", f"команда: {', '.join((f'`{t}`' for t in sorted(ids(H.CFG.get(a.k_team))))) or 'только владелец'}", f"воркер: {(('занят' if w.busy else 'готов') + f', ходов {w.turns}, ${w.cost:.2f}' if w and w.alive() else 'не запущен')}", f'заявок в бэклоге: {n}', CLIENTS_HELP])
