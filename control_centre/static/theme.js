// Shared colour-scheme switcher for the control-centre pages. Sets the CSS
// variables on <html> (so it works over each page's own :root defaults) and
// injects a "Theme" button into <header>. The choice is kept in localStorage,
// which is shared by every page on this origin.
(function () {
  const T = {
    orange: {label: "Orange", scheme: "light", v: {neon: "#d9483b", "neon-dim": "#e0644f", "neon-glow": "rgba(217,72,59,.22)", "accent-rgb": "217,72,59", ink: "#2a1a17", muted: "#8a6a62", border: "#f3d9d0", "border-bright": "#e9b9ab", card: "#ffffff", bg: "#fff5f1", bg2: "#fffaf8", red: "#b3122e", amber: "#b86e00", ok: "#1a8a55", "ok-rgb": "26,138,85"}},
    green:  {label: "Green",  scheme: "light", v: {neon: "#0a7a50", "neon-dim": "#11875a", "neon-glow": "rgba(10,122,80,.2)", "accent-rgb": "10,122,80", ink: "#14231c", muted: "#5a6e64", border: "#d5e2da", "border-bright": "#b5cdbf", card: "#ffffff", bg: "#f3f7f4", bg2: "#f8fbf9", red: "#c0303f", amber: "#a86400", ok: "#0a7a50", "ok-rgb": "10,122,80"}},
    blue:   {label: "Blue",   scheme: "light", v: {neon: "#2563eb", "neon-dim": "#3b78f0", "neon-glow": "rgba(37,99,235,.2)", "accent-rgb": "37,99,235", ink: "#10203a", muted: "#5b6b86", border: "#d6e0f2", "border-bright": "#b3c6e8", card: "#ffffff", bg: "#f2f6fd", bg2: "#f8faff", red: "#c0303f", amber: "#a86400", ok: "#1a8a55", "ok-rgb": "26,138,85"}},
    purple: {label: "Purple", scheme: "light", v: {neon: "#7c3aed", "neon-dim": "#8b52f0", "neon-glow": "rgba(124,58,237,.2)", "accent-rgb": "124,58,237", ink: "#1e1233", muted: "#6b5a86", border: "#e3d9f3", "border-bright": "#c9b6ea", card: "#ffffff", bg: "#f7f3fd", bg2: "#fbf9ff", red: "#c0303f", amber: "#a86400", ok: "#1a8a55", "ok-rgb": "26,138,85"}},
    dark:   {label: "Dark",   scheme: "dark",  v: {neon: "#10dd8f", "neon-dim": "#1fbd85", "neon-glow": "rgba(16,221,143,.25)", "accent-rgb": "16,221,143", ink: "#f1fbf5", muted: "#a5c9b6", border: "#1c3a2d", "border-bright": "#275842", card: "#0c1a14", bg: "#071410", bg2: "#0a1a14", red: "#ff7a86", amber: "#ffb454", ok: "#10dd8f", "ok-rgb": "16,221,143"}},
  };
  const order = Object.keys(T), KEY = "libbyTheme";
  let cur = "orange";
  try { cur = localStorage.getItem(KEY) || cur; } catch (e) {}
  if (!T[cur]) cur = "orange";

  function apply(name) {
    const root = document.documentElement;
    Object.entries(T[name].v).forEach(([k, v]) => root.style.setProperty("--" + k, v));
    root.style.colorScheme = T[name].scheme;
    cur = name;
    try { localStorage.setItem(KEY, name); } catch (e) {}
    const b = document.getElementById("themeBtn");
    if (b) b.textContent = "Theme: " + T[name].label;
  }
  apply(cur);  // before first paint, so there's no flash of the default

  document.addEventListener("DOMContentLoaded", () => {
    const header = document.querySelector("header");
    if (!header) return;
    const b = document.createElement("button");
    b.id = "themeBtn"; b.type = "button"; b.className = "tab-link"; b.style.cursor = "pointer";
    b.title = "Cycle colour scheme";
    b.onclick = () => apply(order[(order.indexOf(cur) + 1) % order.length]);
    header.appendChild(b);
    apply(cur);
  });
})();
