// Copy-to-clipboard for the invite link.
document.addEventListener("click", async (event) => {
  const trigger = event.target.closest("[data-copy]");
  if (!trigger) return;

  const field = document.querySelector(trigger.dataset.copy);
  if (!field) return;

  const original = trigger.textContent;
  try {
    await navigator.clipboard.writeText(field.value);
  } catch {
    // Clipboard API needs a secure context; selecting the text is the next best thing.
    field.select();
    trigger.textContent = "Press ⌘C";
    setTimeout(() => (trigger.textContent = original), 1800);
    return;
  }
  trigger.textContent = "Copied";
  setTimeout(() => (trigger.textContent = original), 1400);
});

// Confirmation for destructive forms.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset?.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

// Light / dark toggle. With nothing stored we follow the OS, so the first click
// has to commit to the opposite of whatever is on screen right now.
(() => {
  const button = document.getElementById("theme-toggle");
  if (!button) return;

  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
  const current = () =>
    document.documentElement.dataset.theme || (prefersDark.matches ? "dark" : "light");

  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("theme", next);
    } catch {
      // Private mode. The choice still applies until the page is left.
    }
  });

  // Someone who never chose should keep tracking the OS while the tab is open.
  prefersDark.addEventListener("change", () => {
    try {
      if (!localStorage.getItem("theme")) delete document.documentElement.dataset.theme;
    } catch { /* nothing stored to respect */ }
  });
})();
