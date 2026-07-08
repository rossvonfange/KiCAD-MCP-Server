/**
 * Loom/InferSynth engine nice-to-haves + KiCad environment bootstrap tools
 *
 * These tools surface the `loom.capability` engine verbs (when the engine is
 * installed) and self-serve a few operational chores the team was previously
 * doing by hand: enabling KiCad's API server and installing the Loom plugin.
 *
 * Every engine-backed tool is gated on a probe (python/utils/loom_probe.py)
 * and degrades to a friendly, actionable message — never a stack trace — when
 * `loom.capability` isn't importable. The MCP never bakes engine logic in;
 * it only calls through that probe/subprocess seam.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerLoomTools(server: McpServer, callKicadScript: Function) {
  // Engine availability check
  server.tool(
    "loom_status",
    "Report whether the Loom/InferSynth engine (loom.capability) is reachable: available or not, which seam is used (in-process vs. a LOOM_PYTHON subprocess), the resolved interpreter path, and its version. Never fails — always returns a status even when the engine isn't installed, with a `how_to_install` hint in that case.",
    {},
    async () => {
      const result = await callKicadScript("loom_status", {});
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // recognize_region — "what is this?" over the open board
  server.tool(
    "loom_recognize_region",
    "Recognize a region of the currently-open board via the Loom/InferSynth engine's recognize_region verb: pass either `refs` (reference designators) or `outline` ([x, y, w, h] in board units). Returns members, ports, port roles, a recognized cell_type (or novel), and any straddling components. Requires the Loom engine to be reachable (see loom_status); if it isn't, returns a friendly degrade message instead of failing.",
    {
      outline: z
        .array(z.number())
        .length(4)
        .optional()
        .describe("[x, y, w, h] bounding box of the region to recognize, in board units"),
      refs: z
        .array(z.string())
        .optional()
        .describe("Explicit list of reference designators (e.g. ['U3', 'R14']) to recognize as a region"),
      boardPath: z
        .string()
        .optional()
        .describe("Path to .kicad_pcb file (default: the currently open board)"),
    },
    async (args: { outline?: number[]; refs?: string[]; boardPath?: string }) => {
      const result = await callKicadScript("loom_recognize_region", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // lift_to_frd — recognize the whole open board back into an observed FRD
  server.tool(
    "loom_lift_to_frd",
    "Lift the currently-open board back into an observed FRD (Markdown requirements doc) via the Loom/InferSynth engine's lift_to_frd verb — the formal inverse of synthesis. Returns `markdown` (the emitted FRD, re-consumable by synthesis/lint), `requirements` (structured per-subsystem requirement list), and `gaps` (what recognition could not recover). Requires the Loom engine to be reachable (see loom_status); if it isn't, returns a friendly degrade message instead of failing.",
    {
      boardPath: z
        .string()
        .optional()
        .describe("Path to .kicad_pcb file (default: the currently open board)"),
    },
    async (args: { boardPath?: string }) => {
      const result = await callKicadScript("loom_lift_to_frd", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // explain_board — a human-readable rendering of the lifted FRD
  server.tool(
    "loom_explain_board",
    "Render a human-readable explanation of the currently-open board via the Loom/InferSynth engine's explain_board verb (a rendering of the same lifted FRD lift_to_frd produces). Requires the Loom engine to be reachable (see loom_status); if it isn't, returns a friendly degrade message instead of failing.",
    {
      boardPath: z
        .string()
        .optional()
        .describe("Path to .kicad_pcb file (default: the currently open board)"),
    },
    async (args: { boardPath?: string }) => {
      const result = await callKicadScript("loom_explain_board", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // plan_io — the CSP pin-assignment solver, as a tool
  server.tool(
    "loom_plan_io",
    "Plan an IO pin assignment via the Loom/InferSynth engine's plan_io CSP solver, over a device loaded from the Loom device-capability corpus. Pass `device` (a device ref/name, e.g. an STM32 part number) and `interfaces` (requested peripheral instance names, e.g. ['SPI1', 'USART2']). Optional `allowedPins` restricts to an observed pin set (Class B); `consumedPins` marks pins already used elsewhere. Returns the solver's Assignment | Infeasible envelope: pin placements on success, or conflicts (each carrying its blocking constraints) on failure. Requires the Loom engine to be reachable (see loom_status); if it isn't, returns a friendly degrade message instead of failing.",
    {
      device: z.string().describe("Device ref/name to load from the Loom device-capability corpus"),
      interfaces: z
        .array(z.string())
        .min(1)
        .describe("Requested peripheral instance names, e.g. ['SPI1', 'USART2']"),
      allowedPins: z
        .array(z.string())
        .optional()
        .describe("Optional Class-B restriction: only place onto these pins"),
      consumedPins: z
        .array(z.string())
        .optional()
        .describe("Optional pins to treat as already consumed elsewhere"),
    },
    async (args: {
      device: string;
      interfaces: string[];
      allowedPins?: string[];
      consumedPins?: string[];
    }) => {
      const result = await callKicadScript("loom_plan_io", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // synthesize_fabric — the two-gate fabric synthesis, as a tool
  server.tool(
    "loom_synthesize_fabric",
    "Run two-gate fabric synthesis via the Loom/InferSynth engine's synthesize_fabric verb, from a compact stuff spec over a device loaded from the Loom device-capability corpus. Pass `device` (a device ref/name) and `specText` (the compact spec, `<region>:<TYPE|instance>[*count][@rot]`). A minimal package is auto-scaffolded (one generous region per region-name in the spec). Returns `seated` (interfaces that passed both gates, with pin placements + room) and `contention` (interfaces that failed a gate, with reasons). Requires the Loom engine to be reachable (see loom_status); if it isn't, returns a friendly degrade message instead of failing.",
    {
      device: z.string().describe("Device ref/name to load from the Loom device-capability corpus"),
      specText: z
        .string()
        .describe("Compact stuff spec, e.g. 'region1:SPI1' or 'region1:USART*2@90'"),
    },
    async (args: { device: string; specText: string }) => {
      const result = await callKicadScript("loom_synthesize_fabric", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Enable KiCad's IPC API server
  server.tool(
    "kicad_enable_api",
    "Enable KiCad's API server by setting api.enable_server=true in the user's kicad_common.json. Auto-detects the installed KiCad version's config directory (does not assume a specific version). Backs up the config file before editing (path returned as backup_path) and warns if a KiCad process looks to be running, since KiCad may overwrite this file with its in-memory settings when it exits. Restarting KiCad is required for the change to take effect.",
    {
      version: z
        .string()
        .optional()
        .describe("KiCad version dir to target, e.g. '10.0' (default: the newest detected)"),
    },
    async (args: { version?: string }) => {
      const result = await callKicadScript("kicad_enable_api", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Install/scaffold the Loom plugin into KiCad's 3rd-party plugin dir
  server.tool(
    "loom_install_plugin",
    "Copy or symlink a plugin package into KiCad's 3rd-party plugin directory (auto-detected version, not hardcoded). If `source` is omitted, does nothing and instead reports where the plugin WOULD install — it never fabricates a plugin package.",
    {
      source: z
        .string()
        .optional()
        .describe("Path to the plugin package (file or directory) to install. Omit to just see the target install directory."),
      name: z
        .string()
        .optional()
        .describe("Destination name under the plugin dir (default: the source's basename)"),
      link: z
        .boolean()
        .optional()
        .describe("Symlink instead of copy (default: false — copies)"),
      version: z
        .string()
        .optional()
        .describe("KiCad version dir to target, e.g. '10.0' (default: the newest detected)"),
    },
    async (args: { source?: string; name?: string; link?: boolean; version?: string }) => {
      const result = await callKicadScript("loom_install_plugin", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );
}
