// Local development PostgreSQL using the real Postgres binaries shipped in the
// `@embedded-postgres/<platform>` npm package. For when Postgres/Docker isn't installed.
// In production, point DATABASE_URL at a managed PostgreSQL instance and skip this.
//
// Binaries + data live under ~/.orbit-pg (short path: Windows initdb fails on paths >260 chars).
import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const pkg = `@embedded-postgres/${process.platform === "win32" ? "windows" : process.platform}-${process.arch}`;
const srcNative = path.resolve("node_modules", pkg, "native");
const root = process.env.ORBIT_PG_HOME ?? path.join(os.homedir(), ".orbit-pg");
const runtime = path.join(root, "runtime");
const data = path.join(root, "data");
const port = String(process.env.DEV_PG_PORT ?? 5433);
const exe = (n) => path.join(runtime, "bin", process.platform === "win32" ? `${n}.exe` : n);

mkdirSync(root, { recursive: true });
if (!existsSync(exe("postgres"))) {
  console.log(`Copying Postgres runtime to ${runtime} …`);
  cpSync(srcNative, runtime, { recursive: true });
}
if (!existsSync(path.join(data, "PG_VERSION"))) {
  const pw = path.join(root, "pw.txt");
  writeFileSync(pw, "orbit");
  const r = spawnSync(exe("initdb"), ["-D", data, "-U", "orbit", `--pwfile=${pw}`, "-A", "scram-sha-256", "-E", "UTF8", "--no-locale"], { stdio: "inherit" });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

const pg = spawn(exe("postgres"), ["-D", data, "-p", port, "-h", "localhost"], { stdio: "inherit" });
console.log(`PostgreSQL starting on postgresql://orbit:orbit@localhost:${port} (data: ${data})`);
const stop = () => pg.kill("SIGINT");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
pg.on("exit", (code) => process.exit(code ?? 0));
