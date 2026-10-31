(function extractAttendanceRows(targetDateKey) {
  const compact = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const courseRe = /\b[A-Z]{3}\d{4}\b/i;
  const typeRe = /\b(workshop|tutorial|studio|applied(?:\s+class)?|practical|laboratory|lab|seminar)\b\s*0*(\d{1,2})?/i;
  const timeRe = /\b\d{1,2}:\d{2}\s*[ap]m\b/i;
  const completionClue = (value) => {
    const clue = String(value || "").toLowerCase();
    if (/question|help|unknown|pending|incomplete/.test(clue)) return false;
    return /[✓✔☑]|(?:glyphicon|ui-icon|icon|fa|fas|far|fal|fab|bi)[-_ ]*(?:ok|check)(?:[-_ ]|\b)|\bcheck(?:ed|mark)?\b|\btick\b|\bcomplete(?:d)?\b|\bpresent\b|\bsuccess\b/.test(clue);
  };
  const syntheticCompletedHref = (value) => {
    const href = String(value || "");
    if (!href) return false;
    try {
      return new URL(href, location.href).searchParams.get("mah_completed") === "1";
    } catch {
      return /[?&]mah_completed=1(?:&|#|$)/.test(href);
    }
  };
  const visible = (node) => {
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden" && (node.getClientRects().length > 0 || node.offsetWidth > 0 || node.offsetHeight > 0);
  };
  const sessions = [];

  const add = (root, completedHint = false, entryHref = "") => {
    if (!root || !visible(root)) return;
    const text = compact(root.innerText || root.textContent);
    if (!text || text.length > 750) return;
    const course = courseRe.exec(text)?.[0]?.toUpperCase();
    const type = typeRe.exec(text);
    const time = timeRe.exec(text)?.[0] || "";
    if (!course || !type || !time) return;
    const number = type[2] ? Number(type[2]) : null;
    const session = `${type[1].replace(/\s+/g, " ")}${number !== null ? ` ${String(number).padStart(2, "0")}` : ""}`;
    const nodes = [root, ...[...(root.querySelectorAll?.("[class],[data-icon],[title],[aria-label],img,svg,i,span") || [])].slice(0, 80)];
    const clue = compact(`${nodes.map((node) => `${node.getAttribute?.("class") || ""} ${node.getAttribute?.("data-icon") || ""} ${node.getAttribute?.("title") || ""} ${node.getAttribute?.("aria-label") || ""} ${node.getAttribute?.("alt") || ""} ${node.getAttribute?.("src") || ""}`).join(" ")} ${String(root.outerHTML || "").slice(0, 6000)}`);
    const syntheticCompleted = syntheticCompletedHref(entryHref);
    const realEntryHref = syntheticCompleted ? "" : String(entryHref || "");
    // A synthetic Entry.aspx-looking link is injected only to make a completed row visible
    // to first-pass discovery. It is never a submittable Attendance entry. A genuine
    // Entry.aspx link still means the row is pending, even if unrelated success/check CSS
    // exists elsewhere inside the same container.
    const completed = syntheticCompleted || (!realEntryHref && (completedHint || completionClue(`${text} ${clue}`)));
    const key = `${targetDateKey}|${course}|${session.toLowerCase()}|${time.toLowerCase()}`;
    const row = {
      course,
      session,
      attendanceLabel: text,
      time,
      entryUrl: completed ? "" : realEntryHref,
      completed,
      sourceUrl: location.href
    };
    const index = sessions.findIndex((item) => item.key === key);
    if (index < 0) {
      sessions.push({ key, ...row });
    } else {
      const previous = sessions[index];
      const finalCompleted = previous.completed || completed;
      sessions[index] = {
        key,
        ...previous,
        ...row,
        entryUrl: finalCompleted ? "" : (row.entryUrl || previous.entryUrl || ""),
        completed: finalCompleted
      };
    }
  };

  for (const link of document.querySelectorAll("a[href*='Entry.aspx']")) {
    let root = link;
    for (let depth = 0; depth < 6 && root.parentElement; depth += 1) {
      const parent = root.parentElement;
      const text = compact(parent.innerText || parent.textContent);
      if (text.length > 750) break;
      root = parent;
      if (courseRe.test(text) && typeRe.test(text) && timeRe.test(text)) break;
    }
    add(root, syntheticCompletedHref(link.href), link.href);
  }

  for (const node of document.querySelectorAll("li,tr,[role='listitem'],[class*='ui-li'],[class*='row'],[class*='activity']")) {
    if (!visible(node)) continue;
    const text = compact(node.innerText || node.textContent);
    if (!courseRe.test(text) || !typeRe.test(text) || !timeRe.test(text)) continue;
    const entry = node.querySelector?.("a[href*='Entry.aspx']");
    add(node, syntheticCompletedHref(entry?.href), entry?.href || "");
  }

  const markers = document.querySelectorAll("[class*='glyphicon-ok'],[class*='ui-icon-check'],[class*='icon-ok'],[class*='check'],[class*='complete'],[class*='success'],[data-icon*='check'],[title*='complete' i],[aria-label*='complete' i],[aria-label*='check' i],svg,i,img,span");
  for (const marker of markers) {
    const clue = compact(`${marker.getAttribute?.("class") || ""} ${marker.getAttribute?.("data-icon") || ""} ${marker.getAttribute?.("title") || ""} ${marker.getAttribute?.("aria-label") || ""} ${marker.getAttribute?.("alt") || ""} ${marker.getAttribute?.("src") || ""}`);
    if (!completionClue(clue)) continue;
    let root = marker;
    for (let depth = 0; depth < 8 && root.parentElement; depth += 1) {
      root = root.parentElement;
      const text = compact(root.innerText || root.textContent);
      if (text.length > 750) break;
      if (courseRe.test(text) && typeRe.test(text) && timeRe.test(text)) {
        const entry = root.querySelector?.("a[href*='Entry.aspx']");
        add(root, true, entry?.href || "");
        break;
      }
    }
  }

  return { ready: Boolean(document.body), sessions: sessions.map(({ key, ...row }) => row) };
})