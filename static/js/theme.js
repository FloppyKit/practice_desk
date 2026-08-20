/**
 * Light/dark toggle — same storage key as drmattbrown-site (dmb-theme).
 */
(function () {
  var STORAGE_KEY = "dmb-theme";

  function apply(theme) {
    var root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    root.style.colorScheme = theme;
  }

  function current() {
    try {
      var s = localStorage.getItem(STORAGE_KEY);
      if (s === "light" || s === "dark") return s;
    } catch (e) {}
    if (document.documentElement.classList.contains("dark")) return "dark";
    return "light";
  }

  function setTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {}
    apply(theme);
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      var isDark = theme === "dark";
      btn.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
      btn.setAttribute("title", isDark ? "Light mode" : "Dark mode");
    }
  }

  function toggle() {
    setTheme(current() === "dark" ? "light" : "dark");
  }

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    // Re-sync from storage (FOUC script already applied class)
    setTheme(current());
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggle);
  });

  window.DmbTheme = { setTheme: setTheme, toggle: toggle, current: current };
})();
