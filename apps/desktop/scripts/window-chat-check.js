"use strict";

const { spawn } = require("child_process");
const path = require("path");
const http = require("http");

const desktopDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopDir, "..", "..");
const port = Number(process.env.WITTY_API_PORT || 18769);
const base = `http://127.0.0.1:${port}`;
const reply = process.env.WITTY_SCRIPTED_REPLY || "from-window";

function waitHealth(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(`${base}/v1/health`, (res) => {
        res.resume();
        if (res.statusCode === 200) {
          resolve();
          return;
        }
        retry();
      });
      req.on("error", retry);
      req.setTimeout(800, () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error("scripted API did not become healthy"));
        return;
      }
      setTimeout(tick, 150);
    };
    tick();
  });
}

async function main() {
  const api = spawn(
    "uv",
    ["run", "python", path.join(desktopDir, "scripts", "serve_scripted.py")],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        WITTY_API_HOST: "127.0.0.1",
        WITTY_API_PORT: String(port),
        WITTY_SCRIPTED_REPLY: reply,
      },
      stdio: "ignore",
    },
  );
  const electronBin = path.join(desktopDir, "node_modules", ".bin", "electron");
  try {
    await waitHealth(45000);
    const child = spawn(electronBin, [".", "--window-chat-check"], {
      cwd: desktopDir,
      env: {
        ...process.env,
        WITTY_API_BASE: base,
        WITTY_VERIFY_PROMPT: process.env.WITTY_VERIFY_PROMPT || "hello-window",
      },
      stdio: "inherit",
    });
    const code = await new Promise((resolve) => child.on("exit", resolve));
    if (code !== 0) {
      process.exit(code || 1);
    }
  } finally {
    api.kill();
  }
}

main().catch((error) => {
  process.stderr.write(`${error && error.message ? error.message : error}\n`);
  process.exit(1);
});
