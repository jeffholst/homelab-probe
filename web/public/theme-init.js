// Applies the stored theme before the first paint, so a dark-theme user never sees a white flash.
// It is a separate file, not an inline script, because the server's content-security policy allows only its own scripts.
// The key and the three words are the ones in src/theme/storage.ts (a test compares them).
(function () {
  try {
    var stored = window.localStorage.getItem("hlp-theme");
    if (stored === "light" || stored === "dark") document.documentElement.setAttribute("data-theme", stored);
  } catch {
    // Storage is blocked: the system theme applies.
  }
})();
