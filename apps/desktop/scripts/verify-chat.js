"use strict";

const api = require("../api");

async function main() {
  const health = await api.health();
  if (!health.ok) {
    throw new Error("health not ok");
  }
  const session = await api.createSession({
    project_id: process.env.WITTY_PROJECT_ID || "default_project",
    agent_id: process.env.WITTY_AGENT_ID || "default_agent",
    workspace_dir: process.env.WITTY_WORKSPACE || process.cwd(),
  });
  if (!session.session_id) {
    throw new Error("missing session_id");
  }
  const reply = await api.sendPrompt(session.session_id, process.env.WITTY_VERIFY_PROMPT || "hello");
  if (typeof reply.text !== "string") {
    throw new Error("missing reply text");
  }
  process.stdout.write(
    `${JSON.stringify({ ok: true, session_id: session.session_id, text: reply.text })}\n`,
  );
}

main().catch((error) => {
  process.stderr.write(`${error && error.message ? error.message : error}\n`);
  process.exit(1);
});
