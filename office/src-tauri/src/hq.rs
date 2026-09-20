use serde_json::{json, Value};
#[path = "hq_history.rs"]
mod history;
use std::{collections::HashMap, io::{BufRead, BufReader}, sync::{Mutex, OnceLock}};
use tauri::{AppHandle, Emitter};

type Cache = HashMap<String, (u64, u128, Value)>;
static CACHE: OnceLock<Mutex<Cache>> = OnceLock::new();

pub fn registered(session_id: &str) -> bool {
    dirs::home_dir().and_then(|h| std::fs::read(h.join("claude-hq/state/sessions.json")).ok())
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|v| v.as_object().map(|m| m.values().any(|s| s.as_str() == Some(session_id))))
        .unwrap_or(false)
}

fn get(path: &str) -> Result<Value, String> {
    let output = std::process::Command::new("/usr/bin/curl")
        .args(["-q", "--noproxy", "*", "-fsS", "--max-time", "5", &format!("http://127.0.0.1:8765/{path}")])
        .output().map_err(|e| e.to_string())?;
    if !output.status.success() { return Err(format!("Claude HQ недоступен: {}",String::from_utf8_lossy(&output.stderr).trim())); }
    serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())
}

fn post(path: &str, body: &str, max_time: &str) -> Result<Value, String> {
    let output = std::process::Command::new("/usr/bin/curl")
        .args(["-q", "--noproxy", "*", "-fsS", "--max-time", max_time, "-X", "POST",
               "-H", "Content-Type: application/json", "-d", body,
               &format!("http://127.0.0.1:8765/{path}")])
        .output().map_err(|e| e.to_string())?;
    if !output.status.success() { return Err(format!("Claude HQ недоступен: {}",String::from_utf8_lossy(&output.stderr).trim())); }
    serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())
}

fn usage(path: &std::path::Path) -> Value {
    let Ok(meta) = path.metadata() else { return Value::Null; };
    let stamp = meta.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|t| t.as_nanos()).unwrap_or(0);
    let key = path.to_string_lossy().to_string();
    let mut cache = CACHE.get_or_init(|| Mutex::new(HashMap::new())).lock().unwrap();
    if let Some((len, mtime, value)) = cache.get(&key) {
        if *len == meta.len() && *mtime == stamp { return value.clone(); }
    }
    let Ok(file) = std::fs::File::open(path) else { return Value::Null; };
    let mut messages: HashMap<String, Value> = HashMap::new();
    let mut dates: HashMap<String, String> = HashMap::new();
    let mut last = Value::Null;
    let mut prompt = String::new();
    let mut timestamp = Value::Null;
    for line in BufReader::new(file).lines().map_while(Result::ok) {
        let Ok(e) = serde_json::from_str::<Value>(&line) else { continue; };
        if e["timestamp"].is_string() { timestamp = e["timestamp"].clone(); }
        if e["type"] == "user" {
            let content = &e["message"]["content"];
            if let Some(s) = content.as_str() { prompt = s.chars().take(2000).collect(); }
            else if let Some(a) = content.as_array() {
                if !a.iter().any(|b| b["type"] == "tool_result") {
                    prompt = a.iter().filter_map(|b| b["text"].as_str()).collect::<Vec<_>>().join("\n").chars().take(2000).collect();
                }
            }
        }
        if e["type"] != "assistant" || !e["message"]["usage"].is_object() { continue; }
        let u = e["message"]["usage"].clone();
        let id = e["message"]["id"].as_str().or(e["uuid"].as_str()).map(str::to_string);
        if let Some(id) = id {
            if let Some(date) = e["timestamp"].as_str().and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok()) {
                dates.entry(id.clone()).or_insert_with(||date.with_timezone(&chrono::Utc).format("%Y-%m-%d").to_string());
            }
            messages.insert(id, u.clone());
        }
        last = u;
    }
    let fields = ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"];
    let mut totals = serde_json::Map::new();
    for field in fields { totals.insert(field.into(), json!(messages.values().map(|u| u[field].as_u64().unwrap_or(0)).sum::<u64>())); }
    let context: u64 = fields[..3].iter().map(|k| last[*k].as_u64().unwrap_or(0)).sum();
    let mut days = std::collections::BTreeMap::<String, Value>::new();
    for (id,u) in &messages {
        if let Some(date) = dates.get(id) {
            let day = days.entry(date.clone()).or_insert_with(||json!({"requests":0}));
            day["requests"] = json!(day["requests"].as_u64().unwrap_or(0)+1);
            for field in fields { day[field] = json!(day[field].as_u64().unwrap_or(0)+u[field].as_u64().unwrap_or(0)); }
        }
    }
    let value = json!({"days":days,"totals":totals,"last":last,"context":context,"requests":messages.len(),"prompt":prompt,"last_event":timestamp});
    cache.insert(key, (meta.len(), stamp, value.clone()));
    value
}

#[tauri::command]
pub async fn get_hq_snapshot() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let mut projects = get("projects")?;
        let home = dirs::home_dir().ok_or("Home unavailable")?;
        if let Some(workers) = projects["workers"].as_object_mut() {
            for (_, worker) in workers.iter_mut() {
                if let Some(sid) = worker["session"].as_str().filter(|s| s.chars().all(|c| c.is_ascii_hexdigit() || c == '-')) {
                    let pattern = format!("{}/.claude/projects/*/{sid}.jsonl", home.display());
                    if let Some(path) = glob::glob(&pattern).ok().and_then(|g| g.flatten().next()) {
                        worker["usage"] = usage(&path);
                    }
                }
            }
        }
        match get("tasks") {
            Ok(tasks) => projects["tasks"] = tasks["tasks"].clone(),
            Err(error) => projects["tasks_error"] = json!(error),
        }
        match get("office") {
            Ok(office) => projects["office"] = office,
            Err(_) => projects["office_error"] = json!("Учёт ходов HQ пока недоступен"),
        }
        let history_path = dirs::data_dir().ok_or("Data directory unavailable")?.join("dev.abdulaziz.office/hq-history.json");
        match history::observe(&history_path, &projects) {
            Ok(history) => projects["history"] = history,
            Err(error) => projects["history_error"] = json!(error),
        }
        Ok(projects)
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn hq_add_project(name: String, path: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let body = json!({"name": name, "path": path}).to_string();
        post("projects/add", &body, "5")
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn hq_restart_worker(project: String, fresh: bool) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let body = json!({"project": project, "fresh": fresh}).to_string();
        post("workers/restart", &body, "30")
    }).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn hq_pending_questions() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| get("ask/pending")).await.map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn hq_answer_question(key: String, index: i32) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let body = json!({"key": key, "index": index}).to_string();
        post("ask/answer", &body, "5")
    }).await.map_err(|e| e.to_string())?
}

/// Parses one line of an SSE stream against carried-over parser state (the `event:`
/// line seen since the last completed frame). Returns the completed `(kind, payload)`
/// once the matching `data:` line arrives; a malformed/non-JSON data line is dropped
/// (state still clears, so the stream can recover on the next `event:`).
fn sse_line(line: &str, event_kind: &mut Option<String>) -> Option<(String, Value)> {
    if let Some(kind) = line.strip_prefix("event: ") {
        *event_kind = Some(kind.to_string());
        None
    } else if let Some(data) = line.strip_prefix("data: ") {
        let kind = event_kind.take()?;
        serde_json::from_str::<Value>(data).ok().map(|payload| (kind, payload))
    } else {
        None
    }
}

/// Streams one chat turn from the Office Chat tab: spawns `curl -N` against the
/// HQ SSE endpoint and re-emits each frame as a Tauri event scoped by `stream_id`,
/// so the frontend can listen before this command's promise even resolves (it
/// returns as soon as the background thread is spawned, not when the turn ends).
/// Mirrors `watcher::spawn`'s own "OS thread -> app.emit" bridging pattern.
#[tauri::command]
pub async fn hq_chat_send(app: AppHandle, stream_id: String, project: String, prompt: String) -> Result<(), String> {
    std::thread::spawn(move || {
        let emit = |kind: &str, payload: Value| {
            let _ = app.emit(&format!("chat://{stream_id}/{kind}"), payload);
        };
        let body = json!({"project": project, "prompt": prompt}).to_string();
        let child = std::process::Command::new("/usr/bin/curl")
            .args(["-N", "-sS", "-q", "--noproxy", "*", "-X", "POST",
                   "-H", "Content-Type: application/json", "-d", &body,
                   "http://127.0.0.1:8765/chat/stream"])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn();
        let mut child = match child {
            Ok(c) => c,
            Err(e) => {
                emit("error", json!({"message": e.to_string()}));
                emit("done", json!({}));
                return;
            }
        };
        if let Some(stdout) = child.stdout.take() {
            let mut event_kind: Option<String> = None;
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Some((kind, payload)) = sse_line(&line, &mut event_kind) {
                    emit(&kind, payload);
                }
            }
        }
        let _ = child.wait();
        emit("done", json!({}));
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sse_line_parses_event_then_data() {
        let mut state = None;
        assert_eq!(sse_line("event: delta", &mut state), None);
        assert_eq!(state.as_deref(), Some("delta"));
        let frame = sse_line("data: {\"text\":\"hi\"}", &mut state);
        assert_eq!(frame, Some(("delta".to_string(), json!({"text": "hi"}))));
        assert_eq!(state, None); // consumed by the completed frame
    }
    #[test]
    fn sse_line_ignores_blank_and_unprefixed_lines() {
        let mut state = Some("tool".to_string());
        assert_eq!(sse_line("", &mut state), None);
        assert_eq!(sse_line("not a frame", &mut state), None);
        assert_eq!(state.as_deref(), Some("tool")); // untouched by non-matching lines
    }
    #[test]
    fn sse_line_drops_data_with_no_preceding_event() {
        let mut state = None;
        assert_eq!(sse_line("data: {\"text\":\"orphan\"}", &mut state), None);
    }
    #[test]
    fn sse_line_drops_malformed_json_but_clears_state() {
        let mut state = Some("delta".to_string());
        assert_eq!(sse_line("data: not-json", &mut state), None);
        assert_eq!(state, None);
    }
    #[test]
    fn usage_deduplicates_message_fragments_and_tracks_cache() {
        let path = std::env::temp_dir().join(format!("hq-usage-{}.jsonl",std::process::id()));
        let a = json!({"type":"assistant","timestamp":"2026-09-17T00:00:00Z","message":{"id":"same","usage":{"input_tokens":2,"cache_creation_input_tokens":10,"cache_read_input_tokens":100,"output_tokens":5}}});
        std::fs::write(&path,format!("{a}\n{a}\n")).unwrap();
        let result=usage(&path);
        assert_eq!(result["requests"],1);
        assert_eq!(result["context"],112);
        assert_eq!(result["totals"]["output_tokens"],5);
        assert_eq!(result["totals"]["cache_read_input_tokens"],100);
        assert_eq!(result["days"]["2026-09-17"]["requests"],1);
        assert_eq!(result["days"]["2026-09-17"]["output_tokens"],5);
        let mut updated = a.clone();
        updated["timestamp"] = json!("2026-09-18T00:00:00Z");
        updated["message"]["usage"]["output_tokens"] = json!(12);
        std::fs::write(&path,format!("{a}\n{updated}\n")).unwrap();
        let result=usage(&path);
        assert_eq!(result["days"]["2026-09-17"]["output_tokens"],12);
        assert!(result["days"].get("2026-09-18").is_none());
        std::fs::remove_file(path).unwrap();
    }
}
