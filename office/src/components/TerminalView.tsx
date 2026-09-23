import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { listen } from "@tauri-apps/api/event";
import "@xterm/xterm/css/xterm.css";
import { api } from "../ipc/commands";
import type { useHqMonitor } from "./HqOffice";
import { useT } from "../i18n";
import "./hq.css";

/** Payload of the `pty://data` / `pty://exit` events emitted by src-tauri/src/pty. */
type PtyChunk = { id: string; data: string };
type PtyExit = { id: string; code: number };

/** Matches the app's dark chrome; xterm ships a black-on-white default otherwise. */
const THEME = {
  background: "#14161c",
  foreground: "#e6e6e6",
  cursor: "#ffbe80",
  selectionBackground: "#3a4152",
};

/**
 * Live agent terminal, inside the app.
 *
 * Every worker already runs as an interactive `claude` TUI in a tmux session
 * (hq/terminal_worker.py); here we attach a real PTY to it, so this panel and
 * Telegram drive the very same session — typing here is the same as typing in
 * the terminal window that used to open separately.
 */
export function TerminalView({ monitor }: { monitor: ReturnType<typeof useHqMonitor> }) {
  const t = useT();
  const workers = monitor.snapshot?.workers ?? {};
  const names = Object.keys(workers).sort();
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [ended, setEnded] = useState(false);
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!selected && names.length) setSelected(names[0]);
  }, [names, selected]);

  useEffect(() => {
    const host = hostRef.current;
    if (!selected || !host) return;

    let disposed = false;
    setError("");
    setEnded(false);

    const term = new Terminal({
      fontSize: 13,
      fontFamily: "Menlo, Monaco, 'Courier New', monospace",
      theme: THEME,
      cursorBlink: true,
      scrollback: 5000,
      // tmux redraws the whole screen itself; letting xterm convert eol saves
      // nothing and breaks TUI box drawing.
      convertEol: false,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    // Opening the tab should be enough to start typing — without this the first
    // keystrokes go nowhere until the user clicks inside the panel.
    term.focus();

    // Keystrokes (already escape-encoded by xterm) go straight to the PTY.
    const typed = term.onData((data) => {
      api.ptyWrite(selected, data).catch((e) => setError(String(e)));
    });

    const unlisten: Array<() => void> = [];
    (async () => {
      const offData = await listen<PtyChunk>("pty://data", (ev) => {
        if (ev.payload.id === selected) term.write(ev.payload.data);
      });
      const offExit = await listen<PtyExit>("pty://exit", (ev) => {
        if (ev.payload.id === selected) setEnded(true);
      });
      if (disposed) {
        offData();
        offExit();
        return;
      }
      unlisten.push(offData, offExit);

      try {
        await api.ptyOpen(selected, term.cols, term.rows);
      } catch (e) {
        // Most common case: the agent is not running, so there is no tmux session.
        if (!disposed) setError(String(e));
      }
    })();

    // Re-wrap the TUI whenever the panel changes size (window resize, sidebar, …).
    const ro = new ResizeObserver(() => {
      if (disposed) return;
      fit.fit();
      api.ptyResize(selected, term.cols, term.rows).catch(() => {});
    });
    ro.observe(host);

    return () => {
      disposed = true;
      ro.disconnect();
      typed.dispose();
      unlisten.forEach((off) => off());
      // Closes only this view's tmux client — the agent's session keeps running.
      api.ptyClose(selected).catch(() => {});
      term.dispose();
    };
  }, [selected]);

  return (
    <div className="hq-chat-layout">
      <aside className="hq-chat-sidebar">
        <h3>{t("hq.terminal.heading")}</h3>
        {names.length === 0 && <p className="hq-muted">{t("hq.terminal.noWorkers")}</p>}
        {names.map((name) => (
          <button
            key={name}
            className={`hq-terminal-project ${selected === name ? "active" : ""}`}
            onClick={() => setSelected(name)}
          >
            {name}
          </button>
        ))}
      </aside>
      <section className="hq-terminal-main">
        {!selected && <p className="hq-muted">{t("hq.terminal.emptyState")}</p>}
        {error && <p className="hq-error">{error}</p>}
        {ended && !error && <p className="hq-muted">{t("hq.terminal.detached")}</p>}
        <div className="hq-terminal-host" ref={hostRef} />
      </section>
    </div>
  );
}
