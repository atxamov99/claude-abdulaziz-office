import { create } from "zustand";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { api } from "../ipc/commands";
import { t } from "../i18n";

export type ChatBubble = {
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

interface ChatState {
  /** Keyed by project slug. Module-level (survives ChatView unmounting when the
   * user switches tabs — previously this lived in ChatView's own useState, so
   * navigating away and back silently wiped the conversation). */
  threads: Record<string, ChatBubble[]>;
  selected: string | null;
  /** Per-project, not global — sending to one project shouldn't block another's input. */
  sending: Record<string, boolean>;
  setSelected: (name: string | null) => void;
  /** Starts a streamed turn for `project` and keeps updating its thread even if
   * no ChatView is currently mounted to look at it (the listeners live here,
   * not in the component, so switching tabs mid-response no longer drops it). */
  send: (project: string, prompt: string) => Promise<void>;
}

function appendBubble(threads: ChatState["threads"], project: string, bubble: ChatBubble) {
  return { ...threads, [project]: [...(threads[project] ?? []), bubble] };
}

function patchBubble(threads: ChatState["threads"], project: string, id: string, patch: Partial<ChatBubble>) {
  return {
    ...threads,
    [project]: (threads[project] ?? []).map((b) => (b.id === id ? { ...b, ...patch } : b)),
  };
}

export const useChatStore = create<ChatState>((set, get) => ({
  threads: {},
  selected: null,
  sending: {},
  setSelected: (name) => set({ selected: name }),

  async send(project, prompt) {
    if (get().sending[project]) return;
    set((s) => ({
      threads: appendBubble(s.threads, project, { id: newId(), role: "user", text: prompt, toolLines: [], done: true }),
      sending: { ...s.sending, [project]: true },
    }));
    const replyId = newId();
    set((s) => ({ threads: appendBubble(s.threads, project, { id: replyId, role: "assistant", text: "", toolLines: [], done: false }) }));
    const streamId = newId();

    const unlisten: UnlistenFn[] = [];
    const cleanup = () => {
      unlisten.forEach((fn) => fn());
      set((s) => ({ sending: { ...s.sending, [project]: false } }));
    };

    unlisten.push(
      ...(await Promise.all([
        listen<{ text: string }>(`chat://${streamId}/delta`, (e) => {
          set((s) => ({
            threads: {
              ...s.threads,
              [project]: (s.threads[project] ?? []).map((b) =>
                b.id === replyId ? { ...b, text: b.text + e.payload.text } : b
              ),
            },
          }));
        }),
        listen<{ name: string; brief: string }>(`chat://${streamId}/tool`, (e) => {
          set((s) => ({
            threads: {
              ...s.threads,
              [project]: (s.threads[project] ?? []).map((b) =>
                b.id === replyId
                  ? { ...b, toolLines: [...b.toolLines, t("hq.chat.toolLine", { name: e.payload.name, brief: e.payload.brief })] }
                  : b
              ),
            },
          }));
        }),
        listen<{ text: string }>(`chat://${streamId}/result`, (e) => {
          set((s) => ({ threads: patchBubble(s.threads, project, replyId, { text: e.payload.text, done: true }) }));
        }),
        listen<{ message: string }>(`chat://${streamId}/error`, (e) => {
          set((s) => ({
            threads: patchBubble(s.threads, project, replyId, {
              role: "error",
              text: t("hq.chat.errorPrefix", { message: e.payload.message }),
              done: true,
            }),
          }));
        }),
        listen(`chat://${streamId}/done`, () => {
          set((s) => {
            const current = (s.threads[project] ?? []).find((b) => b.id === replyId);
            if (current && !current.done) {
              return { threads: patchBubble(s.threads, project, replyId, { role: "error", text: t("hq.chat.streamError"), done: true }) };
            }
            return {};
          });
          cleanup();
        }),
      ]))
    );

    try {
      await api.hqChatSend(streamId, project, prompt);
    } catch (e) {
      set((s) => ({
        threads: patchBubble(s.threads, project, replyId, { role: "error", text: t("hq.chat.errorPrefix", { message: String(e) }), done: true }),
      }));
      cleanup();
    }
  },
}));
