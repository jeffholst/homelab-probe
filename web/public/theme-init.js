// Applies the stored theme before the first paint, so a light-theme user never sees a dark flash (dark is the default).
// It is a separate file, not an inline script, because the server's content-security policy allows only its own scripts.
// The key and the words are the ones in src/theme/storage.ts (a test compares them).
(function () {
  try {
    var stored = window.localStorage.getItem("hlp-theme");
    if (stored === "light" || stored === "system") document.documentElement.setAttribute("data-theme", stored);
  } catch {
    // Storage is blocked: the default (dark) theme applies.
  }
})();
