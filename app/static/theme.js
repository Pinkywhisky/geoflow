// Apply the stored theme before the first paint to avoid a light flash.
document.documentElement.classList.add("js");
try {
  const theme = localStorage.getItem("geoflow-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch (error) {}
