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

// Click a square to see what was solved that day. The squares are buttons only
// when there is something behind them, so anything else here is a no-op.
document.addEventListener("click", async (event) => {
  const cell = event.target.closest("button.hm-cell[data-day]");
  if (!cell) return;

  // The panel sits beside the grid, not inside it: the grid's width tracks the
  // number of weeks and would squash a one-week view to a sliver.
  const wrap = cell.closest(".heatmap-wrap");
  const panel = wrap && wrap.querySelector(".day-detail");
  if (!panel) return;

  // Clicking the open day again closes it.
  if (panel.dataset.day === cell.dataset.day && !panel.hidden) {
    panel.hidden = true;
    cell.classList.remove("picked");
    return;
  }
  wrap.querySelectorAll(".hm-cell.picked").forEach((c) => c.classList.remove("picked"));
  cell.classList.add("picked");

  panel.dataset.day = cell.dataset.day;
  panel.hidden = false;
  panel.textContent = "Loading…";

  const pretty = new Date(cell.dataset.day + "T12:00:00Z").toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
  });

  try {
    const response = await fetch(
      `/api/day/${cell.dataset.owner}/${cell.dataset.day}.json`
    );
    if (!response.ok) throw new Error(String(response.status));
    const { problems } = await response.json();

    panel.textContent = "";
    const heading = document.createElement("p");
    heading.className = "day-detail-head";
    heading.textContent = pretty;
    panel.append(heading);

    if (!problems.length) {
      const none = document.createElement("p");
      none.className = "hint";
      // Counts come from the submission calendar; titles only go back so far.
      none.textContent = "No problem titles recorded for this day.";
      panel.append(none);
      return;
    }
    const list = document.createElement("ul");
    list.className = "day-detail-list";
    for (const problem of problems) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `https://leetcode.com/problems/${problem.slug}/`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = problem.number
        ? `${problem.number}. ${problem.title}`
        : problem.title;
      const badge = document.createElement("span");
      badge.className = `diff ${String(problem.difficulty).toLowerCase()}`;
      badge.textContent = problem.difficulty;
      item.append(link, badge);
      list.append(item);
    }
    panel.append(list);
  } catch {
    panel.textContent = "Could not load that day.";
  }
});
