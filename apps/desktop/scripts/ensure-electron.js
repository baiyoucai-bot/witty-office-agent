#!/usr/bin/env node
"use strict";

/**
 * Install Electron without GitHub releases.
 * Order: existing dist -> local/user cache zip -> npmmirror zip.
 * The npm wrapper is a small local shim; we do not run `npm install electron`
 * (that postinstall times out on github.com).
 */

const { spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const https = require("https");
const path = require("path");
const { pipeline } = require("stream/promises");

const ROOT = path.resolve(__dirname, "..");
const VERSION = String(require("../package.json").devDependencies.electron).replace(/^[^\d]*/, "");
const PLATFORM = process.env.npm_config_platform || process.platform;
const ARCH = process.env.npm_config_arch || process.arch;
const DIST_REL =
  PLATFORM === "darwin" || PLATFORM === "mas"
    ? "Electron.app/Contents/MacOS/Electron"
    : PLATFORM === "win32"
      ? "electron.exe"
      : "electron";
const ZIP_NAME = `electron-v${VERSION}-${PLATFORM}-${ARCH}.zip`;
const ELECTRON_DIR = path.join(ROOT, "node_modules", "electron");
const DIST = path.join(ELECTRON_DIR, "dist");
const MIRROR = (
  process.env.ELECTRON_MIRROR ||
  process.env.npm_config_electron_mirror ||
  "https://cdn.npmmirror.com/binaries/electron/"
).replace(/\/?$/, "/");

function log(message) {
  process.stdout.write(`${message}\n`);
}

function alreadyInstalled() {
  const exe = path.join(DIST, DIST_REL);
  const verFile = path.join(DIST, "version");
  const pathFile = path.join(ELECTRON_DIR, "path.txt");
  if (!fs.existsSync(exe) || !fs.existsSync(verFile) || !fs.existsSync(pathFile)) {
    return false;
  }
  const version = fs.readFileSync(verFile, "utf8").trim().replace(/^v/, "");
  const pointer = fs.readFileSync(pathFile, "utf8").trim();
  return version === VERSION && pointer === DIST_REL;
}

function walkFiles(dir, acc, depth) {
  if (depth < 0 || !fs.existsSync(dir)) {
    return acc;
  }
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkFiles(full, acc, depth - 1);
    } else if (entry.isFile()) {
      acc.push(full);
    }
  }
  return acc;
}

function findCachedZip() {
  const home = process.env.HOME || process.env.USERPROFILE || "";
  const roots = [
    process.env.ELECTRON_CACHE,
    process.env.electron_config_cache,
    path.join(ROOT, ".electron-cache"),
    path.join(home, "Library", "Caches", "electron"),
    path.join(home, ".cache", "electron"),
  ].filter(Boolean);
  for (const root of roots) {
    for (const file of walkFiles(root, [], 3)) {
      if (path.basename(file) === ZIP_NAME) {
        return file;
      }
    }
  }
  return "";
}

function writeShim() {
  fs.mkdirSync(ELECTRON_DIR, { recursive: true });
  const pkg = {
    name: "electron",
    version: VERSION,
    private: true,
    description: "Local Electron wrapper (binary from cache or npmmirror)",
    main: "index.js",
    bin: { electron: "cli.js" },
  };
  fs.writeFileSync(path.join(ELECTRON_DIR, "package.json"), `${JSON.stringify(pkg, null, 2)}\n`);
  fs.writeFileSync(
    path.join(ELECTRON_DIR, "index.js"),
    [
      '"use strict";',
      "const fs = require(\"fs\");",
      "const path = require(\"path\");",
      "const exe = path.join(__dirname, \"dist\", fs.readFileSync(path.join(__dirname, \"path.txt\"), \"utf8\").trim());",
      "if (!fs.existsSync(exe)) {",
      "  throw new Error(\"Electron binary missing at \" + exe + \". Run: node scripts/ensure-electron.js\");",
      "}",
      "module.exports = exe;",
      "",
    ].join("\n"),
  );
  fs.writeFileSync(
    path.join(ELECTRON_DIR, "cli.js"),
    [
      "#!/usr/bin/env node",
      '"use strict";',
      "const { spawn } = require(\"child_process\");",
      "const electron = require(\"./\");",
      "const child = spawn(electron, process.argv.slice(2), { stdio: \"inherit\", windowsHide: false });",
      "child.on(\"close\", (code, signal) => {",
      "  if (code === null) {",
      "    process.stderr.write(`electron exited with signal ${signal}\\n`);",
      "    process.exit(1);",
      "  }",
      "  process.exit(code);",
      "});",
      "",
    ].join("\n"),
  );
  fs.chmodSync(path.join(ELECTRON_DIR, "cli.js"), 0o755);
  const binDir = path.join(ROOT, "node_modules", ".bin");
  fs.mkdirSync(binDir, { recursive: true });
  const binLink = path.join(binDir, "electron");
  try {
    fs.unlinkSync(binLink);
  } catch {
    // missing is fine
  }
  fs.symlinkSync(path.join("..", "electron", "cli.js"), binLink);
}

function unpack(zipPath) {
  fs.mkdirSync(DIST, { recursive: true });
  log(`unpack ${zipPath} -> ${DIST}`);
  const result = spawnSync("unzip", ["-o", "-q", zipPath, "-d", DIST], { stdio: "inherit" });
  if (result.status !== 0) {
    throw new Error(`unzip failed with status ${result.status}`);
  }
  fs.writeFileSync(path.join(ELECTRON_DIR, "path.txt"), `${DIST_REL}\n`);
}

function download(url, dest) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith("https:") ? https : http;
    const request = client.get(url, { timeout: 120000 }, (response) => {
      const code = response.statusCode || 0;
      if (code >= 300 && code < 400 && response.headers.location) {
        response.resume();
        download(response.headers.location, dest).then(resolve, reject);
        return;
      }
      if (code !== 200) {
        response.resume();
        reject(new Error(`GET ${url} -> ${code}`));
        return;
      }
      const tmp = `${dest}.part`;
      const out = fs.createWriteStream(tmp);
      pipeline(response, out)
        .then(() => {
          fs.renameSync(tmp, dest);
          resolve(dest);
        })
        .catch(reject);
    });
    request.on("timeout", () => {
      request.destroy(new Error(`timeout ${url}`));
    });
    request.on("error", reject);
  });
}

async function ensureZip() {
  const cached = findCachedZip();
  if (cached) {
    log(`using cached zip ${cached}`);
    return cached;
  }
  const cacheDir = path.join(ROOT, ".electron-cache");
  fs.mkdirSync(cacheDir, { recursive: true });
  const dest = path.join(cacheDir, ZIP_NAME);
  if (fs.existsSync(dest) && fs.statSync(dest).size > 1024 * 1024) {
    log(`using local zip ${dest}`);
    return dest;
  }
  const url = `${MIRROR}v${VERSION}/${ZIP_NAME}`;
  log(`download ${url}`);
  await download(url, dest);
  return dest;
}

async function main() {
  if (alreadyInstalled()) {
    writeShim();
    log(`electron ${VERSION} already installed`);
    return;
  }
  writeShim();
  const zipPath = await ensureZip();
  unpack(zipPath);
  if (!alreadyInstalled()) {
    throw new Error("electron binary missing after unpack");
  }
  log(`electron ${VERSION} ready at ${path.join(DIST, DIST_REL)}`);
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
