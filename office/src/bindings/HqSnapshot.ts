import type { TurnTelemetry } from "../components/HqTurns";
import type { History } from "../components/HqHistory";

export type HqUsage = {
  context: number;
  requests: number;
  prompt: string;
  last_event: string;
  totals: Record<string, number>;
  last: Record<string, number> | null;
};

export type HqWorker = {
  alive: boolean;
  busy: boolean;
  turns: number;
  cwd: string;
  session: string | null;
  usage?: HqUsage;
  last_activity?: number;
};

export type HqTask = {
  id: string;
  project: string;
  prompt: string;
  status: string;
  started: number;
  finished?: number;
  steps: number;
  error?: string;
};

export type HqSnapshot = {
  workers: Record<string, HqWorker>;
  tasks: HqTask[];
  history?: History;
  history_error?: string;
  tasks_error?: string;
  office?: TurnTelemetry;
  office_error?: string;
};
