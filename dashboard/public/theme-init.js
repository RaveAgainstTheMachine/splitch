try {
  var meta = document.querySelector('meta[name="color-scheme"]');
  var savedScheme = localStorage.getItem("color-scheme");
  if (meta && (savedScheme === "dark" || savedScheme === "light")) {
    meta.content = savedScheme;
  }
  var savedSurface = localStorage.getItem("splitch-surface");
  if (savedSurface) {
    document.documentElement.dataset.surface = savedSurface;
  }
  var savedAccent = localStorage.getItem("splitch-accent");
  if (savedAccent && /^#[0-9a-fA-F]{6}$/.test(savedAccent)) {
    document.documentElement.style.setProperty("--user-accent", savedAccent);
  }

  var osLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  var isLight = savedScheme === "light" || (savedScheme !== "dark" && osLight);
  var accent =
    savedAccent && /^#[0-9a-fA-F]{6}$/.test(savedAccent)
      ? savedAccent
      : isLight
        ? "#5b21b6"
        : "#a78bfa";
  var bg = isLight
    ? "#f6f7fb"
    : savedSurface === "oled"
      ? "#000000"
      : savedSurface === "slate"
        ? "#0b1120"
        : savedSurface === "charcoal"
          ? "#141416"
          : "#0a0b10";
  var border = isLight
    ? "#d7dbe8"
    : savedSurface === "oled"
      ? "#222536"
      : savedSurface === "slate"
        ? "#334155"
        : savedSurface === "charcoal"
          ? "#383842"
          : "#262a3d";
  var upper = isLight ? "#121521" : "#ffffff";
  var svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">' +
    '<defs><clipPath id="ct"><rect x="0" y="0" width="64" height="30.6"/></clipPath>' +
    '<clipPath id="cb"><rect x="0" y="33.4" width="64" height="30.6"/></clipPath></defs>' +
    '<rect width="64" height="64" rx="14" fill="' + bg + '"/>' +
    '<rect x="1" y="1" width="62" height="62" rx="13" stroke="' + border + '" stroke-width="1.8"/>' +
    '<g clip-path="url(#ct)" transform="translate(-1.8 0)">' +
    '<path d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5" stroke="' + upper + '" stroke-width="10.5" stroke-linecap="butt"/>' +
    '<rect x="12.5" y="29" width="29" height="1.7" rx="0.85" fill="' + upper + '"/></g>' +
    '<g clip-path="url(#cb)" transform="translate(1.8 0)">' +
    '<path d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5" stroke="' + accent + '" stroke-width="10.5" stroke-linecap="butt"/>' +
    '<rect x="22.5" y="33.4" width="29" height="1.7" rx="0.85" fill="' + accent + '"/></g></svg>';
  var iconLink = document.querySelector('link[rel="icon"]');
  if (iconLink) {
    iconLink.href = "data:image/svg+xml," + encodeURIComponent(svg);
  }
} catch (e) {}
