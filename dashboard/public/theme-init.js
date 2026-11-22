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
} catch (e) {}
