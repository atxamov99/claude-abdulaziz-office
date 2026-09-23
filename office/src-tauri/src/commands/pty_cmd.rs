use crate::error::AppResult;
use crate::pty::{self, SharedPty};
use tauri::{AppHandle, Emitter, State};

/// Opens the Terminal tab's view onto a worker: a PTY attached to its tmux session.
/// Output arrives on the frontend as `pty://data` events, the end as `pty://exit`.
#[tauri::command]
pub async fn pty_open(
    app: AppHandle,
    registry: State<'_, SharedPty>,
    worker: String,
    cols: u16,
    rows: u16,
) -> AppResult<String> {
    let session = pty::tmux_session_name(&worker);
    let data_app = app.clone();
    let exit_app = app.clone();
    registry.open(
        worker.clone(),
        session.clone(),
        cols,
        rows,
        move |chunk| {
            let _ = data_app.emit(pty::EVENT_DATA, chunk);
        },
        move |exit| {
            let _ = exit_app.emit(pty::EVENT_EXIT, exit);
        },
    )?;
    Ok(session)
}

/// Keystrokes from the Terminal tab (already encoded by xterm.js, escapes included).
#[tauri::command]
pub async fn pty_write(registry: State<'_, SharedPty>, worker: String, data: String) -> AppResult<()> {
    registry.write(&worker, &data)
}

#[tauri::command]
pub async fn pty_resize(
    registry: State<'_, SharedPty>,
    worker: String,
    cols: u16,
    rows: u16,
) -> AppResult<()> {
    registry.resize(&worker, cols, rows)
}

/// Detaches this view. The agent's tmux session keeps running.
#[tauri::command]
pub async fn pty_close(registry: State<'_, SharedPty>, worker: String) -> AppResult<()> {
    registry.close(&worker);
    Ok(())
}
