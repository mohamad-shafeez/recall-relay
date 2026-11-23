export interface HealthChecks {
  api: "healthy" | "degraded";
  database: "healthy" | "degraded";
}

export interface HealthResponse {
  status: "ok" | "degraded";
  service: string;
  version: string;
  environment: string;
  demo_mode: boolean;
  timestamp: string;
  uptime_seconds: number;
  checks: HealthChecks;
}

export interface HealthCheckResult {
  reachable: boolean;
  latencyMs: number | null;
  checkedAt: string;
  data: HealthResponse | null;
  errorMessage: string | null;
}
