#!/usr/bin/env node

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const rootDir = path.resolve(__dirname, "..");
const venvDir = path.join(rootDir, "venv");
const venvPython = path.join(venvDir, "Scripts", "python.exe");
const requirementsFile = path.join(rootDir, "requirements.txt");
const specFile = path.join(rootDir, "build", "backend.spec");

const requiredImports = [
  "fastapi",
  "uvicorn",
  "requests",
  "pydantic",
  "multipart",
  "httpx",
  "PIL",
];

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: rootDir,
    stdio: "inherit",
    shell: false,
    ...options,
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited with code ${result.status}`);
  }
}

function runCapture(command, args) {
  return spawnSync(command, args, {
    cwd: rootDir,
    encoding: "utf8",
    shell: false,
  });
}

function firstAvailablePython() {
  for (const command of ["py", "python"]) {
    const args = command === "py" ? ["-3", "--version"] : ["--version"];
    const result = runCapture(command, args);
    if (!result.error && result.status === 0) {
      return { command, prefixArgs: command === "py" ? ["-3"] : [] };
    }
  }
  throw new Error("Python was not found. Install Python 3.10+ or create venv manually.");
}

function ensureVenv() {
  if (fs.existsSync(venvPython)) {
    return;
  }

  console.log("[backend-build] venv not found, creating project virtual environment...");
  const python = firstAvailablePython();
  run(python.command, [...python.prefixArgs, "-m", "venv", "venv"]);
}

function pythonHasImports(pythonExe, imports) {
  const script = imports
    .map((name) => `import ${name}`)
    .join("; ");
  const result = runCapture(pythonExe, ["-c", script]);
  return result.status === 0;
}

function ensurePythonDependencies() {
  if (!fs.existsSync(requirementsFile)) {
    throw new Error("requirements.txt is missing.");
  }

  if (!pythonHasImports(venvPython, requiredImports)) {
    console.log("[backend-build] Installing Python runtime dependencies into venv...");
    run(venvPython, ["-m", "pip", "install", "-r", "requirements.txt"]);
  } else {
    console.log("[backend-build] Python runtime dependencies already available in venv.");
  }

  if (!pythonHasImports(venvPython, ["PyInstaller"])) {
    console.log("[backend-build] Installing PyInstaller into venv...");
    run(venvPython, ["-m", "pip", "install", "pyinstaller"]);
  } else {
    console.log("[backend-build] PyInstaller already available in venv.");
  }
}

function main() {
  ensureVenv();
  ensurePythonDependencies();

  console.log(`[backend-build] Using Python: ${venvPython}`);
  console.log("[backend-build] Verifying httpx is importable before packaging...");
  run(venvPython, ["-c", "import httpx; print('httpx', httpx.__version__)"]);

  console.log("[backend-build] Building backend with venv PyInstaller...");
  run(venvPython, ["-m", "PyInstaller", "--noconfirm", specFile]);
}

try {
  main();
} catch (error) {
  console.error(`[backend-build] ERROR: ${error.message}`);
  process.exit(1);
}
