import path from "node:path";

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const TOOL_NAME = "boss_hr_runner";
const COMMAND_NAME = "boss_hr_drain";
const DEFAULT_TIMEOUT_MS = 20 * 60 * 1000;

type RunnerArgs = {
  command?: string;
};

function normalizeRawCommand(rawCommand?: string): string {
  const trimmed = (rawCommand || "").trim();
  return trimmed || "drain-unread";
}

function resolveGatewayPort(config: Record<string, unknown>): number {
  const gateway = (config.gateway || {}) as Record<string, unknown>;
  const rawPort = gateway.port;
  return typeof rawPort === "number" && rawPort > 0 ? rawPort : 18789;
}

function resolveGatewayToken(config: Record<string, unknown>): string {
  const gateway = (config.gateway || {}) as Record<string, unknown>;
  const auth = (gateway.auth || {}) as Record<string, unknown>;
  const token = auth.token;
  if (typeof token === "string" && token.trim()) {
    return token.trim();
  }
  throw new Error("gateway.auth.token is required for boss-hr-runner.");
}

function resolveWorkspaceDir(config: Record<string, unknown>): string {
  const agents = (config.agents || {}) as Record<string, unknown>;
  const defaults = (agents.defaults || {}) as Record<string, unknown>;
  const workspace = defaults.workspace;
  if (typeof workspace === "string" && workspace.trim()) {
    return workspace.trim();
  }
  throw new Error("agents.defaults.workspace is required for boss-hr-runner.");
}

function buildRunnerArgv(
  config: Record<string, unknown>,
  pluginConfig: Record<string, unknown> | undefined,
  rawCommand?: string,
): string[] {
  const workspaceDir = resolveWorkspaceDir(config);
  const skillDir = path.join(workspaceDir, "skills", "boss-hr-assistant");
  const scriptPath = path.join(skillDir, "scripts", "drain_unread.py");
  const skillConfigPath = path.join(skillDir, "config", "skill-config.toml");
  const gatewayUrl = `http://127.0.0.1:${resolveGatewayPort(config)}`;
  const gatewayToken = resolveGatewayToken(config);
  const defaultAgentId =
    typeof pluginConfig?.agentId === "string" && pluginConfig.agentId.trim()
      ? pluginConfig.agentId.trim()
      : "main";

  return [
    typeof pluginConfig?.pythonBin === "string" && pluginConfig.pythonBin.trim()
      ? pluginConfig.pythonBin.trim()
      : "python3",
    scriptPath,
    "--config",
    skillConfigPath,
    "--gateway-url",
    gatewayUrl,
    "--gateway-token",
    gatewayToken,
    "--default-agent-id",
    defaultAgentId,
    "--raw-command",
    normalizeRawCommand(rawCommand),
  ];
}

async function runRunner(
  api: {
    config: Record<string, unknown>;
    pluginConfig?: Record<string, unknown>;
    runtime: {
      system: {
        runCommandWithTimeout: (
          argv: string[],
          opts: { timeoutMs: number; cwd?: string },
        ) => Promise<{
          stdout: string;
          stderr: string;
          code: number | null;
          killed: boolean;
          termination: string;
        }>;
      };
    };
  },
  rawCommand?: string,
): Promise<string> {
  const workspaceDir = resolveWorkspaceDir(api.config);
  const timeoutMs =
    typeof api.pluginConfig?.requestTimeoutMs === "number" && api.pluginConfig.requestTimeoutMs > 0
      ? api.pluginConfig.requestTimeoutMs
      : DEFAULT_TIMEOUT_MS;
  const argv = buildRunnerArgv(api.config, api.pluginConfig, rawCommand);
  const result = await api.runtime.system.runCommandWithTimeout(argv, {
    timeoutMs,
    cwd: workspaceDir,
  });

  const stdout = result.stdout.trim();
  const stderr = result.stderr.trim();
  if (result.code !== 0) {
    return stdout || stderr || `boss-hr-runner failed with code ${result.code ?? "unknown"}.`;
  }
  return stdout || stderr || "{}";
}

export default definePluginEntry({
  id: "boss-hr-runner",
  name: "Boss HR Runner",
  description: "Registers /boss_hr_drain and an optional runner tool for batch Boss HR polling.",
  register(api) {
    api.registerTool(
      {
        name: TOOL_NAME,
        description: "Run the external Boss HR drain runner with a raw command string.",
        parameters: {
          type: "object",
          additionalProperties: false,
          properties: {
            command: {
              type: "string",
              description: "Raw runner command, for example: drain-unread --profile chrome --allow-send",
            },
          },
        },
        async execute(_id, params: RunnerArgs) {
          const text = await runRunner(api, params.command);
          return {
            content: [{ type: "text", text }],
          };
        },
      },
      { optional: true },
    );

    api.registerCommand({
      name: COMMAND_NAME,
      description: "Drain unread Boss conversations through the external Python runner.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        const text = await runRunner(api, ctx.args || "drain-unread");
        return { text };
      },
    });
  },
});
