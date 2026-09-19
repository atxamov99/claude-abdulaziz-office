"""claude-hq — Telegram-мост к Claude Code, управляющий несколькими проектами.

Личка = главный (HQ), он делегирует проектам. Топики форум-группы = проекты напрямую.
Транспорт — MTProto (Telethon с bot_token), поэтому файлы ходят до 2 ГБ, а не 50 МБ.
"""
from __future__ import annotations
import asyncio
import base64
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename, UpdateNewChannelMessage
from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest
sys.path.insert(0, str(Path(__file__).parent))
from worker import ClaudeWorker, TurnEvents
import clients
from telemetry import record as office_record
HQ = Path(__file__).resolve().parent
INBOX = HQ / 'inbox'
OUTBOX = HQ / 'outbox'
STATE = HQ / 'state'
LOGS = HQ / 'logs'
for d in (INBOX, OUTBOX, STATE, LOGS):
    d.mkdir(parents=True, exist_ok=True)

def load_env() -> dict:
    env = {}
    f = HQ / '.env'
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(('TG_', 'HQ_'))})
    return env
CFG = load_env()
API_ID = int(CFG.get('TG_API_ID') or 0)
API_HASH = CFG.get('TG_API_HASH', '')
BOT_TOKEN = CFG.get('TG_BOT_TOKEN', '')
OWNERS = {int(x) for x in re.findall('-?\\d+', CFG.get('TG_OWNER_IDS', '')) if x}
HTTP_PORT = int(CFG.get('HQ_HTTP_PORT') or 8765)
DEFAULT_MODEL = CFG.get('HQ_MODEL', 'opus')
DEFAULT_EFFORT = CFG.get('HQ_EFFORT', 'high')
WHISPER_MODEL = CFG.get('HQ_WHISPER_MODEL', 'small')
EDIT_INTERVAL = float(CFG.get('HQ_EDIT_INTERVAL') or 1.6)
TG_LIMIT = 4000

def projects_file() -> Path:
    return HQ / 'projects.json'

def load_projects() -> dict:
    f = projects_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {'projects': {}, 'topics': {}}

def routes_file() -> Path:
    return HQ / 'state' / 'routes.json'

def load_routes() -> dict:
    try:
        return {k: (v[0], v[1]) for k, v in json.loads(routes_file().read_text()).items()}
    except Exception:
        return {}

def save_routes(route: dict) -> None:
    try:
        routes_file().parent.mkdir(parents=True, exist_ok=True)
        routes_file().write_text(json.dumps({k: list(v) for k, v in route.items()}))
    except Exception:
        pass

def save_projects(data: dict) -> None:
    projects_file().write_text(json.dumps(data, ensure_ascii=False, indent=2))

def discover_projects() -> dict:
    """Проекты, которые Claude Code уже знает (из ~/.claude.json)."""
    out = {}
    try:
        data = json.loads((Path.home() / '.claude.json').read_text())
        for p in data.get('projects', {}):
            path = Path(p)
            if path.is_dir() and str(path) != str(Path.home()):
                out[slug(path.name)] = str(path)
    except Exception:
        pass
    return out

def slug(name: str) -> str:
    s = re.sub('[^a-zA-Z0-9]+', '-', name.strip().lower()).strip('-')
    return s or 'project'
HQ_SYSTEM = 'Ты главный агент пользователя в Telegram. Используй hq_projects и hq_start для делегирования. Не объявляй задачу выполненной без проверки результата. Не выполняй разрушительные действия и внешние публикации без разрешения владельца. Секреты не отправляй в сообщения.'

class Manager:

    def __init__(self):
        self.workers: dict[str, ClaudeWorker] = {}
        self.reg = load_projects()
        if not self.reg['projects']:
            self.reg['projects'] = discover_projects()
            save_projects(self.reg)
        self.route: dict[str, tuple[int, Optional[int]]] = load_routes()

    def project_path(self, name: str) -> Optional[str]:
        return self.reg['projects'].get(name)

    def resolve(self, query: str) -> Optional[str]:
        """Имя проекта по неточному вводу: точное → префикс → подстрока."""
        q = slug(query)
        projs = self.reg['projects']
        if q in projs:
            return q
        cands = [k for k in projs if k.startswith(q)] or [k for k in projs if q in k]
        return cands[0] if len(cands) >= 1 else None

    def get(self, name: str) -> ClaudeWorker:
        if name in self.workers:
            return self.workers[name]
        if clients.is_client(name):
            w = clients.make_worker(name, DEFAULT_MODEL, DEFAULT_EFFORT)
        elif name == 'hq':
            w = ClaudeWorker(name='hq', cwd=str(HQ), model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, extra_dirs=[], mcp_config=str(HQ / 'mcp.json'), system_append=HQ_SYSTEM)
        else:
            path = self.project_path(name)
            if not path:
                raise KeyError(name)
            w = ClaudeWorker(name=name, cwd=path, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, mcp_config=str(HQ / 'mcp.json'), system_append='Ты проектный агент в Telegram. Работай в отдельном git worktree, сохраняй чужие изменения. Перед завершением проверь результат и дождись запущенных тобой агентов. Связь с главным — hq_notify.')
        self.workers[name] = w
        if not clients.is_client(name):
            w.permission_mode = CFG.get('HQ_PERMISSION_MODE', 'dontAsk')
        return w

    async def shutdown(self):
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
MGR = Manager()
BOT: Optional[TelegramClient] = None
PENDING_ASKS: dict[str, asyncio.Future] = {}
ASK_OPTIONS: dict[str, list[str]] = {}

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def chunks(text: str, size: int=TG_LIMIT) -> list[str]:
    """Режем длинный ответ по строкам, не разрывая код-блоки посреди строки."""
    if len(text) <= size:
        return [text]
    out, cur = ([], '')
    for line in text.split('\n'):
        while len(line) > size:
            out.append((cur + '\n' + line[:size]).strip() if cur else line[:size])
            cur, line = ('', line[size:])
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = cur + '\n' + line if cur else line
    if cur:
        out.append(cur)
    return out

async def safe_edit(msg, text: str):
    try:
        await msg.edit(text, parse_mode='md', link_preview=False)
    except Exception:
        try:
            await msg.edit(text, parse_mode=None, link_preview=False)
        except Exception:
            pass

async def safe_send(chat, text: str, reply_to=None, buttons=None):
    try:
        m = await BOT.send_message(chat, text, parse_mode='md', link_preview=False, reply_to=reply_to, buttons=buttons)
        log(f'  → отправлено id={m.id} chat={chat} topic={reply_to} ({len(text)} симв.)')
        return m
    except Exception as e1:
        try:
            m = await BOT.send_message(chat, text, parse_mode=None, link_preview=False, reply_to=reply_to, buttons=buttons)
            log(f'  → отправлено (plain) id={m.id} chat={chat} — markdown отклонён: {e1}')
            return m
        except Exception as e2:
            log(f'  ⨯ НЕ ОТПРАВЛЕНО chat={chat} topic={reply_to}: {type(e2).__name__}: {e2}')
            raise

class LiveTurn:
    """Держит одно сообщение в Telegram и дорисовывает его по мере генерации."""

    def __init__(self, chat, reply_to=None, title: str=''):
        self.chat = chat
        self.reply_to = reply_to
        self.title = title
        self.msg = None
        self.text = ''
        self.tool_line = ''
        self.last_edit = 0.0
        self.closed = False
        self._lock = asyncio.Lock()

    async def open(self, queued: bool=False):
        mark = '⧗ в очереди…' if queued else '⏳'
        head = f'_{self.title}_\n\n{mark}' if self.title else mark
        self.msg = await safe_send(self.chat, head, reply_to=self.reply_to)

    def _render(self) -> str:
        body = self.text.strip()
        if len(body) > TG_LIMIT - 200:
            body = '…' + body[-(TG_LIMIT - 200):]
        parts = []
        if self.title:
            parts.append(f'_{self.title}_')
        if self.tool_line:
            parts.append(self.tool_line)
        parts.append(body if body else '⏳')
        return '\n\n'.join(parts)

    async def tick(self, force: bool=False):
        if self.closed or not self.msg:
            return
        now = time.time()
        if not force and now - self.last_edit < EDIT_INTERVAL:
            return
        async with self._lock:
            self.last_edit = now
            await safe_edit(self.msg, self._render())

    async def add_text(self, delta: str):
        self.text += delta
        await self.tick()

    async def set_tool(self, name: str, brief: str):
        icon = {'Bash': '⚙️', 'Read': '📖', 'Edit': '✏️', 'Write': '📝', 'Grep': '🔎', 'WebFetch': '🌐', 'WebSearch': '🔍', 'Agent': '🤖', 'SendMessage': '📨'}.get(name, '🔧')
        self.tool_line = f'{icon} `{name}` {brief}'
        await self.tick()

    async def finish(self, full_text: str, meta: dict):
        self.closed = True
        log(f"  ✓ ход завершён за {int((meta.get('duration_ms') or 0) / 1000)}с, ${meta.get('cost') or 0:.3f}, {len(full_text or '')} симв.")
        self.tool_line = ''
        body = (full_text or '').strip() or '_(пустой ответ)_'
        parts = chunks(body)
        head = f'_{self.title}_\n\n' if self.title else ''
        if self.msg:
            await safe_edit(self.msg, head + parts[0])
        for extra in parts[1:]:
            await safe_send(self.chat, extra, reply_to=self.reply_to)
        if meta.get('is_error'):
            await safe_send(self.chat, f"⚠️ ход завершился ошибкой: `{meta.get('subtype')}`", reply_to=self.reply_to)

async def run_turn(worker_name: str, blocks: list[dict], chat, reply_to=None, topic: Optional[int]=None, title: str='', source: str='telegram'):
    """Отправить сообщение воркеру и отрисовать ход в Telegram."""
    try:
        w = MGR.get(worker_name)
    except KeyError:
        await safe_send(chat, f'нет такого проекта: `{worker_name}`\n/projects — список', reply_to=reply_to)
        return
    MGR.route[worker_name] = (chat if isinstance(chat, int) else chat.id, topic)
    save_routes(MGR.route)
    queued = w.busy
    office_tid = office_record('create', worker_name, {'source': source, 'prompt': '\n'.join((str(b.get('text', '')) for b in blocks))[:500]})
    live = LiveTurn(chat, reply_to=reply_to, title=title)
    if queued:
        log(f'  ⧗ {worker_name} занят — сообщение в очереди')
    ev = TurnEvents(trace={'source': source, 'turn_id': office_tid}, on_text_delta=live.add_text, on_tool=live.set_tool, on_result=live.finish, on_error=lambda m: safe_send(chat, f'⚠️ {m}', reply_to=reply_to))
    try:
        await live.open(queued=queued)
        await w.ask(blocks, ev)
    except asyncio.CancelledError:
        office_record('finish', office_tid, {}, 'CancelledError')
        raise
    except Exception as e:
        office_record('finish', office_tid, {}, type(e).__name__)
        live.closed = True
        await safe_send(chat, f'⚠️ ошибка воркера `{worker_name}`: {e}', reply_to=reply_to)
PENDING: dict[tuple, dict] = {}
DEBOUNCE = float(CFG.get('HQ_DEBOUNCE') or 2.5)

async def _fire(key: tuple):
    """Собрать всё, что пришло за окно склейки, и отправить одним ходом."""
    try:
        await asyncio.sleep(DEBOUNCE)
    except asyncio.CancelledError:
        return
    ent = PENDING.pop(key, None)
    if not ent:
        return
    n = ent['count']
    if n > 1:
        log(f'  ⊕ склеено {n} сообщений в один ход')
    await run_turn(ent['worker'], ent['blocks'], ent['chat'], topic=ent['topic'], title=ent['title'], source=ent['source'])

def enqueue(worker: str, blocks: list[dict], chat, topic, title: str, source: str='telegram'):
    """Текст и файл, отправленные подряд, — это одна мысль. Ждём окно и склеиваем."""
    key = (worker, chat, topic)
    ent = PENDING.get(key)
    if ent:
        if ent['source'] != source:
            ent['source'] = 'mixed'
        ent['blocks'].extend(blocks)
        ent['count'] += 1
        ent['task'].cancel()
    else:
        ent = PENDING[key] = {'worker': worker, 'blocks': list(blocks), 'chat': chat, 'topic': topic, 'title': title, 'count': 1, 'source': source}
    ent['task'] = asyncio.create_task(_fire(key))
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

async def download_media(event) -> Optional[Path]:
    m = event.message
    if not m.media:
        return None
    day = INBOX / time.strftime('%Y-%m-%d')
    day.mkdir(parents=True, exist_ok=True)
    name = None
    if m.document:
        for a in m.document.attributes:
            if isinstance(a, DocumentAttributeFilename):
                name = a.file_name
    if not name:
        ext = '.jpg' if m.photo else mimetypes.guess_extension(getattr(m.document, 'mime_type', '') or '') or '.bin'
        name = f'{int(time.time())}-{uuid.uuid4().hex[:6]}{ext}'
    dest = day / name
    if dest.exists():
        dest = day / f'{dest.stem}-{uuid.uuid4().hex[:4]}{dest.suffix}'
    got = await event.download_media(file=str(dest))
    return Path(got) if got else None

def transcribe(path: Path) -> Optional[str]:
    """Голосовое → текст. Без faster-whisper просто возвращаем None."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    global _WHISPER
    try:
        _WHISPER
    except NameError:
        _WHISPER = None
    if _WHISPER is None:
        _WHISPER = WhisperModel(WHISPER_MODEL, device='cpu', compute_type='int8')
    segments, _ = _WHISPER.transcribe(str(path), beam_size=1)
    return ' '.join((s.text.strip() for s in segments)).strip() or None

def blocks_for(text: str, media: Optional[Path]) -> list[dict]:
    """Собираем content-блоки: картинки уходят прямо в контекст, файлы — путём."""
    blocks: list[dict] = []
    if media and media.suffix.lower() in IMAGE_EXT and (media.stat().st_size < 4500000):
        mt = mimetypes.guess_type(str(media))[0] or 'image/jpeg'
        blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': base64.b64encode(media.read_bytes()).decode()}})
    note = ''
    if media:
        size = media.stat().st_size
        note = f'\n\n[файл из Telegram: `{media}` — {size / 1048576:.1f} МБ]' if size > 1048576 else f'\n\n[файл из Telegram: `{media}`]'
    blocks.append({'type': 'text', 'text': (text or '').strip() + note})
    return blocks

def target_for(worker: str) -> tuple[int, Optional[int]]:
    if worker in MGR.route:
        return MGR.route[worker]
    owner = next(iter(OWNERS), None)
    return (owner, None)

async def http_send(request: web.Request) -> web.Response:
    data = await request.json()
    worker = data.get('worker') or 'hq'
    if clients.is_client(worker):
        r = await clients.http_send(worker, request.match_info['kind'], data)
        return web.json_response(r, status=200 if r.get('ok') else 400)
    chat, topic = target_for(worker)
    if not chat:
        return web.json_response({'ok': False, 'error': 'неизвестен адресат'}, status=400)
    kind = request.match_info['kind']
    caption = (data.get('caption') or '')[:1024]
    try:
        if kind == 'message':
            m = await safe_send(chat, data.get('text') or '', reply_to=topic)
            return web.json_response({'ok': True, 'message_id': m.id})
        path = Path(os.path.expanduser(data.get('path') or ''))
        if not path.exists():
            return web.json_response({'ok': False, 'error': f'нет файла {path}'}, status=404)
        kwargs: dict[str, Any] = {'caption': caption, 'reply_to': topic}
        if kind == 'photo':
            kwargs['force_document'] = False
        elif kind == 'document':
            kwargs['force_document'] = True
        elif kind in ('video', 'audio'):
            kwargs['supports_streaming'] = kind == 'video'
        m = await BOT.send_file(chat, str(path), **kwargs)
        return web.json_response({'ok': True, 'message_id': m.id, 'size': path.stat().st_size})
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=500)

async def http_ask(request: web.Request) -> web.Response:
    data = await request.json()
    worker = data.get('worker') or 'hq'
    if clients.is_client(worker):
        return web.json_response({'ok': False, 'error': 'агенту клиента недоступно'}, status=403)
    chat, topic = target_for(worker)
    question = data.get('question') or '?'
    options = data.get('options') or ['Да', 'Нет']
    key = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    PENDING_ASKS[key] = fut
    ASK_OPTIONS[key] = options[:8]
    rows = [[Button.inline(o[:60], f'ask:{key}:{i}'.encode())] for i, o in enumerate(options[:8])]
    try:
        ask_msg = await safe_send(chat, f'❓ {question}', reply_to=topic, buttons=rows)
    except Exception as e:
        PENDING_ASKS.pop(key, None)
        return web.json_response({'ok': False, 'error': str(e)}, status=500)
    try:
        idx = await asyncio.wait_for(fut, timeout=float(data.get('timeout') or 1800))
        return web.json_response({'ok': True, 'choice': options[idx], 'index': idx})
    except asyncio.TimeoutError:
        try:
            await BOT.edit_message(ask_msg, f'❓ {question}\n\n⌛ ответа не было — вопрос снят', buttons=None)
        except Exception:
            pass
        return web.json_response({'ok': True, 'choice': None, 'timeout': True})
    finally:
        PENDING_ASKS.pop(key, None)
        ASK_OPTIONS.pop(key, None)

async def http_projects(request: web.Request) -> web.Response:
    return web.json_response({'ok': True, 'projects': MGR.reg['projects'], 'workers': {n: {'alive': w.alive(), 'busy': w.busy, 'turns': w.turns, 'cost_usd': round(w.cost, 4), 'cwd': w.cwd, 'session': w.session_id, 'last_activity': w._last_activity, 'office_turn': w._office_turn} for n, w in MGR.workers.items()}})

async def http_office(request: web.Request) -> web.Response:
    data = office_record('snapshot') or {'turns': [], 'background': [], 'error': 'Telemetry unavailable'}
    buffering = [{'project': v['worker'], 'messages': v['count']} for v in PENDING.values()]
    waiting = sum((t['status'] == 'queued' for t in data['turns']))
    active = sum((t['status'] == 'running' for t in data['turns']))
    return web.json_response({'ok': True, 'version': 1, **data, 'buffering': buffering, 'waiting': waiting, 'active': active, 'pending_questions': len(PENDING_ASKS), 'sampled_at': time.time()})
TASKS: dict[str, dict] = {}
WORKER_INBOX: list[dict] = []
LEVELS = ('info', 'decision', 'critical')
STATE_FILE = STATE / 'hq-state.json'

def save_state() -> None:
    try:
        data = {'tasks': TASKS, 'inbox': WORKER_INBOX[-200:]}
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(STATE_FILE)
    except Exception as e:
        log(f'  ⨯ состояние не сохранено: {e}')

def load_state() -> list[dict]:
    """Поднять реестр с диска. Возвращает задачи, которые шли в момент остановки."""
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return []
    TASKS.update(data.get('tasks') or {})
    WORKER_INBOX.extend(data.get('inbox') or [])
    interrupted = [t for t in TASKS.values() if t.get('status') == 'running']
    for t in interrupted:
        t.update(status='error', finished=time.time(), error='бот остановился во время задачи — работа воркера прервана')
    if interrupted:
        save_state()
    return interrupted

async def http_notify(request: web.Request) -> web.Response:
    """hq_notify: воркер сообщает главному. Возвращает ACK сразу — воркер
    не должен ждать, пока главный прочитает."""
    data = await request.json()
    level = (data.get('level') or 'info').lower()
    if level not in LEVELS:
        return web.json_response({'ok': False, 'error': f"level: {', '.join(LEVELS)}"}, status=400)
    item = {'id': uuid.uuid4().hex[:8], 'from': data.get('worker') or '?', 'level': level, 'text': (data.get('text') or '').strip(), 'at': time.time(), 'read': False}
    WORKER_INBOX.append(item)
    save_state()
    log(f"  ✉ {item['from']} → hq [{level}]: {item['text'][:70]}")
    if level == 'critical':
        chat, topic = target_for('hq')
        await safe_send(chat, f"🔴 `{item['from']}`: {item['text'][:900]}", reply_to=topic)
        hq = MGR.workers.get('hq')
        if hq and hq.busy:
            await hq.interrupt()
            enqueue('hq', [{'type': 'text', 'text': f"[critical от воркера {item['from']}]\n{item['text']}\n\nТвой предыдущий ход был прерван этим событием."}], chat, topic, '')
    return web.json_response({'ok': True, 'id': item['id'], 'queued': len(WORKER_INBOX)})

async def http_inbox(request: web.Request) -> web.Response:
    """hq_inbox: главный забирает накопленное. По умолчанию помечает прочитанным."""
    unread = [i for i in WORKER_INBOX if not i['read']]
    if request.query.get('peek') != '1':
        for i in unread:
            i['read'] = True
        save_state()
    now = time.time()
    return web.json_response({'ok': True, 'count': len(unread), 'items': [{k: v for k, v in dict(i, ago_sec=int(now - i['at'])).items() if k != 'at'} for i in unread]})

async def _run_task(tid: str, name: str, prompt: str, timeout: float):
    t = TASKS[tid]
    w = MGR.get(name)
    steps: list[str] = []

    async def on_tool(n, b):
        steps.append(n)
        t['steps'] = len(steps)
    try:
        text = await w.ask([{'type': 'text', 'text': prompt}], TurnEvents(on_tool=on_tool, trace={'source': 'http_task', 'task_id': tid}), timeout=timeout)
        problem = turn_problem(getattr(w, 'last_meta', {}) or {}, text, len(steps))
        if problem:
            t.update(status='error', error=problem, response=text, finished=time.time())
        else:
            t.update(status='done', response=text, finished=time.time())
    except Exception as e:
        t.update(status='error', error=str(e), finished=time.time())
    save_state()
    wake_hq(task_wake_text(t))

def turn_problem(meta: dict, text: str, steps: int) -> Optional[str]:
    """Почему ход нельзя считать сдачей задачи. None — всё в порядке.

    15.09 задача «завершилась» за 3 секунды, 0 шагов, $0 и пустой ответ — и была
    записана как done: главный доложил бы владельцу, что проверки прошли.
    """
    if meta.get('died'):
        return 'процесс воркера завершился, не закончив ход'
    if meta.get('is_error'):
        return f"ход завершился ошибкой {meta.get('subtype')}: {meta.get('result') or ''}".strip()
    if not (text or '').strip() and steps == 0:
        return 'пустой ответ без единого шага — воркер ничего не сделал'
    return None

def task_wake_text(t: dict) -> str:
    head = f"[фоновая задача `{t['id']}` в проекте {t['project']} {('завершена' if t['status'] == 'done' else 'упала')}, шагов {t.get('steps', 0)}]"
    body = t.get('response') if t['status'] == 'done' else f"ошибка: {t.get('error')}"
    return f"{head}\n{(body or '')[:8000]}\n\nЭто не сообщение владельца, а событие. Если ты обещал ему прислать результат или ждал эту задачу, чтобы продолжить, — сделай это сейчас. Если воркер отчитался «запустил агентов, пришлю позже» — работа не закончена: проверь результат по файлам, прежде чем сообщать «готово»."

def wake_hq(text: str) -> None:
    """Запустить ход главного событием, как будто владелец написал в его чат."""
    chat, topic = target_for('hq')
    if not chat:
        log('  ⨯ wake_hq: неизвестен чат главного')
        return
    log(f'  ⏰ будим главного: {text[:80]!r}')
    enqueue('hq', [{'type': 'text', 'text': text}], chat, topic, '', source='wake')

async def http_wake(request: web.Request) -> web.Response:
    data = await request.json()
    text = (data.get('text') or '').strip()
    if not text:
        return web.json_response({'ok': False, 'error': 'пустой text'}, status=400)
    wake_hq(text)
    return web.json_response({'ok': True})

async def http_start(request: web.Request) -> web.Response:
    """hq_start: отправить задачу и сразу вернуть управление."""
    data = await request.json()
    name = MGR.resolve(data.get('project') or '')
    if not name:
        return web.json_response({'ok': False, 'error': 'проект не найден', 'known': list(MGR.reg['projects'])}, status=404)
    try:
        w = MGR.get(name)
    except KeyError:
        return web.json_response({'ok': False, 'error': 'нет пути к проекту'}, status=404)
    if w.busy:
        return web.json_response({'ok': False, 'error': f'{name} занят'}, status=409)
    MGR.route[name] = target_for('hq')
    tid = f'{name}-{uuid.uuid4().hex[:6]}'
    TASKS[tid] = {'id': tid, 'project': name, 'prompt': data.get('prompt', ''), 'status': 'running', 'started': time.time(), 'steps': 0}
    save_state()
    asyncio.create_task(_run_task(tid, name, data.get('prompt') or '', float(data.get('timeout') or 3600)))
    return web.json_response({'ok': True, 'task_id': tid, 'project': name})

async def http_tasks(request: web.Request) -> web.Response:
    now = time.time()
    return web.json_response({'ok': True, 'tasks': [{k: v for k, v in dict(t, elapsed=int(now - t['started'])).items() if k != 'response'} for t in TASKS.values()]})

async def http_result(request: web.Request) -> web.Response:
    data = await request.json()
    t = TASKS.get(data.get('task_id') or '')
    if not t:
        return web.json_response({'ok': False, 'error': 'нет такой задачи', 'known': list(TASKS)}, status=404)
    return web.json_response({'ok': True, **t})

async def http_run(request: web.Request) -> web.Response:
    """hq_run: главный делегирует задачу воркеру проекта и ждёт ответ."""
    data = await request.json()
    name = MGR.resolve(data.get('project') or '')
    if not name:
        return web.json_response({'ok': False, 'error': 'проект не найден', 'known': list(MGR.reg['projects'])}, status=404)
    prompt = data.get('prompt') or ''
    chat, topic = target_for('hq')
    try:
        w = MGR.get(name)
    except KeyError:
        return web.json_response({'ok': False, 'error': 'нет пути к проекту'}, status=404)
    if w.busy:
        return web.json_response({'ok': False, 'error': f'{name} занят другой задачей'}, status=409)
    MGR.route[name] = (chat, topic)
    note = await safe_send(chat, f'→ `{name}`: {prompt[:180]}', reply_to=topic)
    tool_seen: list[str] = []

    async def on_tool(n, b):
        tool_seen.append(n)
        if len(tool_seen) % 6 == 0:
            await safe_edit(note, f'→ `{name}`: {prompt[:180]}\n_{len(tool_seen)} шагов…_')
    ev = TurnEvents(on_tool=on_tool, trace={'source': 'http_run'})
    try:
        text = await w.ask([{'type': 'text', 'text': prompt}], ev, timeout=float(data.get('timeout') or 3600))
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=500)
    await safe_edit(note, f'✅ `{name}` ({len(tool_seen)} шагов)')
    return web.json_response({'ok': True, 'project': name, 'response': text})

async def start_http():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get('/health', lambda r: web.json_response({'ok': True}))
    app.router.add_get('/projects', http_projects)
    app.router.add_get('/office', http_office)
    app.router.add_post('/run', http_run)
    app.router.add_post('/run/start', http_start)
    app.router.add_post('/notify', http_notify)
    app.router.add_post('/wake', http_wake)
    app.router.add_get('/inbox', http_inbox)
    app.router.add_get('/tasks', http_tasks)
    app.router.add_post('/result', http_result)
    app.router.add_post('/ask', http_ask)
    app.router.add_post('/send/{kind}', http_send)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', HTTP_PORT).start()
    return runner
HELP = '**claude-hq** — твой Claude во всех проектах сразу.\n\nПросто пиши — попадёшь к главному, он сам решит, какому проекту делегировать.\nФото, документы, видео, голосовые понимает. Файлы отдаёт обратно в чат.\n\n**Команды**\n`/projects` — список проектов\n`/agents` — живые сессии Claude Code в терминалах\n`/to <проект> <текст>` — напрямую в проект, мимо главного\n`/setup` — в форум-группе: создать тему на каждый проект\n`/diag` — что бот видит в этом чате и какие у него права\n`/bind <проект>` — привязать текущий топик к проекту\n`/add <имя> <путь>` — добавить проект\n`/new [проект]` — начать контекст заново\n`/stop [проект]` — прервать текущую задачу\n`/model <opus|sonnet|haiku>` — сменить модель\n`/status` — что запущено, сколько потрачено\n`/clients` — агент клиентов Example Client: группы, пауза, команда'

def topic_of(event) -> Optional[int]:
    r = getattr(event.message, 'reply_to', None)
    if r and getattr(r, 'forum_topic', False):
        return r.reply_to_top_id or r.reply_to_msg_id
    return None

def worker_for(event) -> str:
    t = topic_of(event)
    if t:
        bound = MGR.reg.get('topics', {}).get(str(t))
        if bound:
            return bound
    return 'hq'

async def cmd_projects(event):
    reg = MGR.reg['projects']
    if not reg:
        await event.reply('проектов нет. `/add <имя> <путь>`')
        return
    topics = {v: k for k, v in MGR.reg.get('topics', {}).items()}
    lines = []
    for name, path in sorted(reg.items()):
        w = MGR.workers.get(name)
        mark = '🟢' if w and w.alive() and w.busy else '⚪️' if w and w.alive() else '·'
        tp = f' (топик {topics[name]})' if name in topics else ''
        lines.append(f'{mark} `{name}`{tp} — {path}')
    await safe_send(event.chat_id, '**Проекты**\n' + '\n'.join(lines), reply_to=topic_of(event))

async def cmd_agents(event):
    try:
        out = subprocess.run(['claude', 'agents', '--json'], capture_output=True, text=True, timeout=20).stdout
        rows = json.loads(out)
    except Exception as e:
        await event.reply(f'не смог получить список: {e}')
        return
    if not rows:
        await event.reply('живых сессий Claude Code нет')
        return
    lines = []
    for r in rows:
        icon = '🟢' if r.get('status') == 'busy' else '⚪️'
        kind = 'bg' if r.get('kind') == 'background' else 'term'
        lines.append(f"{icon} `{r.get('name')}` ({kind}) — {r.get('cwd')}")
    await safe_send(event.chat_id, '**Сессии Claude Code**\n' + '\n'.join(lines), reply_to=topic_of(event))

async def cmd_status(event):
    lines = []
    for n, w in MGR.workers.items():
        state = 'занят' if w.busy else 'готов' if w.alive() else 'остановлен'
        lines.append(f'`{n}` — {state}, ходов {w.turns}, ${w.cost:.2f}\n   {w.cwd}')
    text = '**Воркеры**\n' + ('\n'.join(lines) if lines else 'ни одного не запущено')
    text += f'\n\nмодель по умолчанию: `{DEFAULT_MODEL}` · эффорт `{DEFAULT_EFFORT}`'
    await safe_send(event.chat_id, text, reply_to=topic_of(event))
TOPIC_TITLES = {}

async def cmd_setup(event):
    """Создать тему на каждый проект в текущей форум-группе и привязать их."""
    chat = await event.get_chat()
    if not getattr(chat, 'forum', False):
        await event.reply('это не форум-группа. Включи в настройках группы: **Темы**')
        return
    existing = {}
    try:
        res = await BOT(GetForumTopicsRequest(peer=chat, offset_date=None, offset_id=0, offset_topic=0, limit=100))
        existing = {t.title: t.id for t in res.topics if hasattr(t, 'title')}
    except Exception:
        pass
    mapping = dict(MGR.reg.get('topics') or {})
    created, reused, failed = ([], [], [])
    for name in sorted(MGR.reg['projects']):
        title, color = TOPIC_TITLES.get(name, (name, 7322096))
        if title in existing:
            mapping[str(existing[title])] = name
            reused.append(title)
            continue
        try:
            r = await BOT(CreateForumTopicRequest(peer=chat, title=title, icon_color=color, random_id=int.from_bytes(os.urandom(7), 'big')))
            tid = next((u.message.id for u in getattr(r, 'updates', []) if isinstance(u, UpdateNewChannelMessage)), None)
            if tid:
                mapping[str(tid)] = name
                created.append(title)
            else:
                failed.append(title)
            await asyncio.sleep(0.4)
        except Exception as e:
            log(f'  ⨯ тема «{title}» не создана: {type(e).__name__}: {e}')
            failed.append(f'{title} ({type(e).__name__})')
    MGR.reg['topics'] = mapping
    save_projects(MGR.reg)
    lines = [f'✅ тем привязано: **{len(mapping)}**']
    if created:
        lines.append('создано: ' + ', '.join(created))
    if reused:
        lines.append('уже были: ' + ', '.join(reused))
    if failed:
        lines.append('не вышло: ' + ', '.join(failed) + '\n(проверь, что у бота есть право «Управление темами»)')
    lines.append('\nПиши в тему — попадёшь прямо в тот проект. General остаётся главным.')
    await safe_send(event.chat_id, '\n'.join(lines), reply_to=topic_of(event))

async def cmd_diag(event):
    chat = await event.get_chat()
    lines = [f'**чат:** `{event.chat_id}`', f"**тип:** {('личка' if event.is_private else type(chat).__name__)}", f"**форум (темы):** {('да' if getattr(chat, 'forum', False) else 'НЕТ')}", f"**текущая тема:** {topic_of(event) or 'нет (General)'}", f"**ты:** `{event.sender_id}` {('✅ владелец' if event.sender_id in OWNERS else '⨯ не владелец')}"]
    if not event.is_private:
        try:
            me = await BOT.get_me()
            perm = await BOT.get_permissions(chat, me)
            lines.append(f"**бот админ:** {('да' if perm.is_admin else 'НЕТ')}")
            if perm.is_admin:
                mt = getattr(perm.participant.admin_rights, 'manage_topics', None)
                lines.append(f"**право «управление темами»:** {('да' if mt else 'НЕТ')}")
        except Exception as e:
            lines.append(f'**права бота:** не смог прочитать ({type(e).__name__})')
    bound = MGR.reg.get('topics') or {}
    lines.append(f'**привязано тем:** {len(bound)}')
    await safe_send(event.chat_id, '\n'.join(lines), reply_to=topic_of(event))

async def handle_command(event, text: str) -> bool:
    parts = text.split(maxsplit=2)
    cmd = parts[0].lower().split('@')[0]
    arg = parts[1] if len(parts) > 1 else ''
    rest = parts[2] if len(parts) > 2 else ''
    if cmd in ('/start', '/help'):
        await safe_send(event.chat_id, HELP, reply_to=topic_of(event))
    elif cmd == '/projects':
        await cmd_projects(event)
    elif cmd == '/setup':
        await cmd_setup(event)
    elif cmd == '/diag':
        await cmd_diag(event)
    elif cmd == '/agents':
        await cmd_agents(event)
    elif cmd == '/status':
        await cmd_status(event)
    elif cmd == '/add':
        path = os.path.expanduser(rest.strip() or '')
        if not arg or not Path(path).is_dir():
            await event.reply('формат: `/add <имя> <существующий путь>`')
        else:
            MGR.reg['projects'][slug(arg)] = str(Path(path).resolve())
            save_projects(MGR.reg)
            await event.reply(f'добавлен `{slug(arg)}` → {path}')
    elif cmd == '/bind':
        t = topic_of(event)
        name = MGR.resolve(arg) if arg else None
        if not t:
            await event.reply('это не топик форум-группы')
        elif not name:
            await event.reply(f'нет проекта `{arg}`')
        else:
            MGR.reg.setdefault('topics', {})[str(t)] = name
            save_projects(MGR.reg)
            await event.reply(f'топик привязан к `{name}` — пиши сюда, попадёшь прямо в проект')
    elif cmd == '/to':
        name = MGR.resolve(arg) if arg else None
        if not name:
            await event.reply(f'нет проекта `{arg}`. /projects')
        elif not rest.strip():
            await event.reply('формат: `/to <проект> <задача>`')
        else:
            await run_turn(name, [{'type': 'text', 'text': rest}], event.chat_id, topic=topic_of(event), title=name)
    elif cmd == '/new':
        name = MGR.resolve(arg) if arg else worker_for(event)
        w = MGR.get(name or 'hq')
        await w.restart(fresh=True)
        await event.reply(f'`{w.name}`: контекст сброшен')
    elif cmd == '/stop':
        name = MGR.resolve(arg) if arg else worker_for(event)
        w = MGR.workers.get(name or 'hq')
        if w and w.busy:
            await w.interrupt()
            await event.reply(f'`{w.name}`: прервано')
        else:
            await event.reply('нечего прерывать')
    elif cmd == '/model':
        global DEFAULT_MODEL
        if arg not in ('opus', 'sonnet', 'haiku', 'fable'):
            await event.reply('модели: `opus`, `sonnet`, `haiku`, `fable`')
        else:
            DEFAULT_MODEL = arg
            for w in MGR.workers.values():
                w.model = arg
                if w.alive():
                    await w.restart(fresh=False)
            await event.reply(f'модель: `{arg}` (контекст сохранён)')
    elif cmd == '/clients':
        await safe_send(event.chat_id, await clients.command(arg, rest), reply_to=topic_of(event))
    else:
        return False
    return True

async def claim_owner(event) -> bool:
    """Первый /claim при пустом TG_OWNER_IDS делает написавшего владельцем."""
    if OWNERS:
        return False
    OWNERS.add(event.sender_id)
    env = HQ / '.env'
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [l for l in lines if not l.startswith('TG_OWNER_IDS=')]
    lines.append(f'TG_OWNER_IDS={event.sender_id}')
    env.write_text('\n'.join(lines) + '\n')
    print(f'владелец закреплён: {event.sender_id}', flush=True)
    await event.reply(f'✅ ты владелец (`{event.sender_id}`). Записал в .env. /help — что умею.')
    return True

async def on_message(event):
    try:
        chat = await event.get_chat()
        kind = 'личка' if event.is_private else 'форум' if getattr(chat, 'forum', False) else 'группа'
        log(f"← {kind} chat={event.chat_id} topic={topic_of(event)} from={event.sender_id} media={bool(event.message.media)} {(event.message.message or '')[:60]!r}")
    except Exception:
        pass
    if event.sender_id not in OWNERS:
        log(f'  ⨯ отклонено: {event.sender_id} не в TG_OWNER_IDS={sorted(OWNERS)}')
        if (event.message.message or '').strip().startswith('/claim'):
            await claim_owner(event)
        return
    text = (event.message.message or '').strip()
    if text.startswith('/'):
        try:
            if await handle_command(event, text):
                return
        except Exception as e:
            import traceback
            log(f'  ⨯ команда {text.split()[0]} упала: {type(e).__name__}: {e}')
            traceback.print_exc()
            await event.reply(f'⚠️ `{text.split()[0]}` упала: `{type(e).__name__}: {e}`')
            return
    media = None
    if event.message.media:
        try:
            async with BOT.action(event.chat_id, 'typing'):
                media = await download_media(event)
        except Exception as e:
            log(f'  ⚠ медиа не скачалось ({type(e).__name__}: {e}) — беру только текст')
            media = None
        if media and media.suffix.lower() in ('.ogg', '.oga', '.m4a', '.mp3', '.wav') and getattr(event.message, 'voice', None):
            note = await event.reply('🎧 распознаю…')
            tr = await asyncio.to_thread(transcribe, media)
            if tr:
                text = (text + '\n' + tr).strip() if text else tr
                await safe_edit(note, f'🎙 _{tr}_')
                media = None
            else:
                await safe_edit(note, '🎙 голосовое (распознавание не установлено — передал файлом)')
    if not text and (not media):
        return
    name = worker_for(event)
    title = '' if name == 'hq' else name
    enqueue(name, blocks_for(text, media), event.chat_id, topic_of(event), title)

async def on_callback(event):
    if event.sender_id not in OWNERS:
        return
    data = event.data.decode()
    if not data.startswith('ask:'):
        return
    _, key, idx = data.split(':', 2)
    fut = PENDING_ASKS.get(key)
    log(f"← кнопка {key}:{idx} ({('ждали' if fut and (not fut.done()) else 'устарела')})")
    if fut and (not fut.done()):
        fut.set_result(int(idx))
        choice = ASK_OPTIONS.get(key, [])[int(idx)] if int(idx) < len(ASK_OPTIONS.get(key, [])) else idx
        await event.answer(f'✅ {choice}')
        await _edit_ask(event, f'✅ выбрано: {choice}')
    else:
        await event.answer('⌛ вопрос уже неактуален — ответь сообщением', alert=True)
        await _edit_ask(event, '⌛ неактуально')

async def _edit_ask(event, mark: str):
    text = f'{event.message.message}\n\n{mark}'
    for mode in ('md', None):
        try:
            await event.edit(text, parse_mode=mode, buttons=None)
            return
        except Exception as e:
            last = e
    log(f'  ⨯ не смог отметить ответ на кнопку: {last}')

async def main():
    global BOT
    missing = [k for k, v in (('TG_API_ID', API_ID), ('TG_API_HASH', API_HASH), ('TG_BOT_TOKEN', BOT_TOKEN), ('TG_OWNER_IDS', OWNERS)) if not v]
    if missing:
        print(f"не заполнено в ~/claude-hq/.env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    BOT = TelegramClient(str(STATE / 'bot'), API_ID, API_HASH, connection_retries=None, retry_delay=5, catch_up=True)
    await BOT.start(bot_token=BOT_TOKEN)
    me = await BOT.get_me()
    clients.init(sys.modules[__name__])
    print(f"claude-hq запущен: @{me.username} · владельцы {sorted(OWNERS) or 'нет'} · {len(MGR.reg['projects'])} проектов", flush=True)
    if not OWNERS:
        print('TG_OWNER_IDS пуст — напиши боту /claim, чтобы закрепить себя владельцем', flush=True)
    BOT.add_event_handler(on_message, events.NewMessage(incoming=True))
    BOT.add_event_handler(on_callback, events.CallbackQuery())
    interrupted = load_state()
    runner = await start_http()
    await clients.start_all()
    if interrupted:
        wake_hq('[событие: бот перезапустился, пока шли фоновые задачи — они прерваны]\n' + '\n'.join((f"- `{t['id']}` ({t['project']}): {t.get('prompt', '')[:200]}" for t in interrupted)) + '\n\nПроверь по файлам и git, что успело сделаться, и перезапусти задачу или сообщи владельцу, если он её ждёт.')
    try:
        await BOT.run_until_disconnected()
    finally:
        await MGR.shutdown()
        await clients.stop_all()
        await runner.cleanup()
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
