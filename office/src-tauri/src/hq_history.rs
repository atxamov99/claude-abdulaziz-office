//! Local observation history. Polling times are NOT exact worker transition times.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path, sync::Mutex, io::Write};

static LOCK: Mutex<()> = Mutex::new(());

#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct History {
    version: u32,
    since: String,
    workers: BTreeMap<String, Value>,
    sessions: BTreeMap<String, Value>,
    tasks: BTreeMap<String, Value>,
    events: Vec<Value>,
}

impl History {
    fn update(&mut self, snapshot: &Value, now: &str) {
        self.version = 1;
        if self.since.is_empty() { self.since = now.into(); }
        if let Some(workers) = snapshot["workers"].as_object() {
            let mut next = BTreeMap::new();
            for (name,w) in workers {
                let state = json!({"alive":w["alive"],"busy":w["busy"],"session":w["session"]});
                if self.workers.get(name) != Some(&state) {
                    self.events.push(json!({"at":now,"project":name,"kind":if self.workers.contains_key(name){"worker"}else{"discovered"},"before":self.workers.get(name),"after":state}));
                }
                next.insert(name.clone(), state);
                if let Some(sid) = w["session"].as_str().filter(|s|!s.is_empty()) {
                    if w["usage"]["days"].is_object() {
                        self.sessions.insert(sid.into(),json!({"project":name,"days":w["usage"]["days"]}));
                    }
                }
            }
            for name in self.workers.keys().filter(|name|!next.contains_key(*name)) {
                self.events.push(json!({"at":now,"project":name,"kind":"missing"}));
            }
            self.workers = next;
        }
        if let Some(tasks) = snapshot["tasks"].as_array() {
            for t in tasks {
                let Some(id) = t["id"].as_str() else { continue; };
                let mut task = t.clone();
                // HQ's elapsed grows even for completed tasks. Never present it as duration.
                task.as_object_mut().unwrap().remove("elapsed");
                if self.tasks.get(id).map(|old|&old["status"]) != Some(&task["status"]) {
                    self.events.push(json!({"at":now,"project":task["project"],"kind":"task","id":id,"status":task["status"],"initial":!self.tasks.contains_key(id)}));
                }
                self.tasks.insert(id.into(),task);
            }
        }
        if self.events.len()>1000 { self.events.drain(..self.events.len()-1000); }
        while self.tasks.len()>500 {
            let oldest = self.tasks.iter().min_by(|(_,a),(_,b)|a["started"].as_f64().unwrap_or(0.).total_cmp(&b["started"].as_f64().unwrap_or(0.))).map(|(id,_)|id.clone()).unwrap();
            self.tasks.remove(&oldest);
        }
    }
}

pub fn observe(path: &Path, snapshot: &Value) -> Result<Value,String> {
    let _guard = LOCK.lock().map_err(|e|e.to_string())?;
    let mut history: History = match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e|format!("История повреждена, файл сохранён без изменений: {e}"))?,
        Err(e) if e.kind()==std::io::ErrorKind::NotFound => History::default(),
        Err(e) => return Err(e.to_string()),
    };
    let before = serde_json::to_vec(&history).map_err(|e|e.to_string())?;
    history.update(snapshot,&chrono::Utc::now().to_rfc3339());
    let bytes = serde_json::to_vec(&history).map_err(|e|e.to_string())?;
    if before != bytes {
        std::fs::create_dir_all(path.parent().ok_or("Invalid history path")?).map_err(|e|e.to_string())?;
        let tmp = path.with_extension("tmp");
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)] {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&tmp).map_err(|e|e.to_string())?;
        file.write_all(&bytes).and_then(|_|file.sync_all()).map_err(|e|e.to_string())?;
        std::fs::rename(tmp,path).map_err(|e|e.to_string())?;
    }
    serde_json::to_value(history).map_err(|e|e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn transitions_are_deduplicated_and_sessions_retained() {
        let mut h = History::default();
        let mut s = json!({"workers":{"hq":{"alive":true,"busy":false,"session":"a","usage":{"days":{"2026-09-17":{"output_tokens":3}}}}}});
        h.update(&s,"a"); h.update(&s,"b");
        assert_eq!(h.events.len(),1);
        s["workers"]["hq"]["busy"]=json!(true);
        h.update(&s,"c"); assert_eq!(h.events.len(),2);
        s["workers"]["hq"]["session"]=json!("b");
        h.update(&s,"d"); assert_eq!(h.sessions.len(),2);
        h.update(&json!({"workers":{}}),"e");
        assert_eq!(h.events.last().unwrap()["kind"],"missing");
    }
    #[test]
    fn task_elapsed_does_not_change_history_and_missing_tasks_are_retained() {
        let mut h = History::default();
        h.update(&json!({"tasks":[{"id":"x","status":"done","elapsed":10}]}),"a");
        h.update(&json!({"tasks":[{"id":"x","status":"done","elapsed":20}]}),"b");
        h.update(&json!({"tasks":[]}),"c");
        assert_eq!(h.events.len(),1);
        assert_eq!(h.tasks.len(),1);
        assert!(h.tasks["x"].get("elapsed").is_none());
    }
    #[test]
    fn persistence_roundtrip_and_corrupt_file_preserved() {
        let path=std::env::temp_dir().join(format!("hq-history-{}.json",std::process::id()));
        let snapshot=json!({"workers":{"hq":{"alive":true,"busy":false,"session":"x"}}});
        let a=observe(&path,&snapshot).unwrap();
        let b=observe(&path,&snapshot).unwrap();
        assert_eq!(a,b);
        std::fs::write(&path,"invalid").unwrap();
        assert!(observe(&path,&snapshot).is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(),"invalid");
        std::fs::remove_file(path).unwrap();
    }
}
