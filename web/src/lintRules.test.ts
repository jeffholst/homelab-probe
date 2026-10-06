// @vitest-environment node
import { ESLint } from "eslint";
import tseslint from "typescript-eslint";
import { describe, expect, it } from "vitest";

/** The rules of eslint.config.js that keep controller strings text and secrets out of storage must be able to fail. */
// The snippets are not files of the project, so they are linted without type information (the rules tested here are syntactic).
const eslint = new ESLint({
  cwd: process.cwd(),
  overrideConfig: [tseslint.configs.disableTypeChecked],
});

async function messages(code: string, filePath: string): Promise<string[]> {
  const [result] = await eslint.lintText(code, { filePath });
  return (result?.messages ?? []).filter((message) => message.ruleId === "no-restricted-syntax" || message.ruleId === "no-restricted-globals").map((message) => message.message);
}

describe("the lint rules that keep strings text", () => {
  it("forbids dangerouslySetInnerHTML as a JSX attribute", async () => {
    const found = await messages('export const A = ({ html }: { html: string }) => <div dangerouslySetInnerHTML={{ __html: html }} />;\n', "src/pages/A.tsx");
    expect(found.join()).toContain("dangerouslySetInnerHTML is forbidden");
  });

  it("forbids it in a spread or a createElement call too", async () => {
    const found = await messages('const props = { dangerouslySetInnerHTML: { __html: "x" } };\nexport default props;\n', "src/pages/B.tsx");
    expect(found.join()).toContain("dangerouslySetInnerHTML is forbidden");
    const quoted = await messages('const props = { "dangerouslySetInnerHTML": { __html: "x" } };\nexport default props;\n', "src/pages/B.tsx");
    expect(quoted.join()).toContain("dangerouslySetInnerHTML is forbidden");
  });

  it("forbids writing markup into the document", async () => {
    const found = await messages('export function f(el: HTMLElement, text: string) { el.innerHTML = text; el.insertAdjacentHTML("beforeend", text); document.write(text); process.stderr.write(text); }\n', "src/pages/C.tsx");
    expect(found).toHaveLength(3); // innerHTML, insertAdjacentHTML, document.write: but not a stream's write
  });

  it("forbids browser storage outside the theme module, including through window", async () => {
    expect((await messages('export const t = () => localStorage.getItem("token");\n', "src/pages/D.tsx")).join()).toContain("Browser storage");
    expect((await messages('export const t = () => window.sessionStorage.setItem("a", "b");\n', "src/pages/D.tsx")).join()).toContain("Browser storage");
  });

  it("allows storage in the theme module and ordinary code everywhere", async () => {
    expect(await messages('export const t = () => window.localStorage.getItem("hlp-theme");\n', "src/theme/storage.ts")).toEqual([]);
    expect(await messages("export const A = () => <p>text</p>;\n", "src/pages/E.tsx")).toEqual([]);
  });
});
