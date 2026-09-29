// Runs before first paint (loaded synchronously from <head>) so the theme never flashes.
(function () {
  try {
    var pref = localStorage.getItem("adjutant.theme") || "system";
    var dark = pref === "dark" || (pref === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
