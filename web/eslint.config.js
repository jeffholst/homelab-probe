import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// Controller and user strings (names, SSIDs, event text, notes) are untrusted: they are rendered as text only, through
// `<Text>` / `safeText()`. Nothing in this app may turn a string into markup, and nothing secret may reach the
// browser's storage; the rules below make both a lint error rather than a review comment.
const markup = [
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "dangerouslySetInnerHTML is forbidden: render controller strings as text (<Text>, safeText()).",
  },
  {
    selector: "Identifier[name='dangerouslySetInnerHTML']",
    message: "dangerouslySetInnerHTML is forbidden: render controller strings as text (<Text>, safeText()).",
  },
  {
    selector: "Literal[value='dangerouslySetInnerHTML']",
    message: "dangerouslySetInnerHTML is forbidden: render controller strings as text (<Text>, safeText()).",
  },
  {
    selector: "AssignmentExpression[left.property.name=/^(innerHTML|outerHTML)$/]",
    message: "Do not assign markup: render text (<Text>) or build elements with React.",
  },
  {
    selector: "CallExpression[callee.property.name=/^(insertAdjacentHTML|write|writeln)$/]",
    message: "Do not write markup into the document: render text (<Text>) or build elements with React.",
  },
];

const storage = [
  {
    selector: "MemberExpression[property.name=/^(localStorage|sessionStorage|indexedDB)$/]",
    message: "Browser storage is for the theme preference only (src/theme/storage.ts). Never store a token or any secret.",
  },
];

const storageGlobals = ["localStorage", "sessionStorage", "indexedDB"].map((name) => ({
  name,
  message: "Browser storage is for the theme preference only (src/theme/storage.ts). Never store a token or any secret.",
}));

export default tseslint.config(
  { ignores: ["dist", "node_modules", "test-results", "playwright-report", "src/generated"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  jsxA11y.flatConfigs.recommended,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser },
      parserOptions: { project: ["./tsconfig.json", "./tsconfig.node.json"], tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-restricted-syntax": ["error", ...markup, ...storage],
      "no-restricted-globals": ["error", ...storageGlobals],
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
      "@typescript-eslint/no-floating-promises": "error",
    },
  },
  {
    // The one module that may touch browser storage, for the theme preference (a word, not a secret).
    files: ["src/theme/storage.ts"],
    rules: {
      "no-restricted-syntax": ["error", ...markup],
      "no-restricted-globals": "off",
    },
  },
  {
    files: ["**/*.mjs", "e2e/**", "playwright.config.ts", "vite.config.ts", "scripts/**", "eslint.config.js"],
    languageOptions: { globals: { ...globals.node } },
    rules: {
      // Tooling runs in Node, not in the browser: it may read and write the files it needs.
      "no-restricted-syntax": ["error", ...markup],
      "no-restricted-globals": "off",
    },
  },
  {
    // Plain JavaScript (the scripts, this file, the script that runs before the first paint) has no type information.
    files: ["**/*.mjs", "eslint.config.js", "public/**/*.js"],
    ...tseslint.configs.disableTypeChecked,
  },
  {
    files: ["public/**/*.js"],
    languageOptions: { globals: { ...globals.browser } },
    rules: {
      // public/theme-init.js reads the theme preference before the first paint, like src/theme/storage.ts.
      "no-restricted-syntax": ["error", ...markup],
      "no-restricted-globals": "off",
    },
  },
  {
    // Tests inspect storage to prove nothing but the theme word is ever put there.
    files: ["src/**/*.test.{ts,tsx}", "src/test/**"],
    rules: {
      "no-restricted-syntax": ["error", ...markup],
      "no-restricted-globals": "off",
    },
  },
);
