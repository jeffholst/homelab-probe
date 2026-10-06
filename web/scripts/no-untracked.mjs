// Fails when a path holds files git does not know about: `git diff --exit-code` alone passes when a regeneration
// creates a new file (a schema that was added without its generated types).
import { execFileSync } from "node:child_process";

const paths = process.argv.slice(2);
if (paths.length === 0) throw new Error("usage: no-untracked.mjs PATH...");

const out = execFileSync("git", ["ls-files", "--others", "--exclude-standard", "--", ...paths], { encoding: "utf8" });
if (out.trim() !== "") {
  console.error(`Untracked generated files (run \`npm run generate:types\` and commit them):\n${out}`);
  process.exit(1);
}
