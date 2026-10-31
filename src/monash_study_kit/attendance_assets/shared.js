export const ATTENDANCE_URL = "https://attendance.monash.edu.my/student/Default.aspx";

export const DEFAULT_SETTINGS = {
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kuala_Lumpur",
  weekOneMonday: "",
  autoDiscover: true,
  lookbackDays: 7,
  reminder: { weekday: 0, hour: 19, minute: 0 },
  backup: { enabled: true, weekday: 1, hour: 10, minute: 0 },
  courses: []
};

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function dateInfo(date) {
  const value = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 12);
  return {
    iso: `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`,
    key: `${value.getDate()}_${MONTH_ABBR[value.getMonth()]}_${String(value.getFullYear()).slice(-2)}`
  };
}

export function parseDateKey(key) {
  const match = /^(\d{1,2})_([A-Za-z]{3})_(\d{2})$/.exec(String(key || ""));
  if (!match) return null;
  const [, day, mon, yy] = match;
  const month = MONTH_ABBR.findIndex((name) => name.toLowerCase() === mon.toLowerCase());
  if (month === -1) return null;
  return dateInfo(new Date(2000 + Number(yy), month, Number(day), 12));
}

export function mondayOf(date = new Date()) {
  const local = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const offset = (local.getDay() + 6) % 7;
  local.setDate(local.getDate() - offset);
  return local;
}

export function recentAttendanceDates(now = new Date(), count = 7) {
  const dates = [];
  for (let offset = Math.max(1, count); offset >= 0; offset -= 1) {
    dates.push(dateInfo(new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset, 12)));
  }
  // Attendance can list a day's scheduled sessions before that day happens - a pending
  // "?" row rather than a signed-in tick - not just once the day has arrived. Scanning only
  // backward from today missed every later-this-week class entirely (Wednesday couldn't see
  // Thursday's row even though the portal already had it), which looked like those classes
  // had vanished. Extend through Sunday of the current week; anything past that belongs to a
  // future teaching week this window has no business claiming yet.
  const sunday = mondayOf(now);
  sunday.setDate(sunday.getDate() + 6);
  for (
    let cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    cursor <= sunday;
    cursor.setDate(cursor.getDate() + 1)
  ) {
    dates.push(dateInfo(new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate(), 12)));
  }
  return dates;
}

// Counting calendar weeks from Week 1 goes wrong as soon as the semester has a non-teaching
// week: S2 2026 Malaysia's mid-semester break (Mon 21 Sep) made Mon 28 Sep calendar week 10
// while every unit labels it "Week 9", so Ed and Moodle were searched for a Week 10 post that
// did not exist yet. settings.weekAnchors holds {week, monday} pairs read off Moodle's own
// "Week N ... Mon D Month YY" section dates; a date belongs to the latest anchor at or before
// it. Anchors that don't fit this Week 1 (another semester, or a misread date) are ignored.
export function weekAnchors(settings) {
  const one = new Date(`${settings?.weekOneMonday || ""}T00:00:00`);
  if (Number.isNaN(one.getTime())) return [];
  const anchors = [{ week: 1, monday: one }];
  for (const anchor of settings?.weekAnchors || []) {
    const monday = new Date(`${anchor?.monday || ""}T00:00:00`);
    const week = Number(anchor?.week);
    if (!Number.isInteger(week) || week < 2 || Number.isNaN(monday.getTime())) continue;
    const breakWeeks = Math.round((monday.getTime() - one.getTime()) / 604800000) - (week - 1);
    if (breakWeeks < 0 || breakWeeks > 3) continue;
    anchors.push({ week, monday });
  }
  return anchors.sort((a, b) => a.monday - b.monday);
}

export function teachingWeek(settings, now = new Date()) {
  const anchors = weekAnchors(settings);
  if (!anchors.length) return null;
  const monday = mondayOf(now).getTime();
  const anchor = anchors.filter((item) => item.monday.getTime() <= monday).pop() || anchors[0];
  return anchor.week + Math.round((monday - anchor.monday.getTime()) / 604800000);
}

// Moodle's own header for a week (the only thing that reveals a break) is only known for the
// weeks already opened, and it governs just the dates after it. A date still governed by the
// Week 1 anchor therefore might sit before a break nobody has seen yet, which makes the
// calendar-week count one too high - so both labels are candidates until a post-break anchor
// at or before the date settles it.
export function teachingWeekCandidates(settings, now = new Date()) {
  const anchors = weekAnchors(settings);
  if (!anchors.length) return [];
  const monday = mondayOf(now).getTime();
  const anchor = anchors.filter((item) => item.monday.getTime() <= monday).pop() || anchors[0];
  const week = anchor.week + Math.round((monday - anchor.monday.getTime()) / 604800000);
  return anchor.week === 1 && week > 1 ? [week, week - 1] : [week];
}

export function attendanceDate(settings, week, day) {
  const dayOffsets = { Monday: 0, Tuesday: 1, Wednesday: 2, Thursday: 3, Friday: 4, Saturday: 5, Sunday: 6 };
  const anchors = weekAnchors(settings);
  if (!anchors.length || !Number.isFinite(week) || !(day in dayOffsets)) return null;
  const anchor = anchors.filter((item) => item.week <= week).pop() || anchors[0];
  const date = new Date(anchor.monday.getFullYear(), anchor.monday.getMonth(), anchor.monday.getDate(), 12);
  date.setDate(date.getDate() + ((week - anchor.week) * 7) + dayOffsets[day]);
  return dateInfo(date);
}

// Moodle prints each section as "Week 9 ... Mon 28 Sept 26 - Sun 4 Oct 26". Only a range that
// starts on a Monday counts, so a stray date in a section summary can't become an anchor.
export function detectWeekAnchors(text) {
  const re = new RegExp(`\\bweek\\s*(\\d{1,2})\\b(?:(?!\\bweek\\s*\\d)[\\s\\S]){0,200}?\\bmon(?:day)?\\.?,?\\s+(\\d{1,2})\\s+(${MONTH_NAMES_RE})[a-z]*\\.?,?\\s+(\\d{4}|\\d{2})\\b`, "gi");
  const found = new Map();
  for (const match of String(text || "").matchAll(re)) {
    const week = Number(match[1]);
    const month = MONTH_ABBR.findIndex((name) => name.toLowerCase() === match[3].toLowerCase());
    const year = match[4].length === 2 ? 2000 + Number(match[4]) : Number(match[4]);
    const date = new Date(year, month, Number(match[2]), 12);
    if (!week || Number.isNaN(date.getTime()) || date.getDay() !== 1 || found.has(week)) continue;
    found.set(week, dateInfo(date).iso);
  }
  return [...found].map(([week, monday]) => ({ week, monday })).sort((a, b) => a.week - b.week);
}

// Keeps only anchors that change the week count (they sit after a break) and fit this
// semester's Week 1. Returns the merged list, or null when nothing new was learned.
export function mergeWeekAnchors(settings, detected) {
  const current = (settings?.weekAnchors || [])
    .filter((anchor) => weekAnchors({ weekOneMonday: settings?.weekOneMonday, weekAnchors: [anchor] }).length === 2);
  const known = new Set(current.map((anchor) => `${anchor.week}@${anchor.monday}`));
  const plain = { weekOneMonday: settings?.weekOneMonday };
  const fresh = (detected || []).filter((anchor) => {
    if (known.has(`${anchor.week}@${anchor.monday}`)) return false;
    if (weekAnchors({ ...plain, weekAnchors: [anchor] }).length !== 2) return false;
    return teachingWeek(plain, new Date(`${anchor.monday}T12:00:00`)) !== anchor.week;
  });
  if (!fresh.length) return null;
  const byWeek = new Map([...current, ...fresh].map((anchor) => [anchor.week, anchor]));
  return [...byWeek.values()].sort((a, b) => a.week - b.week);
}

export function normalise(value) {
  return String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
}

const MONTH_NAMES_RE = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec";

export function detectWeekOneMonday(text) {
  const re = new RegExp(`\\bweek\\s*1\\b[\\s\\S]{0,400}?\\b(\\d{1,2})\\s+(${MONTH_NAMES_RE})[a-z]*\\.?,?\\s+(\\d{4}|\\d{2})\\b`, "i");
  const match = re.exec(String(text || ""));
  if (!match) return "";
  const month = MONTH_ABBR.findIndex((name) => name.toLowerCase() === match[2].toLowerCase());
  const year = match[3].length === 2 ? 2000 + Number(match[3]) : Number(match[3]);
  const date = new Date(year, month, Number(match[1]), 12);
  if (Number.isNaN(date.getTime())) return "";
  return dateInfo(mondayOf(date)).iso;
}

function stripHash(href) {
  return String(href || "").split("#")[0];
}

export function scopedAttendanceItems(items, courseHints) {
  const allowed = new Set((courseHints || []).map((value) => normalise(value)).filter(Boolean));
  if (!allowed.size) return (items || []).map((item, index) => ({ item, index }));
  return (items || [])
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => allowed.has(normalise(item.course)));
}

export function courseCodesInText(text, courseCodes) {
  const allowed = new Map((courseCodes || []).map((code) => [String(code).toUpperCase(), String(code)]));
  const seen = new Set(String(text || "").toUpperCase().match(/\b[A-Z]{3}\d{4}\b/g) || []);
  return [...seen].filter((code) => allowed.has(code)).map((code) => allowed.get(code));
}

export function findCourseLinks(links, courseCodes, { hrefPattern, normaliseHref = (href) => href }) {
  const codes = (courseCodes || []).map((code) => String(code).toLowerCase());
  const seen = new Map();
  for (const link of links || []) {
    const raw = stripHash(link.href);
    if (!hrefPattern.test(raw)) continue;
    // The clickable label is the anchor's own text (e.g. "FIT2102 1 Programming
    // paradigms") and almost always names just the one course that link points to.
    // The wider DOM "context" climbed a few parents up is there for pages where the
    // course code sits beside the link rather than inside it - but confirmed against
    // the real Ed dashboard and Moodle "My units" page, that climb regularly lands on
    // a shared list/sidebar container whose text is every enrolled unit concatenated
    // together. Matching against that blob tagged every single course link with every
    // requested code instead of just its own. Try the label alone first, and only fall
    // back to the full label+context+href haystack when the label itself names none of
    // the requested courses.
    const labelHaystack = `${link.label} ${raw}`.toLowerCase();
    const labelCourses = codes.filter((code) => labelHaystack.includes(code));
    // A label naming some other real course (e.g. "FIT3143 Parallel computing") is proof
    // this link belongs to that other course, even when FIT3143 isn't one of the codes we
    // want - falling back to the polluted wider context there would wrongly resurrect a
    // match. Only fall back when the label names no course at all, i.e. the code genuinely
    // isn't in the anchor's own text (an Ed thread titled "Week 3 Attendance Codes").
    const labelNamesAnyCourse = /\b[a-z]{3}\d{4}\b/.test(labelHaystack);
    const courses = labelCourses.length || labelNamesAnyCourse
      ? labelCourses
      : codes.filter((code) => `${link.label} ${link.context || ""} ${raw}`.toLowerCase().includes(code));
    if (!courses.length) continue;
    const href = normaliseHref(raw);
    if (!seen.has(href)) seen.set(href, courses);
  }
  return [...seen.entries()].map(([href, courses]) => ({ href, courses }));
}

// Confirmed by hand against the real FIT2102 Ed course: it does not keep one persistent
// "Attendance Codes" thread that gets edited in place, it posts a brand new thread every
// single week ("Week 8 Attendance Codes", "Week 7 International Students Attendance
// Codes", ...). The old flat `.slice(0, 2)` cap was sized for a normal weekly scan, where
// only the current week's thread could possibly matter - but automaticSourceScans is the
// same function a full semester export reuses with `items` spanning every week that still
// needs a code. Against that cap, only the one or two highest-numbered threads a course
// happens to post ever get opened, and every earlier week is silently starved no matter
// how many discussion links this course actually has. When the caller knows which teaching
// weeks still need a code (targetWeeks), pick the single best-matching thread for each of
// those weeks instead of a fixed global count, so a ten-week export can open ten threads
// just as easily as a weekly scan opens one.
export function edThreadLinks(links, targetWeeks) {
  const seen = new Map();
  for (const link of links || []) {
    const href = stripHash(link.href);
    if (!/\/discussion\/\d+/.test(href)) continue;
    const label = `${link.label || ""} ${link.context || ""}`.trim();
    const kind = /attendance/i.test(label) ? 0 : /\bcodes?\b/i.test(label) ? 1 : /\bweek\s*\d+/i.test(label) ? 2 : -1;
    const week = Number(/\bweek\s*(\d{1,2})\b/i.exec(label)?.[1] || 0);
    const priority = kind < 0 ? null : { kind, week };
    if (!priority) continue;
    const previous = seen.get(href);
    if (!previous || priority.kind < previous.kind || (priority.kind === previous.kind && priority.week > previous.week)) seen.set(href, priority);
  }
  const entries = [...seen.entries()].map(([href, priority]) => ({ href, ...priority }));

  const targets = [...new Set((targetWeeks || []).filter(Number.isFinite))];
  if (targets.length) {
    const byWeek = new Map();
    for (const entry of entries) {
      if (!entry.week) continue;
      const previous = byWeek.get(entry.week);
      if (!previous || entry.kind < previous.kind) byWeek.set(entry.week, entry);
    }
    // A target is a computed teaching week. If a break hasn't been learned from Moodle yet
    // that number runs one ahead of the units' own labels, so fall back to the week before;
    // the exact-date rule in matchCodesToAttendance still rejects codes from the wrong week.
    const selected = targets.map((week) => byWeek.get(week) || byWeek.get(week - 1)).filter(Boolean);
    // A course can also run one general, never-week-numbered "Attendance Codes" thread
    // alongside (or instead of) weekly ones - keep the single best one of those too, the
    // same way the no-targets path below already would.
    const general = entries.filter((entry) => !entry.week).sort((a, b) => a.kind - b.kind)[0];
    if (general) selected.push(general);
    const hrefs = [...new Set(selected.map((entry) => entry.href))];
    if (hrefs.length) return hrefs;
  }

  return entries
    .sort((a, b) => a.kind - b.kind || b.week - a.week)
    .map((entry) => entry.href)
    .slice(0, 2);
}

export function moodleWeekLinks(links) {
  const byWeek = new Map();
  for (const link of links || []) {
    const match = /\bweek\s*(\d{1,2})\b/i.exec(link.label || "");
    if (!match) continue;
    const href = stripHash(link.href);
    if (!/section/i.test(href)) continue;
    const week = Number(match[1]);
    if (!byWeek.has(week)) byWeek.set(week, href);
  }
  return byWeek;
}

export function pickWeekNumbers(available, targetWeeks) {
  const weeks = [...available].sort((a, b) => b - a);
  const targets = (targetWeeks || []).filter(Number.isFinite);
  if (targets.length) {
    const wanted = weeks.filter((week) => targets.some((target) => Math.abs(week - target) <= 1));
    if (wanted.length) return wanted;
  }
  return weeks.slice(0, 5);
}

export function inferWeekNumbersFromText(text, attendanceDates = []) {
  const source = String(text || "");
  if (!source) return [];

  const targets = (attendanceDates || []).map((value) => {
    const key = typeof value === "string" ? value : value?.key;
    const parts = String(key || "").split("_");
    if (parts.length !== 3) return null;
    const day = Number(parts[0]);
    const month = MONTH_ABBR.findIndex((name) => name.toLowerCase() === parts[1].slice(0, 3).toLowerCase());
    if (!day || month < 0) return null;
    return { day, month, ordinal: month * 32 + day };
  }).filter(Boolean);

  const weeks = [...source.matchAll(/\bweek\s*(\d{1,2})\b/gi)];
  const found = new Set();
  const dateRe = new RegExp(`\\b(\\d{1,2})\\s+(${MONTH_NAMES_RE})[a-z]*\\b`, "gi");

  weeks.forEach((match, index) => {
    const week = Number(match[1]);
    const next = weeks[index + 1]?.index ?? Math.min(source.length, match.index + 700);
    const nearby = source.slice(match.index, next);
    const dates = [...nearby.matchAll(dateRe)].map((dateMatch) => {
      const month = MONTH_ABBR.findIndex((name) => name.toLowerCase() === dateMatch[2].slice(0, 3).toLowerCase());
      return month < 0 ? null : { day: Number(dateMatch[1]), month, ordinal: month * 32 + Number(dateMatch[1]) };
    }).filter(Boolean);

    if (dates.length >= 2) {
      const low = Math.min(dates[0].ordinal, dates[1].ordinal);
      const high = Math.max(dates[0].ordinal, dates[1].ordinal);
      if (targets.some((target) => target.ordinal >= low && target.ordinal <= high)) found.add(week);
      return;
    }

    if (dates.length === 1 && targets.some((target) => target.ordinal === dates[0].ordinal)) found.add(week);
  });
  return [...found].sort((a, b) => a - b);
}

export function extractCandidates(text, course, week) {
  const clean = text.replace(/\u00a0/g, " ").replace(/[\t ]+/g, " ");
  const lines = clean.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const codeRe = /\b(?=[A-Z0-9]{5}\b)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5}\b/g;
  const hits = [];

  lines.forEach((line, lineIndex) => {
    for (const match of line.matchAll(codeRe)) {
      const from = Math.max(0, lineIndex - 4);
      const to = Math.min(lines.length, lineIndex + 5);
      const context = lines.slice(from, to).join(" · ");
      if (/^(FIT|ECE|ENG|MMA|TRC)\d$/i.test(match[0])) continue;
      hits.push({ code: match[0].toUpperCase(), context, line: line.trim(), lineIndex });
    }
  });

  const weekTokens = [`week ${week}`, `wk ${week}`, `第 ${week} 周`, `第${week}周`];
  return (course.sessions || []).map((session) => {
    const aliases = [session.label, ...(session.aliases || [])].map(normalise).filter(Boolean);
    const ranked = hits.map((hit) => {
      const haystack = normalise(hit.context);
      const sameLine = normalise(hit.line);
      let score = 0;
      if (weekTokens.some((token) => haystack.includes(normalise(token)))) score += 8;
      if (aliases.some((alias) => haystack.includes(alias))) score += 12;
      if (aliases.some((alias) => sameLine.includes(alias))) score += 20;
      if (haystack.includes(normalise(session.day))) score += 3;
      if (sameLine.includes(normalise(session.day))) score += 4;
      if (haystack.includes(session.time)) score += 3;
      if (sameLine.includes(session.time)) score += 4;
      if (haystack.includes(normalise(course.name))) score += 2;
      if (course.category && haystack.includes(normalise(course.category))) score += 2;
      return { ...hit, score };
    }).sort((a, b) => b.score - a.score);

    const best = ranked[0];
    return {
      id: `${course.id}:${session.id}:w${week}`,
      courseId: course.id,
      course: course.name,
      sessionId: session.id,
      session: session.label,
      day: session.day,
      time: session.time,
      week,
      code: best?.score >= 12 ? best.code : "",
      confidence: best?.score >= 20 ? "high" : best?.score >= 12 ? "review" : "missing",
      context: best?.score >= 12 ? best.context.slice(0, 420) : "",
      sourceUrl: course.url
    };
  });
}

const SESSION_TYPE_RE = /\b(workshop|tutorial|studio|applied(?: class)?|practical|laboratory|lab|seminar)\b/i;
const TIME_TOKEN_RE = /\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\b/gi;
// The month-first alternative explicitly refuses a colon after the day token. Without
// that guard, a row such as "15 Sep 8:00AM" could be misread as the target date "Sep 8".
const ANY_DATE_RE = new RegExp(`\\b\\d{1,2}\\s+(?:${MONTH_NAMES_RE})[a-z]*\\b|\\b(?:${MONTH_NAMES_RE})[a-z]*\\s+\\d{1,2}\\b(?!\\s*:)`, "i");

export function normaliseTimeToken(value) {
  TIME_TOKEN_RE.lastIndex = 0;
  const match = TIME_TOKEN_RE.exec(String(value || ""));
  if (!match) return null;
  let hour = Number(match[1]) % 12;
  if (match[3].toLowerCase() === "p") hour += 12;
  return `${String(hour).padStart(2, "0")}:${match[2]}`;
}

function lineHasMatchingTime(line, targetTime) {
  if (!targetTime) return false;
  const tokens = String(line || "").match(TIME_TOKEN_RE) || [];
  return tokens.some((token) => normaliseTimeToken(token) === targetTime);
}

function explicitRowSessionNumbers(row) {
  const value = String(row || "");
  const timeFinder = new RegExp(TIME_TOKEN_RE.source, "gi");
  const time = timeFinder.exec(value);
  if (!time) return new Set();
  const prefix = value.slice(0, time.index);
  const dateFinder = new RegExp(ANY_DATE_RE.source, "i");
  const date = dateFinder.exec(prefix);
  if (!date) return new Set();

  const beforeDate = prefix.slice(0, date.index);
  const afterDate = prefix.slice(date.index + date[0].length);
  const beforeNumbers = [...beforeDate.matchAll(/\b(\d{1,2})\b/g)].map((match) => Number(match[1]));
  const afterNumbers = [...afterDate.matchAll(/\b(\d{1,2})\b/g)].map((match) => Number(match[1]));
  const candidates = new Set();
  if (beforeNumbers.length) candidates.add(beforeNumbers.at(-1));
  if (afterNumbers.length) candidates.add(afterNumbers[0]);
  return candidates;
}

export function matchCodesToAttendance(text, attendanceItems) {
  const clean = String(text || "").replace(/\u00a0/g, " ").replace(/[\t ]+/g, " ");
  const lines = clean.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const codeRe = /\b(?=[A-Z0-9]{5}\b)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5}\b/g;
  const standaloneMonthRe = new RegExp(`^(?:${MONTH_NAMES_RE})[a-z]*\\.?$`, "i");
  const weekdayDayRe = /\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b,?\s+\d{1,2}\b/i;
  // OCR of a narrow date cell ("Wednesday, 30" / "Sep") often drops the first letter of the
  // wrapped month ("ep"). Every month abbreviation minus its first letter is unique, so a
  // lone two-letter line next to a weekday row can only be that month.
  const clippedMonths = { an: "Jan", eb: "Feb", ar: "Mar", pr: "Apr", ay: "May", un: "Jun", ul: "Jul", ug: "Aug", ep: "Sep", ct: "Oct", ov: "Nov", ec: "Dec" };
  const detachedMonthOf = (value) => {
    if (!value) return "";
    if (standaloneMonthRe.test(value)) return value.match(new RegExp(`(?:${MONTH_NAMES_RE})[a-z]*`, "i"))?.[0] || "";
    return clippedMonths[value.replace(/\.$/, "").toLowerCase()] || "";
  };

  // Ed occasionally renders a table cell such as "Wednesday, 16 Sep" as two DOM lines:
  //   Tutorial Wednesday, 16 09 2:00PM UBMP4
  //   Sep
  // Keep the exact-date safety rule, but reconstruct this narrowly-defined layout before
  // matching. Only an immediately adjacent, month-only line is accepted, so unrelated
  // context cannot donate a month and turn an ambiguous candidate into a confident code.
  function repairDetachedMonth(row, lineIndex) {
    if (!row || ANY_DATE_RE.test(row) || !SESSION_TYPE_RE.test(row) || !normaliseTimeToken(row)) return row;
    const dateStem = weekdayDayRe.exec(row);
    if (!dateStem) return row;

    const neighbours = [lines[lineIndex + 1], lines[lineIndex - 1]];
    const month = neighbours.map(detachedMonthOf).find(Boolean);
    if (!month) return row;

    const insertAt = dateStem.index + dateStem[0].length;
    return `${row.slice(0, insertAt)} ${month}${row.slice(insertAt)}`;
  }

  function rowBlock(lineIndex, codeIndex = -1) {
    const current = lines[lineIndex] || "";

    // Browser-rendered Ed text sometimes flattens several visual table rows onto one
    // physical DOM line, for example:
    //   Workshop Monday ... TREW9 · Workshop Wednesday ... SQP3R · Workshop Wednesday ...
    // If we score that whole line, the group-number guard sees the first row's number and
    // can reject the correct later row. Isolate the session-type slice containing this code.
    if (codeIndex >= 0) {
      const typeFinder = new RegExp(SESSION_TYPE_RE.source, "gi");
      const starts = [...current.matchAll(typeFinder)].map((match) => match.index ?? -1).filter((index) => index >= 0);
      const start = starts.filter((index) => index <= codeIndex).at(-1);
      if (Number.isInteger(start)) {
        const end = starts.find((index) => index > codeIndex) ?? current.length;
        const local = current.slice(start, end).trim();
        if (local) return repairDetachedMonth(local, lineIndex);
      }
    }

    let start = lineIndex;
    for (let index = lineIndex; index >= Math.max(0, lineIndex - 6); index -= 1) {
      if (SESSION_TYPE_RE.test(lines[index])) {
        start = index;
        break;
      }
      if (index < lineIndex && (lines[index].match(codeRe) || []).length) break;
    }

    let end = lineIndex;
    for (let index = lineIndex + 1; index < Math.min(lines.length, lineIndex + 7); index += 1) {
      if (SESSION_TYPE_RE.test(lines[index])) break;
      if ((lines[index].match(codeRe) || []).length) break;
      end = index;
    }
    return repairDetachedMonth(lines.slice(start, end + 1).join(" "), lineIndex);
  }

  const hits = [];
  lines.forEach((line, lineIndex) => {
    for (const match of line.matchAll(codeRe)) {
      if (/^(FIT|ECE|ENG|MMA|TRC)\d$/i.test(match[0])) continue;
      hits.push({
        code: match[0].toUpperCase(),
        line,
        row: rowBlock(lineIndex, match.index ?? -1),
        lineIndex,
        context: lines.slice(Math.max(0, lineIndex - 7), Math.min(lines.length, lineIndex + 8)).join(" · ")
      });
    }
  });

  const typeSource = "workshop|tutorial|studio|applied(?: class)?|practical|laboratory|lab|seminar";
  const segments = clean.split(new RegExp(`(?=\\b(?:${typeSource})\\b)`, "i")).map((part) => part.trim()).filter(Boolean);
  const spacedCodeRe = /\b[A-Z0-9]{1,4}(?:[ \t]+[A-Z0-9]{1,4})+\b/g;
  const timeFinder = new RegExp(TIME_TOKEN_RE.source, "gi");
  for (const segment of segments) {
    if (!SESSION_TYPE_RE.test(segment) || !ANY_DATE_RE.test(segment)) continue;
    const times = [...segment.matchAll(timeFinder)];
    timeFinder.lastIndex = 0;
    if (!times.length) continue;
    const lastTime = times.at(-1);
    const tail = segment.slice((lastTime.index || 0) + lastTime[0].length).trim();
    for (const match of tail.matchAll(spacedCodeRe)) {
      const raw = match[0];
      const compact = raw.replace(/\s+/g, "");
      if (!/^(?=[A-Z0-9]{5}$)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5}$/.test(compact)) continue;
      if (/^(FIT|ECE|ENG|MMA|TRC)\d$/i.test(compact)) continue;
      if (hits.some((hit) => hit.code === compact && normalise(hit.row).includes(normalise(segment).slice(0, 40)))) continue;
      hits.push({ code: compact, line: segment, row: segment, lineIndex: -1, context: segment.slice(0, 1200) });
    }
  }

  const candidates = attendanceItems.map((item) => {
    const course = normalise(item.course);
    const sessionAliases = [item.session, ...(item.aliases || [])].map(normalise).filter(Boolean);
    const dateTokens = [item.attendanceDate?.iso, item.attendanceDate?.key?.replaceAll("_", " ")].map(normalise).filter(Boolean);
    const sessionType = SESSION_TYPE_RE.exec(item.session || "")?.[1]?.toLowerCase();
    const itemTime = normaliseTimeToken(item.time);
    const typeRe = sessionType ? new RegExp(`\\b${sessionType}\\b`, "i") : null;
    const dateParts = String(item.attendanceDate?.key || "").split("_");
    const lineDateRe = dateParts.length === 3
      ? new RegExp(`\\b${Number(dateParts[0])}\\s+${dateParts[1]}[a-z]*\\b|\\b${dateParts[1]}[a-z]*\\s+${Number(dateParts[0])}\\b(?!\\s*:)`, "i")
      : null;
    const sessionNumber = /\b(\d{1,2})\b/.exec(item.session || "")?.[1];
    const numberRe = sessionNumber
      ? new RegExp(`\\b0*${Number(sessionNumber)}\\b(?![:\\d])(?!\\s*(?:${MONTH_NAMES_RE}))`, "i")
      : null;

    const ranked = hits.map((hit) => {
      const context = normalise(hit.context);
      const line = normalise(hit.line);
      const row = normalise(hit.row || hit.line);
      let score = 0;
      if (course && context.includes(course)) score += 14;
      if (course && line.includes(course)) score += 12;
      if (sessionAliases.some((alias) => context.includes(alias))) score += 14;
      if (sessionAliases.some((alias) => line.includes(alias))) score += 14;
      else if ((itemTime || lineDateRe) && sessionAliases.some((alias) => row.includes(alias))) score += 14;
      if (dateTokens.some((token) => context.includes(token))) score += 4;

      const hasType = Boolean(typeRe?.test(row));
      const hasTime = lineHasMatchingTime(row, itemTime);
      const hasDate = Boolean(lineDateRe?.test(row));
      const explicitNumbers = explicitRowSessionNumbers(row);
      const hasNumber = Boolean(sessionNumber)
        ? (explicitNumbers.size
          ? explicitNumbers.has(Number(sessionNumber))
          : Boolean(numberRe?.test((itemTime || lineDateRe) ? row : line)))
        : false;
      const numberConflict = Boolean(sessionNumber && explicitNumbers.size && !hasNumber);
      const dateConflict = Boolean(lineDateRe && ANY_DATE_RE.test(row) && !hasDate);
      const dateMissing = Boolean(lineDateRe && !ANY_DATE_RE.test(row));

      if (hasType && hasTime) score += 26;
      else if (hasTime) score += 8;
      if (lineDateRe) {
        if (hasDate) score += 10;
        else if (dateConflict) score -= 80;
      }
      if (numberConflict) score -= 80;
      else if (hasNumber) score += 8;
      if (hasDate && hasTime && hasNumber) score += 12;
      // A real code row names the class type or its time. Without either, the token is just a
      // five character word near a course code and a date, like the FIT3162 team name MCS21 in
      // an email that also mentioned "Thursday 15 October" and so passed as Studio 01's code.
      const notARow = Boolean(lineDateRe && (sessionType || itemTime) && !hasType && !hasTime);
      return { ...hit, score, numberConflict, dateConflict, dateMissing, notARow, numberVerified: !sessionNumber || hasNumber };
    });
    // Auto-discovered Attendance rows always carry an exact calendar date. A Gmail/Ed/Moodle
    // candidate that lost that date cannot safely be distinguished from another week's class
    // at the same weekday/time, so refuse it instead of guessing from a broader search window.
    return ranked.filter((hit) => !hit.numberConflict && !hit.dateConflict && !hit.dateMissing && !hit.notARow && hit.score >= 24);
  });

  const claims = candidates
    .flatMap((hits, index) => hits.map((hit) => ({ index, ...hit })))
    .sort((a, b) => b.score - a.score);
  const assigned = new Map();
  const usedCodes = new Set();
  for (const claim of claims) {
    if (assigned.has(claim.index) || usedCodes.has(claim.code)) continue;
    const rival = claims.some((other) => other.code === claim.code && other.score === claim.score && other.index !== claim.index && !assigned.has(other.index));
    usedCodes.add(claim.code);
    if (!rival) assigned.set(claim.index, claim);
  }

  return attendanceItems.map((item, index) => {
    const best = assigned.get(index);
    return {
      ...item,
      code: best ? best.code : "",
      confidence: best ? (best.score >= 36 && best.numberVerified ? "high" : "review") : "missing",
      context: best ? best.context.slice(0, 520) : ""
    };
  });
}

// ocr.js reports codes that different OCR passes spelled differently (W47VD and W4ZVD for
// the same cell). Whichever spelling matched, it is only a guess, so it drops to "review"
// and the other spellings go into the context for the student to check against the image.
export function flagOcrDoubts(items, doubts) {
  return (items || []).map((item) => {
    if (!item?.code) return item;
    const group = (doubts || []).find((codes) => codes.includes(item.code));
    if (!group) return item;
    const others = group.filter((code) => code !== item.code);
    return {
      ...item,
      confidence: item.confidence === "high" ? "review" : item.confidence,
      context: `OCR 读法不一致：${[item.code, ...others].join(" / ")}，请打开来源对照原图。${item.context ? ` ${item.context}` : ""}`.slice(0, 620)
    };
  });
}

export function validSchedule(item) {
  return Number.isInteger(item?.weekday) && item.weekday >= 0 && item.weekday <= 6
    && Number.isInteger(item?.hour) && item.hour >= 0 && item.hour <= 23
    && Number.isInteger(item?.minute) && item.minute >= 0 && item.minute <= 59;
}

export function hasUsableConfig(settings) {
  const backupValid = !settings?.backup?.enabled || validSchedule(settings.backup);
  const autoDiscover = settings?.autoDiscover !== false;
  return Boolean(
    validSchedule(settings?.reminder)
    && backupValid
    && (autoDiscover
      || (settings?.weekOneMonday && settings?.courses?.some((course) => course.enabled !== false && course.name && course.url && course.sessions?.length)))
  );
}

export async function loadSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return settings || structuredClone(DEFAULT_SETTINGS);
}

// The full scan/reconciliation round trip can legitimately take a couple of minutes, but it
// must never hang the popup/review/options page forever: the background work is already
// timeout-bounded internally, so anything past a generous ceiling here means the service
// worker was killed or stopped responding, not that the scan is still progressing. Surface
// that as a normal failure instead of leaving the UI stuck on "正在..." indefinitely.
// "未找到" alone is ambiguous: for this week it just means the teacher hasn't posted the
// code yet (normal, keep checking), but for a past week it would read like a missed class or
// a broken scan. Every UI surface labels status by which week it belongs to instead of using
// one generic word for both situations.
export function startOfWeek(date) {
  const value = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const sinceMonday = (value.getDay() + 6) % 7; // getDay(): 0 = Sunday
  value.setDate(value.getDate() - sinceMonday);
  return value;
}

export function weekBucket(item, now = new Date()) {
  const iso = item?.attendanceDate?.iso;
  if (!iso) return "";
  const itemDate = new Date(`${iso}T12:00:00`);
  if (Number.isNaN(itemDate.getTime())) return "";
  const thisMonday = startOfWeek(now);
  if (itemDate >= thisMonday) return "thisWeek";
  const lastMonday = new Date(thisMonday);
  lastMonday.setDate(lastMonday.getDate() - 7);
  return itemDate >= lastMonday ? "lastWeek" : "earlier";
}

export function weekPrefix(bucket) {
  return bucket === "thisWeek" ? "本周" : bucket === "lastWeek" ? "上周" : bucket === "earlier" ? "更早" : "";
}

export function sendMessageWithTimeout(message, ms = 300000) {
  return Promise.race([
    chrome.runtime.sendMessage(message),
    new Promise((_, reject) => setTimeout(() => reject(new Error("扩展没有响应，请重试（可能是后台进程被浏览器回收）")), ms))
  ]);
}

const DEBUG_LOG_KEY = "debugLog";
const DEBUG_LOG_LIMIT = 200;

// The service worker gets torn down and respawned by Chrome between messages, which silently
// disconnects any DevTools "inspect service worker" window that was already open - it just
// stops receiving new console output with no indication anything is wrong. Storage survives
// every respawn, so persisting the log there (and rendering it on the confirm page) is the
// only way to see what actually happened without racing the inspector's connection.
//
// A multi-minute semester export calls this dozens of times from one long-running async
// function, but a weekly alarm can fire and start its own scan in the same window - two
// concurrent callers each doing get-then-set on the same storage key is a lost-update race:
// whichever write lands second overwrites the first caller's entries as if they never
// happened, which can leave the log looking empty even though logging never actually failed.
// Chaining every call onto one promise serialises the read-modify-write so no entry is lost.
let logChain = Promise.resolve();
export function logDebug(message, data) {
  console.log(`[MAH] ${message}`, data);
  logChain = logChain.then(() => writeLogEntry(message, data));
  return logChain;
}
async function writeLogEntry(message, data) {
  try {
    const { [DEBUG_LOG_KEY]: existing = [] } = await chrome.storage.local.get(DEBUG_LOG_KEY);
    const next = [...existing, { at: new Date().toISOString(), message, data }].slice(-DEBUG_LOG_LIMIT);
    await chrome.storage.local.set({ [DEBUG_LOG_KEY]: next });
  } catch {
    // Logging must never be why a real operation fails.
  }
}

export async function readDebugLog() {
  const { [DEBUG_LOG_KEY]: log = [] } = await chrome.storage.local.get(DEBUG_LOG_KEY);
  return log;
}

export async function clearDebugLog() {
  await chrome.storage.local.set({ [DEBUG_LOG_KEY]: [] });
}

