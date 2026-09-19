#!/usr/bin/env python3
"""MCP-сервер claude-hq: даёт Claude руки в Telegram и доступ к другим проектам.

Тонкий клиент — вся работа с Telegram живёт в боте (у него один MTProto-коннект),
сюда ходим по локальному HTTP. Только stdlib: сервер стартует процессом `claude`.
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

PORT = os.environ.get("HQ_HTTP_PORT", "8765")
BASE = f"http://127.0.0.1:{PORT}"
WORKER = os.environ.get("CLAUDE_HQ_WORKER", "hq")

FILE_TOOLS = {
    "tg_send_photo": ("photo", "Отправить изображение в Telegram-чат пользователя (скриншот, график, диаграмма)."),
    "tg_send_video": ("video", "Отправить видео в Telegram (запись экрана, демо). До 2 ГБ."),
    "tg_send_document": ("document", "Отправить файл в Telegram (лог, PDF, архив, код). До 2 ГБ."),
    "tg_send_audio": ("audio", "Отправить аудиофайл в Telegram."),
}

TOOLS = [
    {
        "name": "tg_send_message",
        "description": ("Промежуточное сообщение пользователю в Telegram, не дожидаясь конца хода. "
                        "Для прогресса длинных задач. Обычный финальный ответ слать сюда не нужно."),
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Текст (Markdown)"}}, "required": ["text"]},
    },
    {
        "name": "tg_ask",
        "description": ("Задать пользователю вопрос с кнопками и ДОЖДАТЬСЯ ответа. "
                        "Используй перед необратимым: удаление, force-push, деплой, отправка наружу."),
        "inputSchema": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Варианты, по умолчанию Да/Нет (до 8)"},
        }, "required": ["question"]},
    },
    {
        "name": "hq_run",
        "description": ("Поставить задачу другому проекту. У каждого свой постоянный процесс claude "
                        "со своим контекстом, CLAUDE.md и MCP-серверами. Блокирует до ответа. "
                        "Список проектов — hq_projects."),
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "Имя проекта (можно неточно)"},
            "prompt": {"type": "string", "description": "Задача. Пиши так, будто пишешь коллеге в этом проекте"},
        }, "required": ["project", "prompt"]},
    },
    {
        "name": "hq_start",
        "description": ("Отправить задачу проекту и СРАЗУ вернуть управление, не дожидаясь "
                        "результата. Так можно запустить несколько проектов одновременно и "
                        "продолжать разговор. Возвращает task_id. Когда воркер закончит, придёт "
                        "уведомление; результат забирается через hq_result."),
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string"},
            "prompt": {"type": "string", "description": "Задача целиком: воркер не видит этот разговор"},
        }, "required": ["project", "prompt"]},
    },
    {
        "name": "hq_tasks",
        "description": "Что сейчас выполняется в проектах: статус, сколько шагов, сколько идёт.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hq_result",
        "description": "Забрать результат задачи по task_id (после уведомления о завершении).",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "hq_notify",
        "description": ("Сообщить главному агенту снизу вверх. Для воркеров проектов: "
                        "главный не видит, что у тебя происходит, пока ты не скажешь. "
                        "Возвращает подтверждение приёма сразу — ждать, пока главный "
                        "прочитает, не нужно.\n"
                        "level=info — нашёл что-то, работай дальше, прочитают позже.\n"
                        "level=decision — нужен выбор; делай что можешь без ответа, "
                        "остановись только когда упрёшься.\n"
                        "level=critical — секрет в логах, конфликт с продом, потеря данных. "
                        "Оборвёт текущий ход главного. Не использовать для «я закончил»."),
        "inputSchema": {"type": "object", "properties": {
            "level": {"type": "string", "enum": ["info", "decision", "critical"]},
            "text": {"type": "string", "description": "Суть целиком: главный не видит твой контекст"},
        }, "required": ["level", "text"]},
    },
    {
        "name": "hq_inbox",
        "description": ("Забрать накопившиеся сообщения от воркеров. Для главного: "
                        "смотреть на границах хода, особенно перед тем как отвечать "
                        "владельцу или запускать новую задачу."),
        "inputSchema": {"type": "object", "properties": {
            "peek": {"type": "boolean", "description": "true — посмотреть, не помечая прочитанным"},
        }},
    },
    {
        "name": "hq_projects",
        "description": "Список проектов и состояние их воркеров.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
for tname, (_kind, desc) in FILE_TOOLS.items():
    TOOLS.append({
        "name": tname, "description": desc,
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Абсолютный путь к файлу на этой машине"},
            "caption": {"type": "string", "description": "Подпись (необязательно)"},
        }, "required": ["path"]},
    })


# Агенту клиента нельзя видеть hq_run/tg_ask и прочее: список сужается через env.
_ONLY = {t.strip() for t in os.environ.get("HQ_TOOLS", "").split(",") if t.strip()}
if _ONLY:
    TOOLS = [t for t in TOOLS if t["name"] in _ONLY]


def post(path: str, payload: dict, timeout: float = 3700) -> dict:
    payload = dict(payload, worker=WORKER)
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"бот claude-hq недоступен на {BASE}: {e}"}


def call(name: str, args: dict) -> str:
    if name == "tg_send_message":
        r = post("/send/message", {"text": args.get("text", "")})
        return "отправлено" if r.get("ok") else f"ошибка: {r.get('error')}"
    if name == "tg_ask":
        r = post("/ask", {"question": args.get("question", "?"),
                          "options": args.get("options") or ["Да", "Нет"]})
        if not r.get("ok"):
            return f"ошибка: {r.get('error')}"
        if r.get("timeout"):
            return "пользователь не ответил (таймаут) — считай, что согласия нет"
        return f"пользователь выбрал: {r.get('choice')}"
    if name == "hq_run":
        r = post("/run", {"project": args.get("project", ""), "prompt": args.get("prompt", "")})
        if not r.get("ok"):
            known = r.get("known")
            return f"ошибка: {r.get('error')}" + (f" · известные: {', '.join(known)}" if known else "")
        return f"[{r.get('project')}]\n{r.get('response')}"
    if name == "hq_notify":
        r = post("/notify", {"level": args.get("level", "info"),
                             "text": args.get("text", "")}, timeout=30)
        if not r.get("ok"):
            return f"ошибка: {r.get('error')}"
        return f"принято ({r.get('id')}), в очереди у главного: {r.get('queued')}"
    if name == "hq_inbox":
        try:
            q = "?peek=1" if args.get("peek") else ""
            with urllib.request.urlopen(BASE + "/inbox" + q, timeout=15) as resp:
                d = json.loads(resp.read().decode())
            if not d.get("count"):
                return "входящих нет"
            out = [f"непрочитанных: {d['count']}"]
            for i in d["items"]:
                out.append(f"[{i['level']}] от {i['from']}, {i['ago_sec']} с назад:\n{i['text']}")
            return "\n\n".join(out)
        except Exception as e:
            return f"ошибка: {e}"
    if name == "hq_start":
        r = post("/run/start", {"project": args.get("project", ""),
                                "prompt": args.get("prompt", "")}, timeout=60)
        if not r.get("ok"):
            known = r.get("known")
            return f"ошибка: {r.get('error')}" + (f" · известные: {', '.join(known)}" if known else "")
        return (f"задача {r['task_id']} запущена в проекте {r['project']}. "
                f"Управление вернулось — можно работать дальше.")
    if name == "hq_tasks":
        try:
            with urllib.request.urlopen(BASE + "/tasks", timeout=15) as resp:
                return json.dumps(json.loads(resp.read().decode()), ensure_ascii=False, indent=2)
        except Exception as e:
            return f"ошибка: {e}"
    if name == "hq_result":
        r = post("/result", {"task_id": args.get("task_id", "")}, timeout=30)
        if not r.get("ok"):
            return f"ошибка: {r.get('error')} · известные: {', '.join(r.get('known', []))}"
        if r.get("status") == "running":
            return f"ещё выполняется, {r.get('steps', 0)} шагов"
        if r.get("status") == "error":
            return f"задача упала: {r.get('error')}"
        return f"[{r.get('project')}]\n{r.get('response')}"
    if name == "hq_projects":
        try:
            with urllib.request.urlopen(BASE + "/projects", timeout=15) as resp:
                return json.dumps(json.loads(resp.read().decode()), ensure_ascii=False, indent=2)
        except Exception as e:
            return f"ошибка: {e}"
    if name in FILE_TOOLS:
        kind = FILE_TOOLS[name][0]
        path = os.path.expanduser(args.get("path", ""))
        if not os.path.exists(path):
            return f"файла нет: {path}"
        r = post(f"/send/{kind}", {"path": path, "caption": args.get("caption", "")}, timeout=900)
        if not r.get("ok"):
            return f"ошибка: {r.get('error')}"
        return f"отправлено ({r.get('size', 0)/1048576:.1f} МБ)"
    return f"неизвестный инструмент: {name}"


# stdout пишут несколько потоков сразу — строка ответа должна уходить целиком,
# иначе два JSON-RPC ответа перемешаются и клиент разберёт мусор.
_OUT = threading.Lock()


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error:
        msg["error"] = {"code": -32000, "message": error}
    else:
        msg["result"] = result
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _OUT:
        sys.stdout.write(line)
        sys.stdout.flush()


def handle_call(mid, params):
    """Один tools/call. Выполняется в отдельном потоке: hq_run ждёт воркера
    проекта минутами, и пока он ждёт, сервер обязан принимать другие вызовы —
    иначе два проекта физически не могут работать одновременно."""
    try:
        out = call(params.get("name", ""), params.get("arguments") or {})
        reply(mid, {"content": [{"type": "text", "text": out}]})
    except Exception as e:  # noqa: BLE001
        reply(mid, {"content": [{"type": "text", "text": f"сбой: {e}"}], "isError": True})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method, mid = req.get("method"), req.get("id")
        if method == "initialize":
            reply(mid, {"protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "claude-hq", "version": "2.0.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            # JSON-RPC разрешает отвечать не по порядку — ответ находят по id.
            threading.Thread(target=handle_call, args=(mid, req.get("params") or {}),
                             daemon=True).start()
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif method in ("resources/list", "prompts/list"):
            reply(mid, {"resources": []} if method == "resources/list" else {"prompts": []})
        elif mid is not None:
            reply(mid, error=f"метод не поддерживается: {method}")


if __name__ == "__main__":
    main()
