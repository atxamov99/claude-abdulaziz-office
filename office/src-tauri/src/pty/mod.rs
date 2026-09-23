//! A real PTY that lets the Terminal tab show a live agent terminal inside the app.
//!
//! The HQ bot (hq/terminal_worker.py) already runs every agent as an interactive
//! `claude` TUI inside a tmux session named `hq-<worker>`, and reads replies back
//! with `tmux capture-pane`. So this module does not spawn `claude` itself — it
//! opens a PTY running `tmux attach`, which means Office and Telegram look at the
//! very same session and neither side breaks the other.
//!
//! The pure helpers here (session naming, tmux lookup, attach argv) are unit
//! tested; the PTY plumbing itself is exercised by running the app.

use crate::error::{AppError, AppResult};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};

/// Tauri event carrying a chunk of terminal output (payload: `PtyChunk`).
pub const EVENT_DATA: &str = "pty://data";
/// Tauri event fired once when a PTY ends (payload: `PtyExit`).
pub const EVENT_EXIT: &str = "pty://exit";

/// Where tmux usually lives. The app is launched from Finder, not a login shell,
/// so PATH is minimal (/usr/bin:/bin:...) and a bare `tmux` would not resolve —
/// the same reason terminal_cmd.rs calls binaries by absolute path.
const TMUX_CANDIDATES: [&str; 3] = [
    "/opt/homebrew/bin/tmux", // Apple Silicon Homebrew
    "/usr/local/bin/tmux",    // Intel Homebrew
    "/usr/bin/tmux",
];

#[derive(Debug, Clone, serde::Serialize)]
pub struct PtyChunk {
    pub id: String,
    /// Raw terminal output, including ANSI escapes — xterm.js on the frontend renders it.
    pub data: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PtyExit {
    pub id: String,
    pub code: i32,
}

/// One live PTY. The reader lives in its own thread; this struct keeps the ends
/// needed to write, resize and kill.
struct PtySession {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn Child + Send + Sync>,
}

/// All open PTYs, keyed by the id the frontend chose (we use the worker name).
#[derive(Default)]
pub struct PtyRegistry {
    sessions: Mutex<HashMap<String, PtySession>>,
}

/// tmux session name for a worker — must match `TerminalWorker._session` in
/// hq/terminal_worker.py (`re.sub(r"[^a-zA-Z0-9_-]", "-", name)`, prefixed `hq-`),
/// otherwise Office would attach to a session that does not exist.
pub fn tmux_session_name(worker: &str) -> String {
    let safe: String = worker
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '-' })
        .collect();
    format!("hq-{safe}")
}

/// First tmux binary that exists on disk, or None when tmux is not installed.
pub fn resolve_tmux_in(candidates: &[&str]) -> Option<PathBuf> {
    candidates.iter().map(PathBuf::from).find(|p| p.exists())
}

pub fn resolve_tmux() -> Option<PathBuf> {
    resolve_tmux_in(&TMUX_CANDIDATES)
}

/// argv for attaching to an existing session.
///
/// `-d` detaches any other client first. That is deliberate: tmux sizes a shared
/// session to its smallest client, so an old Terminal.app window left attached
/// would squeeze the Office view down to its width. Detaching it keeps the
/// session alive — only that stale view goes away.
pub fn attach_args(session: &str) -> Vec<String> {
    vec![
        "attach-session".to_string(),
        "-t".to_string(),
        session.to_string(),
        "-d".to_string(),
    ]
}

/// Whether the tmux session exists (i.e. the agent is actually running).
pub fn tmux_has_session(tmux: &Path, session: &str) -> bool {
    Command::new(tmux)
        .args(["has-session", "-t", session])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

impl PtyRegistry {
    /// Opens a PTY attached to the worker's tmux session and starts streaming it.
    ///
    /// `on_data` / `on_exit` are called from the reader thread; the command layer
    /// passes closures that emit the Tauri events.
    pub fn open<D, X>(
        &self,
        id: String,
        session: String,
        cols: u16,
        rows: u16,
        on_data: D,
        on_exit: X,
    ) -> AppResult<()>
    where
        D: Fn(PtyChunk) + Send + 'static,
        X: Fn(PtyExit) + Send + 'static,
    {
        let tmux = resolve_tmux().ok_or_else(|| {
            AppError::Other("tmux not found (looked in /opt/homebrew/bin, /usr/local/bin, /usr/bin)".into())
        })?;
        if !tmux_has_session(&tmux, &session) {
            return Err(AppError::Other(format!(
                "tmux session '{session}' is not running — start the agent from the Chat tab first"
            )));
        }

        // Reopening the same worker replaces the old PTY rather than leaking it.
        self.close(&id);

        let pair = native_pty_system()
            .openpty(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
            .map_err(|e| AppError::Other(format!("failed to open pty: {e}")))?;

        let mut cmd = CommandBuilder::new(&tmux);
        for arg in attach_args(&session) {
            cmd.arg(arg);
        }
        // tmux itself renders in 256 colors; without TERM it falls back to a dumb
        // terminal and the Claude TUI loses its box drawing.
        cmd.env("TERM", "xterm-256color");

        let child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| AppError::Other(format!("failed to spawn tmux: {e}")))?;
        // The slave end must be dropped here, otherwise the PTY never reports EOF
        // when tmux exits and the reader thread would hang forever.
        drop(pair.slave);

        let reader = pair
            .master
            .try_clone_reader()
            .map_err(|e| AppError::Other(format!("failed to read pty: {e}")))?;
        let writer = pair
            .master
            .take_writer()
            .map_err(|e| AppError::Other(format!("failed to write pty: {e}")))?;

        let exit_id = id.clone();
        std::thread::spawn(move || {
            let mut reader = reader;
            let mut buf = [0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => on_data(PtyChunk {
                        id: exit_id.clone(),
                        // The TUI emits UTF-8; a chunk can split a multi-byte char,
                        // so replace rather than fail (xterm.js stitches the rest).
                        data: String::from_utf8_lossy(&buf[..n]).into_owned(),
                    }),
                }
            }
            on_exit(PtyExit { id: exit_id, code: 0 });
        });

        self.sessions.lock().unwrap().insert(
            id,
            PtySession { master: pair.master, writer, child },
        );
        Ok(())
    }

    /// Sends keystrokes typed in the Terminal tab to the agent.
    pub fn write(&self, id: &str, data: &str) -> AppResult<()> {
        let mut map = self.sessions.lock().unwrap();
        let s = map
            .get_mut(id)
            .ok_or_else(|| AppError::Other(format!("terminal '{id}' is not open")))?;
        s.writer
            .write_all(data.as_bytes())
            .map_err(|e| AppError::Other(format!("failed to write to terminal: {e}")))?;
        s.writer
            .flush()
            .map_err(|e| AppError::Other(format!("failed to flush terminal: {e}")))
    }

    /// Follows the window size, so the TUI re-wraps to the panel's real width.
    pub fn resize(&self, id: &str, cols: u16, rows: u16) -> AppResult<()> {
        let map = self.sessions.lock().unwrap();
        let s = map
            .get(id)
            .ok_or_else(|| AppError::Other(format!("terminal '{id}' is not open")))?;
        s.master
            .resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
            .map_err(|e| AppError::Other(format!("failed to resize terminal: {e}")))
    }

    /// Kills the tmux *client* for this view. The session (and the agent in it)
    /// keeps running — that is what makes closing the tab harmless.
    pub fn close(&self, id: &str) {
        if let Some(mut s) = self.sessions.lock().unwrap().remove(id) {
            let _ = s.child.kill();
            let _ = s.child.wait();
        }
    }
}

/// Registry handle stored in Tauri's managed state.
pub type SharedPty = Arc<PtyRegistry>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_name_matches_python_worker() {
        // Mirrors TerminalWorker._session in hq/terminal_worker.py.
        assert_eq!(tmux_session_name("hq"), "hq-hq");
        assert_eq!(tmux_session_name("asd"), "hq-asd");
        assert_eq!(tmux_session_name("my_proj-1"), "hq-my_proj-1");
    }

    #[test]
    fn session_name_replaces_unsafe_chars() {
        // Spaces, dots and slashes are all illegal in a tmux target name.
        assert_eq!(tmux_session_name("Shtab Files"), "hq-Shtab-Files");
        assert_eq!(tmux_session_name("a.b/c"), "hq-a-b-c");
    }

    #[test]
    fn attach_args_detach_other_clients() {
        assert_eq!(
            attach_args("hq-x"),
            vec!["attach-session", "-t", "hq-x", "-d"]
        );
    }

    #[test]
    fn resolve_tmux_picks_first_existing() {
        // /usr/bin/env exists everywhere; the bogus path before it must be skipped.
        let found = resolve_tmux_in(&["/nope/tmux", "/usr/bin/env"]);
        assert_eq!(found, Some(PathBuf::from("/usr/bin/env")));
    }

    #[test]
    fn resolve_tmux_none_when_missing() {
        assert_eq!(resolve_tmux_in(&["/nope/tmux", "/also/nope"]), None);
    }

    /// End-to-end over a real tmux session: open a PTY, type into it, and read
    /// the text back out of the pane. Ignored by default because it needs tmux
    /// installed; run with `cargo test -- --ignored pty_write`.
    #[test]
    #[ignore]
    fn pty_write_reaches_the_tmux_session() {
        use std::sync::mpsc;
        use std::time::Duration;

        let Some(tmux) = resolve_tmux() else { return };
        let session = "hq-ptywritetest";
        // Own scratch session, so no real agent session is ever touched.
        let _ = Command::new(&tmux).args(["kill-session", "-t", session]).status();
        let ok = Command::new(&tmux)
            .args(["new-session", "-d", "-s", session, "cat"])
            .status()
            .expect("tmux new-session");
        assert!(ok.success(), "could not create the test session");

        let reg = PtyRegistry::default();
        let (tx, rx) = mpsc::channel();
        reg.open(
            "ptywritetest".into(),
            session.into(),
            80,
            24,
            move |chunk| {
                let _ = tx.send(chunk.data);
            },
            |_| {},
        )
        .expect("pty open");

        // Give tmux a moment to attach and paint before typing.
        std::thread::sleep(Duration::from_millis(700));
        reg.write("ptywritetest", "HELLO-FROM-PTY\n").expect("pty write");

        // `cat` echoes it back, so it must land in the pane the agent would see.
        let mut found = false;
        for _ in 0..20 {
            std::thread::sleep(Duration::from_millis(200));
            let out = Command::new(&tmux)
                .args(["capture-pane", "-p", "-t", session])
                .output()
                .expect("capture-pane");
            if String::from_utf8_lossy(&out.stdout).contains("HELLO-FROM-PTY") {
                found = true;
                break;
            }
        }

        // Anything typed in the app must also be visible to the streamed output.
        let streamed: String = rx.try_iter().collect();

        reg.close("ptywritetest");
        let _ = Command::new(&tmux).args(["kill-session", "-t", session]).status();

        assert!(found, "text typed through the PTY never reached the tmux pane");
        assert!(
            !streamed.is_empty(),
            "no output was streamed back to the frontend"
        );
    }
}
