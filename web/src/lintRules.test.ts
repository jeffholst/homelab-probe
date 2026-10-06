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

  // Every spelling of a forbidden access: the property written as a name, as a string, as a template, or destructured.
  it.each([
    ['el.innerHTML = text;', "Do not assign markup"],
    ['el["innerHTML"] = text;', "Do not assign markup"],
    ["el['outerHTML'] = text;", "Do not assign markup"],
    ["el[`innerHTML`] = text;", "Do not assign markup"],
    ['el.insertAdjacentHTML("beforeend", text);', "Do not write markup"],
    ['el["insertAdjacentHTML"]("beforeend", text);', "Do not write markup"],
    ["el[`insertAdjacentHTML`]('beforeend', text);", "Do not write markup"],
    ["document.write(text);", "Do not write markup"],
    ['document["write"](text);', "Do not write markup"],
    ['document["writeln"](text);', "Do not write markup"],
    ["window.document.write(text);", "Do not write markup"],
    ['window["document"]["write"](text);', "Do not write markup"],
    ['window["localStorage"].setItem("a", "b");', "Browser storage"],
    ['globalThis["sessionStorage"].clear();', "Browser storage"],
    ["window[`localStorage`].clear();", "Browser storage"],
    ['self["indexedDB"].open("x");', "Browser storage"],
    ["const { localStorage: store } = window;", "Browser storage"],
    ['const { "sessionStorage": store } = globalThis;', "Browser storage"],
    ["const props = { ['dangerouslySetInnerHTML']: { __html: text } };", "dangerouslySetInnerHTML is forbidden"],
  ])("rejects the computed and destructured form: %s", async (code, expected) => {
    const found = await messages(`declare const el: HTMLElement; declare const text: string;\n${code}\nexport {};\n`, "src/pages/F.tsx");
    expect(found.join()).toContain(expected);
  });

  it("does not take a stream's write or an unrelated property for the document's", async () => {
    const found = await messages('declare const stream: { write(t: string): void }; stream.write("x"); const o = { write: 1, innerHTMLLength: 2 }; export { o };\n', "src/pages/G.tsx");
    expect(found).toEqual([]);
  });

  it("allows storage in the theme module and ordinary code everywhere", async () => {
    expect(await messages('export const t = () => window.localStorage.getItem("hlp-theme");\n', "src/theme/storage.ts")).toEqual([]);
    expect(await messages("export const A = () => <p>text</p>;\n", "src/pages/E.tsx")).toEqual([]);
  });
});
