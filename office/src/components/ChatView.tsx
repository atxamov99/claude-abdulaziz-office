import { useEffect, useState } from "react";
import { api, type PendingQuestion } from "../ipc/commands";
import type { useHqMonitor } from "./HqOffice";
import { useChatStore } from "../stores/chatStore";
import { t } from "../i18n";
import "./hq.css";

export function ChatView({ monitor }: { monitor: ReturnType<typeof useHqMonitor> }) {
  const workers = monitor.snapshot?.workers ?? {};
  const names = Object.keys(workers).sort();
  const threads = useChatStore((s) => s.threads);
  const selected = useChatStore((s) => s.selected);
  const setSelected = useChatStore((s) => s.setSelected);
  const sending = useChatStore((s) => (selected ? s.sending[selected] ?? false : false));
  const send = useChatStore((s) => s.send);
  const [input, setInput] = useState("");
  const [restarting, setRestarting] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingQuestion[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addError, setAddError] = useState("");

  useEffect(() => {
    if (!selected && names.length) setSelected(names[0]);
  }, [names, selected, setSelected]);

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

  async function handleSend() {
    const project = selected;
    const prompt = input.trim();
    if (!project || !prompt || sending) return;
    setInput("");
    await send(project, prompt);
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
                    handleSend();
                  }
                }}
                placeholder={t("hq.chat.inputPlaceholder", { project: selected })}
                disabled={sending}
              />
              <button onClick={handleSend} disabled={sending || !input.trim()}>
                {sending ? t("hq.chat.sending") : t("hq.chat.send")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
