import { useEffect, useRef, useState } from "react";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { api, type PendingQuestion } from "../ipc/commands";
import type { useHqMonitor } from "./HqOffice";
import { t } from "../i18n";
import "./hq.css";

type Bubble = {
  id: string;
  role: "user" | "assistant" | "error";
  text: string;
  toolLines: string[];
  done: boolean;
};

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function ChatView({ monitor }: { monitor: ReturnType<typeof useHqMonitor> }) {
  const workers = monitor.snapshot?.workers ?? {};
  const names = Object.keys(workers).sort();
  const [selected, setSelected] = useState<string | null>(names[0] ?? null);
  const [threads, setThreads] = useState<Record<string, Bubble[]>>({});
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [restarting, setRestarting] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingQuestion[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addError, setAddError] = useState("");
  const unlistenRef = useRef<UnlistenFn[]>([]);

  useEffect(() => {
    if (!selected && names.length) setSelected(names[0]);
  }, [names, selected]);

  useEffect(() => {
    let disposed = false;
    async function poll() {
      const r = await api.hqPendingQuestions().catch(() => null);
      if (!disposed && r?.ok) setPending(r.pending ?? []);
    }
    poll();
    const timer = setInterval(poll, 5000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    return () => {
      unlistenRef.current.forEach((fn) => fn());
      unlistenRef.current = [];
    };
  }, []);

  function appendBubble(project: string, bubble: Bubble) {
    setThreads((prev) => ({ ...prev, [project]: [...(prev[project] ?? []), bubble] }));
  }

  function patchBubble(project: string, id: string, patch: Partial<Bubble>) {
    setThreads((prev) => ({
      ...prev,
      [project]: (prev[project] ?? []).map((b) => (b.id === id ? { ...b, ...patch } : b)),
    }));
  }

  async function send() {
    const project = selected;
    const prompt = input.trim();
    if (!project || !prompt || sending) return;
    setInput("");
    setSending(true);
    appendBubble(project, { id: newId(), role: "user", text: prompt, toolLines: [], done: true });
    const replyId = newId();
    appendBubble(project, { id: replyId, role: "assistant", text: "", toolLines: [], done: false });
    const streamId = newId();

    unlistenRef.current.forEach((fn) => fn());
    unlistenRef.current = [];
    const cleanup = () => {
      unlistenRef.current.forEach((fn) => fn());
      unlistenRef.current = [];
      setSending(false);
    };

    const subs = await Promise.all([
      listen<{ text: string }>(`chat://${streamId}/delta`, (e) => {
        setThreads((prev) => ({
          ...prev,
          [project]: (prev[project] ?? []).map((b) =>
            b.id === replyId ? { ...b, text: b.text + e.payload.text } : b
          ),
        }));
      }),
      listen<{ name: string; brief: string }>(`chat://${streamId}/tool`, (e) => {
        setThreads((prev) => ({
          ...prev,
          [project]: (prev[project] ?? []).map((b) =>
            b.id === replyId
              ? { ...b, toolLines: [...b.toolLines, t("hq.chat.toolLine", { name: e.payload.name, brief: e.payload.brief })] }
              : b
          ),
        }));
      }),
      listen<{ text: string }>(`chat://${streamId}/result`, (e) => {
        patchBubble(project, replyId, { text: e.payload.text, done: true });
      }),
      listen<{ message: string }>(`chat://${streamId}/error`, (e) => {
        patchBubble(project, replyId, { role: "error", text: t("hq.chat.errorPrefix", { message: e.payload.message }), done: true });
      }),
      listen(`chat://${streamId}/done`, () => {
        // Belt-and-suspenders: if the stream ended without ever seeing a result/error
        // (e.g. the bot process died mid-turn), don't leave the bubble spinning forever.
        setThreads((prev) => ({
          ...prev,
          [project]: (prev[project] ?? []).map((b) =>
            b.id === replyId && !b.done ? { ...b, role: "error", text: t("hq.chat.streamError") } : b
          ),
        }));
        cleanup();
      }),
    ]);
    unlistenRef.current = subs;

    try {
      await api.hqChatSend(streamId, project, prompt);
    } catch (e) {
      patchBubble(project, replyId, { role: "error", text: t("hq.chat.errorPrefix", { message: String(e) }), done: true });
      cleanup();
    }
  }

  async function restart(name: string) {
    setRestarting(name);
    await api.hqRestartWorker(name, false).catch(() => {});
    setRestarting(null);
  }

  async function addProject() {
    setAddError("");
    try {
      const r = await api.hqAddProject(addName.trim(), addPath.trim());
      if (!r.ok) {
        setAddError(t("hq.chat.addProjectError", { error: r.error ?? "?" }));
        return;
      }
      setAddName("");
      setAddPath("");
      setAddOpen(false);
      if (r.slug) setSelected(r.slug);
    } catch (e) {
      setAddError(t("hq.chat.addProjectError", { error: String(e) }));
    }
  }

  async function answer(key: string, index: number) {
    await api.hqAnswerQuestion(key, index).catch(() => {});
    setPending((prev) => prev.filter((p) => p.key !== key));
  }

  const activeThread = selected ? threads[selected] ?? [] : [];
  const activeWorker = selected ? workers[selected] : undefined;
  const activePending = pending.filter((p) => p.worker === selected);

  return (
    <div className="hq-chat-layout">
      <aside className="hq-chat-sidebar">
        {names.map((name) => {
          const w = workers[name];
          return (
            <div key={name} className={`hq-chat-row ${selected === name ? "selected" : ""}`}>
              <button className="hq-chat-row-main" onClick={() => setSelected(name)}>
                <strong>{name}</strong>
                <span className={w.alive ? (w.busy ? "hq-live" : "") : "hq-error"}>
                  {!w.alive ? t("hq.chat.stopped") : w.busy ? t("hq.chat.busy") : t("hq.chat.idle")}
                </span>
              </button>
              {!w.alive && (
                <button className="hq-chat-restart" disabled={restarting === name} onClick={() => restart(name)}>
                  {restarting === name ? t("hq.chat.restarting") : t("hq.chat.restart")}
                </button>
              )}
            </div>
          );
        })}
        {!addOpen ? (
          <button className="hq-chat-add-open" onClick={() => setAddOpen(true)}>
            + {t("hq.chat.addProject")}
          </button>
        ) : (
          <div className="hq-chat-add-form">
            <input placeholder={t("hq.chat.addProjectName")} value={addName} onChange={(e) => setAddName(e.target.value)} />
            <input placeholder={t("hq.chat.addProjectPath")} value={addPath} onChange={(e) => setAddPath(e.target.value)} />
            <button onClick={addProject}>{t("hq.chat.addProjectSubmit")}</button>
            {addError && <p className="hq-error">{addError}</p>}
          </div>
        )}
      </aside>
      <div className="hq-chat-main">
        {!selected ? (
          <p className="hq-muted hq-chat-empty">{t("hq.chat.emptyState")}</p>
        ) : (
          <>
            <p className="hq-muted hq-chat-note">{t("hq.chat.noHistoryNote")}</p>
            {activePending.map((q) => (
              <div key={q.key} className="hq-chat-question">
                <p>{t("hq.chat.pendingQuestion", { worker: q.worker })} {q.question}</p>
                <div className="hq-chat-question-options">
                  {q.options.map((opt, i) => (
                    <button key={i} onClick={() => answer(q.key, i)}>{opt}</button>
                  ))}
                </div>
              </div>
            ))}
            <div className="hq-chat-thread">
              {activeThread.map((b) => (
                <div key={b.id} className={`hq-chat-bubble ${b.role}`}>
                  {b.toolLines.map((line, i) => (
                    <div key={i} className="hq-chat-tool-line">{line}</div>
                  ))}
                  <p>{b.text || (!b.done ? "…" : "")}</p>
                </div>
              ))}
            </div>
            {activeWorker?.busy && <p className="hq-muted">{t("hq.chat.busyWarning")}</p>}
            <div className="hq-chat-input-row">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                placeholder={t("hq.chat.inputPlaceholder", { project: selected })}
                disabled={sending}
              />
              <button onClick={send} disabled={sending || !input.trim()}>
                {sending ? t("hq.chat.sending") : t("hq.chat.send")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
