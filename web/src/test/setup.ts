import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  // Some test files run in Node (`@vitest-environment node`) and have no document to clean.
  if (typeof window === "undefined") return;
  cleanup();
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.title = "";
});
