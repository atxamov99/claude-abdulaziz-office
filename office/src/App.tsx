import { useEffect, useState } from "react";
import { HqOffice, useHqMonitor } from "./components/HqOffice";
import { ReplayView } from "./replay/ReplayView";
import { useConfigStore } from "./stores/configStore";
import { useScopedConfigStore } from "./stores/scopedConfigStore";
import { useEffectiveHooksStore } from "./stores/effectiveHooksStore";
import { useUiPrefsStore } from "./stores/uiPrefsStore";
import { AgentsManager } from "./components/AgentsManager";
import { HooksManager } from "./components/HooksManager";
import { SkillsManager } from "./components/SkillsManager";
import { Settings } from "./components/Settings";
import { MetricsDashboard } from "./components/MetricsDashboard";
import { UpdateBanner } from "./components/UpdateBanner";
import { useT, type MessageKey } from "./i18n";

type Tab = "office" | "replay" | "metrics" | "agents" | "hooks" | "skills" | "settings";

// office/settings are special-cased below (hq.tabs.office / hq.tabs.settings); the rest read their key here.
const TABS: { id: Tab; labelKey: MessageKey | null }[] = [
  { id: "office", labelKey: null },
  { id: "replay", labelKey: "hq.tabs.replay" },
  { id: "metrics", labelKey: "hq.tabs.metricsPark" },
  { id: "agents", labelKey: "hq.tabs.agents" },
  { id: "hooks", labelKey: "hq.tabs.hooks" },
  { id: "skills", labelKey: "hq.tabs.skills" },
  { id: "settings", labelKey: null },
];

export function App() {
  const monitor = useHqMonitor();
  const t = useT();
  const [tab, setTab] = useState<Tab>("office");
  const loadConfig = useConfigStore((s) => s.loadAll);
  const watchConfig = useConfigStore((s) => s.watch);
  const watchScoped = useScopedConfigStore((s) => s.watch);
  const watchEffective = useEffectiveHooksStore((s) => s.watch);
  const configError = useConfigStore((s) => s.error);

  useEffect(() => {
    // On startup: begin subscribing and fetch the initial state, load config, and watch the CLI for changes.
    // Metrics are heavy (a full scan of all projects), so we don't fetch them on startup;
    // they are lazy-loaded the first time the Metrics tab is opened (via ensureLoaded in MetricsDashboard).
    loadConfig();
    watchConfig();
    // Project-scoped config and effective hooks also follow CLI-side changes live (incl. project .claude).
    watchScoped();
    watchEffective();
    // Silently check GitHub Releases for a newer version (shows a banner only if one exists).
    // The tray icon itself lives in Rust and starts out hidden; sync it to the
    // persisted Settings preference (default: shown). Routed through the store's
    // own setter (rather than calling the IPC command directly) so there's exactly
    // one place that applies this preference to the real tray icon.
    const prefs = useUiPrefsStore.getState();
    prefs.setTrayEnabled(prefs.trayEnabled);
  }, [loadConfig, watchConfig, watchScoped, watchEffective]);

  return (
    <div className="app">
      <div className="tabbar" data-tauri-drag-region>
        <span className="brand" data-tauri-drag-region>Claude — Abdulaziz Office</span>
        {TABS.map((item) => (
          <button
            key={item.id}
            className={`tab ${tab === item.id ? "active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            {item.id === "office"
              ? t("hq.tabs.office")
              : item.id === "settings"
                ? t("hq.tabs.settings")
                : t(item.labelKey!)}
          </button>
        ))}
      </div>
      <UpdateBanner />
      <div className="content">
        {tab !== "office" && monitor.error && <p className="hq-error">{t("hq.connectionLost", {time: monitor.updated || t("hq.noneFallback")})}</p>}
        {tab === "office" && <HqOffice monitor={monitor}/>}
        {tab === "replay" && <ReplayView />}
        {tab === "metrics" && <><p style={{padding:"10px 24px",color:"#ffbe80"}}>{t("hq.metricsBanner")}</p><MetricsDashboard /></>}
        {tab === "agents" && <AgentsManager />}
        {tab === "hooks" && <HooksManager />}
        {tab === "skills" && <SkillsManager />}
        {tab === "settings" && <Settings />}
        {configError && tab !== "office" && (
          <div className="err" style={{ padding: "0 24px" }}>
            {t("app.configError", { error: configError })}
          </div>
        )}
      </div>
    </div>
  );
}
