#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const rootDir = path.resolve(__dirname, "..");
const versionFile = path.join(rootDir, "VERSION");
const packageFile = path.join(rootDir, "package.json");
const lockFile = path.join(rootDir, "package-lock.json");

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJsonIfChanged(filePath, data) {
  const current = fs.existsSync(filePath) ? fs.readFileSync(filePath, "utf8") : "";
  const eol = current.includes("\r\n") ? "\r\n" : "\n";
  const next = `${JSON.stringify(data, null, 2)}\n`.replace(/\n/g, eol);
  if (current === next) {
    return false;
  }
  fs.writeFileSync(filePath, next, "utf8");
  return true;
}

function readProjectVersion() {
  if (!fs.existsSync(versionFile)) {
    throw new Error("VERSION file is missing.");
  }

  const firstLine = fs.readFileSync(versionFile, "utf8").split(/\r?\n/)[0].trim();
  if (!firstLine) {
    throw new Error("VERSION file is empty.");
  }

  if (/[<>:"/\\|?*\x00-\x1F]/.test(firstLine) || /[. ]$/.test(firstLine)) {
    throw new Error(`VERSION contains characters that are unsafe for a Windows installer filename: ${firstLine}`);
  }

  return firstLine;
}

function normalizePackageVersion(projectVersion) {
  const plainVersion = projectVersion.replace(/^v/i, "");
  const match = plainVersion.match(/^(\d+)\.(\d+)\.(\d+)$/);
  if (!match) {
    throw new Error(
      `VERSION must use numeric MAJOR.MINOR.PATCH format for Electron metadata. Current value: ${projectVersion}`,
    );
  }

  return match
    .slice(1)
    .map((part) => String(Number.parseInt(part, 10)))
    .join(".");
}

function main() {
  const projectVersion = readProjectVersion();
  const packageVersion = normalizePackageVersion(projectVersion);
  const pkg = readJson(packageFile);
  const artifactName = "Infinite-Canvas-Setup-" + projectVersion + ".${ext}";

  pkg.version = packageVersion;
  pkg.build = pkg.build || {};
  pkg.build.win = pkg.build.win || {};
  pkg.build.win.artifactName = artifactName;

  const packageChanged = writeJsonIfChanged(packageFile, pkg);

  let lockChanged = false;
  if (fs.existsSync(lockFile)) {
    const lock = readJson(lockFile);
    lock.version = packageVersion;
    if (lock.packages && lock.packages[""]) {
      lock.packages[""].version = packageVersion;
    }
    lockChanged = writeJsonIfChanged(lockFile, lock);
  }

  console.log(`[desktop-version] Project VERSION: ${projectVersion}`);
  console.log(`[desktop-version] Electron metadata version: ${packageVersion}`);
  console.log(`[desktop-version] Installer artifactName: ${artifactName}`);
  console.log(`[desktop-version] Expected installer: release/Infinite-Canvas-Setup-${projectVersion}.exe`);
  console.log(`[desktop-version] package.json ${packageChanged ? "updated" : "already current"}`);
  if (fs.existsSync(lockFile)) {
    console.log(`[desktop-version] package-lock.json ${lockChanged ? "updated" : "already current"}`);
  }
}

try {
  main();
} catch (error) {
  console.error(`[desktop-version] ERROR: ${error.message}`);
  process.exit(1);
}
