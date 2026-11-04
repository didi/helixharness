import type { Plugin } from "@opencode/plugin";

import {
  PLUGIN_ID,
  type PluginContext,
  registerHelixHarness,
} from "./core.ts";

const helixharness: Plugin.Plugin = {
  id: PLUGIN_ID,
  async setup(ctx) {
    // The real context is a superset of the structural surface used by the
    // host-agnostic registration core.
    await registerHelixHarness(ctx as unknown as PluginContext);
  },
};

export default helixharness;
