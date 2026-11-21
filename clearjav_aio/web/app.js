// ClearJAV Uploader page: shows the tool's upload queue and sends what the member does (see web.py).
"use strict";
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const link = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`;
  // A message with web addresses in it (the torrent ClearJAV already has): the addresses become links.
  const linked = text => esc(text).replace(/https?:\/\/[\w.\-~:/?#=%+]+/g, url => {
    const tail = url.match(/[.:]+$/)?.[0] || "";
    const href = url.slice(0, url.length - tail.length);
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>${tail}`;
  });

  // Members install FFmpeg and MediaInfo themselves: the download pages (the same as in tools.py).
  const DOWNLOADS = {
    ffmpeg: `FFmpeg (${link("https://www.gyan.dev/ffmpeg/builds/", "Windows builds")} · ${link("https://ffmpeg.org/download.html", "all systems")})`,
    mediainfo: link("https://mediaarea.net/en/MediaInfo/Download", "MediaInfo CLI"),
  };
  // Each program is found (or missing) on its own; ffprobe comes with FFmpeg's download.
  const TOOL_NAMES = { mediainfo: "MediaInfo", ffmpeg: "FFmpeg", ffprobe: "ffprobe" };
  const TOOL_DOWNLOADS = { mediainfo: DOWNLOADS.mediainfo, ffmpeg: DOWNLOADS.ffmpeg, ffprobe: DOWNLOADS.ffmpeg };
  const listed = words => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`);

  let state = null;
  let lastLine = 0;
  const open = new Set();
  const rendered = new Map(); // job id -> html
  const drafts = new Map(); // input id -> typed value (kept across redraws)
  const seen = new Map(); // job id -> last state (a video that comes to need an answer opens by itself)
  const failedCovers = new Set(); // cover addresses that didn't load: the stand-in tile stays
  let coversOn = true;
  let pageEpoch = 0; // counts the member's theme and cover changes: an answer asked for before the last one is out of date
  let stopped = false;

  // -- talking to the tool ------------------------------------------------------------------

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-ClearJAV-AIO": "1" },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty answer */ }
    if (!res.ok) throw new Error(data.error || `The uploader answered ${res.status}.`);
    return data;
  }

  async function act(path, body, okText) {
    try {
      const data = await api(path, body);
      if (okText) toast(typeof okText === "function" ? okText(data) : okText, true);
      await refresh();
      return data;
    } catch (err) {
      toast(err.message);
      await refresh();
      return null;
    }
  }

  let toastTimer = 0;
  function toast(text, ok = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("ok", ok);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, ok ? Math.max(3000, text.length * 70) : 7000); // time to read it
  }

  const addedText = d => `Added ${d.added} video${d.added === 1 ? "" : "s"}.${d.skipped ? ` ${d.skipped}.` : ""}`;

  let pollTimer = 0;
  let failures = 0;
  async function refresh() {
    clearTimeout(pollTimer);
    if (stopped) return;
    const epoch = pageEpoch;
    try {
      const res = await fetch(`/api/state?after=${lastLine}`, { credentials: "same-origin" });
      if (res.status === 403) { stopped = true; document.body.textContent = "Open the uploader with the link shown in its window."; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state = await res.json();
      state.pageEpoch = epoch;
      failures = 0;
      $("#goodbye").hidden = true;
      render();
    } catch {
      failures++;
      if (failures >= 3) $("#goodbye").hidden = false; // "The uploader doesn't answer": it keeps trying
    }
    pollTimer = setTimeout(refresh, failures ? 3000 : state && state.running ? 600 : 1200);
  }

  // -- theme: ClearJAV Dark or Light, or as the computer is set until the button is used (saved in the tool's
  // settings). The button has the same half-dark circle as DMM Downloader's and Video Merger's.

  const systemLight = matchMedia("(prefers-color-scheme: light)");
  let appliedTheme = document.documentElement.dataset.theme || "system"; // the server writes the saved theme into the page
  const shownDark = () => (appliedTheme === "system" ? !systemLight.matches : appliedTheme === "dark");

  function applyTheme(theme) {
    appliedTheme = ["dark", "light"].includes(theme) ? theme : "system";
    if (appliedTheme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = appliedTheme;
    showThemeButton();
  }

  function showThemeButton() {
    const dark = shownDark();
    const label = dark ? "Switch to the light theme" : "Switch to the dark theme";
    $("#themeBtn").setAttribute("aria-label", label);
    $("#themeBtn").title = label;
  }

  // Theme and covers change on the page at once, then are saved (an older answer of the tool doesn't undo them).
  function savePage(body) {
    pageEpoch++;
    if ("theme" in body) applyTheme(body.theme);
    if ("covers" in body) {
      coversOn = body.covers;
      if (state) renderQueue();
    }
    act("/api/page", body);
  }

  // -- formatting ---------------------------------------------------------------------------

  function size(bytes) {
    if (!bytes) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let n = bytes;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
  }

  function duration(seconds) {
    if (!seconds) return "";
    const s = Math.round(seconds);
    return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  const PILLS = {
    checking: ["run", "Checking"], ready: ["ready", "Ready"], ask: ["ask", "Needs your answer"], bad: ["bad", "Not accepted"],
    waiting: ["ready", "Waiting"], working: ["run", "Working"], done: ["ok", "Uploaded"], test: ["grey", "Test run done"],
    skipped: ["grey", "Skipped"], failed: ["bad", "Failed"],
  };

  function draft(id, fallback) {
    return drafts.has(id) ? drafts.get(id) : fallback ?? "";
  }

  // Redraws only on a change: replacing a link every second could swallow a click on it.
  const shownHtml = new WeakMap(); // element -> html
  function setHtml(el, html) {
    if (shownHtml.get(el) === html) return;
    el.innerHTML = html;
    shownHtml.set(el, html);
  }

  // -- the queue ----------------------------------------------------------------------------

  function questionBoxes(job) {
    const q = job.questions || [];
    let html = "";
    if (q.includes("content_id")) {
      const s = job.cid_suggestion;
      // A content id found for the DVD-ID in the name is only filled in: the member checks it and uses it.
      const why = s
        ? `The ${esc(job.cid_where)} looks like the DVD-ID <span class="mono">${esc(job.cid_dvd_id)}</span>. ${esc(job.cid_source)} ${/ and /.test(job.cid_source) ? "give" : "gives"} the CID filled in below
           (${link(`https://r18.dev/videos/vod/movies/detail/-/id=${s}/`, "see it on r18.dev")}): check that it is your video (another maker may use the same DVD-ID), then use it.`
          + ((job.cid_others || []).length ? ` Other listing: <span class="mono">${job.cid_others.map(esc).join(", ")}</span> (DMM's DVD listing; ClearJAV uses the digital one).` : "")
        : `Neither the file nor its name tells the DMM CID (content id). Type it (e.g. <span class="mono">ipzz00020</span>); the tool then looks it up${job.cid_guess ? " (the start of the file name is filled in: check it)" : ""}.`;
      html += `<div class="box ask"><b>Which video is this?</b>
        <span>${why}</span>${s ? suggestedVideo(job) : ""}
        <div class="actions">
          <label class="field" for="cid-${job.id}"><span>CID${s ? ` <span class="tag rec">suggested from the DVD-ID</span>` : ""}</span><input type="text" class="mono" id="cid-${job.id}" value="${esc(draft(`cid-${job.id}`, s || job.cid_guess))}" placeholder="abc00123" aria-label="${esc(`CID of ${job.name}${s ? " (suggested from the DVD-ID)" : ""}`)}"></label>
          <button type="button" class="btn primary small" data-act="content_id" data-id="${job.id}">Use this id</button>
          <button type="button" class="btn small" data-act="skip" data-id="${job.id}">Skip this video</button>
        </div></div>`;
    }
    if (q.includes("unknown") || q.includes("dvd_id")) {
      const unknown = q.includes("unknown");
      const dvd = draft(`dvd-${job.id}`, unknown ? (job.dvd_id || job.dmm_dvd_id) : (job.guess || job.dvd_id));
      const date = draft(`date-${job.id}`, unknown ? (job.date || job.dmm_date) : job.date);
      const onSite = (job.existing || []).length;
      const where = job.checked ? `ClearJAV already has ${onSite} torrent${onSite === 1 ? "" : "s"} of it, so it can take yours` : "Not checked against ClearJAV (no API key): only a test run is possible";
      const why = unknown
        ? `r18.dev doesn't know <span class="mono">${esc(job.cid)}</span>. ${where}. ${job.dvd_from ? `The DVD-ID and date below come from ${esc(job.dvd_from)}` : "Nobody gives a DVD-ID or date"}: check them for the title, then confirm.`
        : `r18.dev and DMM have no DVD-ID for <span class="mono">${esc(job.cid)}</span>.${job.guess ? ` The tool worked out <span class="mono">${esc(job.guess)}</span> from the CID.` : ""} Check it on DMM, then confirm.`;
      html += `<div class="box ask"><b>${unknown ? "Unknown on r18.dev" : "No DVD-ID"}</b><span>${why}</span>
        <div class="actions">
          <label class="field" for="dvd-${job.id}"><span>DVD-ID</span><input type="text" class="mono" id="dvd-${job.id}" value="${esc(dvd)}" placeholder="ABC-123" aria-label="${esc(`DVD-ID of ${job.name}`)}"></label>
          ${unknown ? `<label class="field" for="date-${job.id}"><span>Release date</span><input type="text" class="mono" id="date-${job.id}" value="${esc(date)}" placeholder="YYYY-MM-DD" aria-label="${esc(`Release date of ${job.name} (YYYY-MM-DD)`)}"></label>` : ""}
          <button type="button" class="btn primary small" data-act="confirm" data-id="${job.id}">Confirm</button>
          <button type="button" class="btn small" data-act="skip" data-id="${job.id}">Skip this video</button>
        </div></div>`;
    }
    if (q.includes("duplicate")) {
      html += `<div class="box ask"><b>ClearJAV already has ${job.existing.length} torrent${job.existing.length === 1 ? "" : "s"} of this video.</b>
        ${existingTable(job)}
        <span>Upload only if yours is better (for example 2160p when only 1080p is there).</span>
        <div class="actions">
          <button type="button" class="btn small" data-act="upload_anyway" data-id="${job.id}">Upload anyway</button>
          <button type="button" class="btn small" data-act="skip" data-id="${job.id}">Skip this video</button>
        </div></div>`;
    }
    return html;
  }

  // What r18.dev or DMM said about the suggested video: another maker may use the same DVD-ID (KIL-033 is Prestige's
  // 118kil033 on r18.dev), so the member compares it with their file.
  function suggestedVideo(job) {
    const rows = [["Title", job.cid_title], ["Maker", job.cid_maker], ["Released", job.cid_date],
      ["Length", job.cid_runtime ? `${job.cid_runtime} min${job.file_duration ? ` (your file: ${duration(job.file_duration)})` : ""}` : ""]]
      .filter(([, value]) => value);
    return rows.length ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : "";
  }

  function existingTable(job) {
    const rows = (job.existing || []).map(t => `<tr><td>${t.details_link ? link(t.details_link, t.name) : esc(t.name)}</td>
      <td>${esc(t.type)}</td><td>${esc(t.resolution)}</td><td>${esc(t.seeders ?? 0)} seeders</td></tr>`).join("");
    return `<div class="dups"><table>${rows}</table></div>`;
  }

  // The torrents ClearJAV already has, where no question about them is shown (test runs, refused, answered).
  function existingInfo(job) {
    const n = (job.existing || []).length;
    if (!n || (job.questions || []).includes("duplicate") || !["ready", "ask", "bad", "test"].includes(job.state)) return "";
    const anyway = job.upload_anyway || state.options.allow_duplicates;
    const what = job.state === "bad" || job.state === "test" ? "" // the reason, or the test run's note, says it
      : state.options.dry_run ? (anyway ? "A real upload would go ahead, as you chose."
        : "A test run doesn't ask; a real upload would ask whether to upload yours anyway.")
      : anyway ? "Yours is uploaded anyway, as you chose." : "";
    return `<div class="box info"><b>ClearJAV already has ${n} torrent${n === 1 ? "" : "s"} of this video.</b>
      ${existingTable(job)}${what ? `<span>${what}</span>` : ""}</div>`;
  }

  function detailOf(job) {
    let html = "";
    if (job.cid) {
      const parts = job.files.length;
      const kind = job.kind === "REMUX" ? `Remux from ${esc(job.source)}` : "WEB-DL from DMM";
      const scan = job.interlaced && job.kind === "REMUX" ? ", interlaced" : "";
      const renames = (job.renames || []).length && !["done", "test", "failed"].includes(job.state)
        ? job.renames.map(([a, b]) => `<div><span>${esc(a)}</span> → ${esc(b)}</div>`).join("")
        : `<div>${esc(job.files.join(", "))}${["done", "test"].includes(job.state) ? "" : " <span>(name already right)</span>"}</div>`;
      const from = job.dvd_from ? ` <span class="faint">(${esc(job.dvd_from)})</span>` : "";
      html += `<dl class="kv">
        <dt>Video</dt><dd>${kind} · ${esc(job.width)}×${esc(job.height)}${scan} · ${esc(job.video)} · ${esc(job.audio)} · ${duration(job.duration)}${parts > 1 ? ` in ${parts} parts` : ""}</dd>
        <dt>DVD-ID</dt><dd>${esc(job.dvd_id || job.guess || "—")}${from}${job.date ? ` · released ${esc(job.date)}` : ""}</dd>
        <dt>Files</dt><dd class="renames">${renames}</dd>
        <dt>Torrent</dt><dd>${parts > 1 ? `a folder with the ${parts} parts only` : "one file"} · ${size(job.size)}</dd>
        <dt>Where</dt><dd class="mono">${esc(job.target)}</dd>
      </dl>`;
      if (job.kind === "REMUX" && ["ready", "ask"].includes(job.state)) {
        const sources = ["DVD", "VHS", "Blu-Ray", "3D Blu-Ray", "UHD Blu-Ray"];
        html += `<div class="box ask"><label class="field" for="source-${job.id}"><span>Remux source${job.source_chosen ? "" : " (worked out from the name or size: check it)"}</span>
          <select id="source-${job.id}" data-source="${job.id}" aria-label="${esc(`Remux source of ${job.name}`)}">${sources.map(s => `<option${s === job.source ? " selected" : ""}>${s}</option>`).join("")}</select></label></div>`;
      }
    } else {
      html += `<dl class="kv"><dt>Where</dt><dd class="mono">${esc(job.target)}</dd>${job.size ? `<dt>Size</dt><dd>${size(job.size)}</dd>` : ""}</dl>`;
    }
    if (job.state === "ready" || job.state === "ask") html += questionBoxes(job);
    if (job.state === "bad" || job.state === "failed") {
      // Only where the CID can be what's wrong (unknown, not found, can't tell), not for an ISO, a rip or codecs.
      const other = job.state === "bad" && job.files.length && job.cid_problem
        ? `<label class="field" for="cid-${job.id}"><span>Wrong CID? Check with this one</span><input type="text" class="mono" id="cid-${job.id}" value="${esc(draft(`cid-${job.id}`, job.cid || ""))}" placeholder="abc00123" aria-label="${esc(`Another CID for ${job.name}`)}"></label>
           <button type="button" class="btn small" data-act="content_id" data-id="${job.id}">Use this id</button>` : "";
      // No "Check again" where it can't change anything: what the files are decides (an ISO, a part on its own ...).
      const again = job.final ? "" : `<button type="button" class="btn small" data-act="retry" data-id="${job.id}">Check again</button>`;
      html += `<div class="box bad"><span>${linked(job.message)}</span><div class="actions">${other}
        ${again}
        <button type="button" class="btn small" data-act="remove" data-id="${job.id}">Remove from the list</button></div></div>`;
    }
    html += existingInfo(job);
    if ((job.state === "working" || job.state === "checking" || job.state === "done" || job.state === "test" || job.state === "failed") && job.steps.length) {
      const now = job.state === "working" || job.state === "checking";
      html += `<ul class="steps">${job.steps.map((s, i) => `<li class="${now && i === job.steps.length - 1 ? "now" : ""}">${esc(s)}</li>`).join("")}</ul>`;
    }
    if (job.state === "done") {
      const page = job.page ? `<a href="${esc(job.page)}" target="_blank" rel="noopener noreferrer">${esc(job.page)}</a>` : "";
      // The site answers "Torrent uploaded successfully." also when the upload waits for staff: the tool says that instead.
      const said = job.waits_for_staff ? `${esc(job.message)}${page ? ` (${page})` : ""}` : `${esc(job.message.replace(/\.$/, ""))}${page ? `: ${page}` : ""}`;
      html += `<div class="box ok"><span>${said}</span>
        ${job.seed ? `<span>Torrent to seed: <span class="mono">${esc(job.seed)}</span></span>` : ""}</div>`;
    }
    if (job.state === "test") {
      html += `<div class="box ok"><span>Test run: the contact sheet, MediaInfo and torrent are ready. Nothing was uploaded.</span>
        <div class="actions"><button type="button" class="btn small" data-act="retry" data-id="${job.id}">Check again to upload it</button></div></div>`;
    }
    for (const w of job.warnings || []) html += `<div class="warn">⚠ ${esc(w)}</div>`;
    return `<div class="detail" id="detail-${job.id}">${html}</div>`;
  }

  // The right of a row: the state, then the size, the step and its progress, or the torrent's number on ClearJAV.
  function sideOf(job, cls, label) {
    const pill = `<span class="pill ${cls}">${label}</span>`;
    const step = job.steps[job.steps.length - 1] || "";
    if (job.state === "working" && job.progress != null) {
      return `${pill}<span>${Math.round(job.progress * 100)}%${step ? ` · ${esc(step)}` : ""}</span><progress class="bar" max="1" value="${job.progress}" aria-label="Progress"></progress>`;
    }
    if (job.state === "working" || job.state === "checking") return `${pill}<span>${esc(step)}</span>`;
    const torrent = job.state === "done" && /\/torrents\/(\d+)/.exec(job.page || "");
    if (torrent) return `${pill}<span class="mono">#${torrent[1]}</span>`;
    const parts = job.files.length > 1 ? ` · ${job.files.length} parts` : "";
    return `${pill}<span>${size(job.size)}${parts}</span>`;
  }

  // The stand-in tile's letters (until the cover loads, or without one): the DVD-ID's (IPZZ), else the CID's or name's.
  function coverLabel(job) {
    const dvd = job.dvd_id || job.guess || job.cid_dvd_id || "";
    if (dvd) return dvd.split(/[-\s]/)[0].slice(0, 6);
    const word = /^[a-z]+/i.exec((job.cid || job.name || "").replace(/^(h_)?\d+/i, ""));
    return word ? word[0].slice(0, 6).toUpperCase() : "?";
  }

  // DMM's cover comes from the uploader (/cover/<id>), never from DMM directly; ?cid= is new when the video changes.
  const coverUrl = job => `/cover/${job.id}?cid=${encodeURIComponent(job.cid || "")}`;

  function coverOf(job) {
    const url = coverUrl(job);
    const img = job.cover && !failedCovers.has(url) ? `<img src="${esc(url)}" alt="" decoding="async">` : "";
    return `<div class="cover" aria-hidden="true">${esc(coverLabel(job))}${img}</div>`;
  }

  // A closed row that needs an answer says the question in one line.
  function askNote(job) {
    const q = job.questions || [];
    if (q.includes("content_id")) return job.cid_suggestion ? "Which video is this? A CID is suggested." : "Which video is this? Type its CID.";
    if (q.includes("unknown")) return "Unknown on r18.dev: check the DVD-ID and date, then confirm.";
    if (q.includes("dvd_id")) return job.guess ? "No DVD-ID: check the one worked out from the CID." : "No DVD-ID: type it.";
    if (q.includes("duplicate")) return "ClearJAV already has this video: upload anyway or skip?";
    return job.message || "";
  }

  function jobHtml(job) {
    const [cls, label] = PILLS[job.state] || ["grey", job.state];
    const chips = job.cid ? `<span class="chip cid">${esc(job.cid)}</span><span class="chip">${esc(job.kind)}</span>${job.kind === "REMUX" ? `<span class="chip">${esc(job.source)}</span>` : ""}
      <span class="chip">${esc(job.resolution)}</span><span class="chip">${esc(job.video)}</span><span class="chip">${esc(job.audio)}</span>` : "";
    const title = job.title || job.name;
    const shown = open.has(job.id);
    // The reason of a refused video (or the question) is in its box when open: the row says it in one line while closed.
    const answer = ["bad", "failed", "ask"].includes(job.state);
    const waits = !shown && job.state === "done" && job.waits_for_staff; // "Uploaded: waits for staff approval"
    const note = !shown && answer ? (job.state === "ask" ? askNote(job) : job.message) : waits ? job.message : !job.cid && !(shown && answer) ? job.message || job.name : "";
    return `<div class="itemrow" data-open="${job.id}" role="button" tabindex="0" aria-expanded="${shown}" aria-controls="detail-${job.id}"
        aria-label="${esc(`${title}: ${label}. ${shown ? "Hide" : "Show"} the details`)}">
        ${coversOn ? coverOf(job) : ""}
        <div class="main"><div class="title">${esc(title)}</div><div class="chips">${chips}</div>${note && note !== title ? `<div class="rownote" title="${esc(note)}">${esc(note)}</div>` : ""}</div>
        <div class="side">${sideOf(job, cls, label)}</div>
      </div>${shown ? detailOf(job) : ""}`;
  }

  function renderQueue() {
    const queue = $("#queue");
    const jobs = state.jobs;
    queue.classList.toggle("covers", coversOn);
    queue.classList.toggle("no-covers", !coversOn);
    if (!jobs.length) {
      queue.innerHTML = `<div class="empty">No videos yet. Paste a folder or file above, or use Browse.</div>`;
      rendered.clear();
      return;
    }
    if (queue.querySelector(".empty")) queue.innerHTML = "";
    const focused = document.activeElement && queue.contains(document.activeElement) ? document.activeElement : null;
    // Only a field being typed in holds back the redraw of its video; a clicked row gets its focus back afterwards.
    const typing = focused && focused.matches("input, select, textarea") ? focused : null;
    const focusedRow = focused && focused.matches("[data-open]") ? focused.dataset.open : null;
    const ids = new Set(jobs.map(j => j.id));
    for (const el of $$(".item", queue)) if (!ids.has(+el.dataset.id)) { el.remove(); rendered.delete(+el.dataset.id); }
    let previous = null;
    for (const job of jobs) {
      if (seen.get(job.id) !== job.state && ["ask", "bad", "failed"].includes(job.state)) { open.add(job.id); rendered.delete(job.id); }
      seen.set(job.id, job.state);
      let el = queue.querySelector(`.item[data-id="${job.id}"]`);
      if (!el) {
        el = document.createElement("div");
        el.className = "item";
        el.dataset.id = job.id;
      }
      const html = jobHtml(job);
      if (rendered.get(job.id) !== html && !(typing && el.contains(typing))) {
        const cover = el.querySelector(".cover img");
        el.innerHTML = html;
        rendered.set(job.id, html);
        // The cover already there is kept, not loaded again: a working video's row is redrawn with every bit of progress.
        const drawn = el.querySelector(".cover img");
        if (cover && drawn && cover.getAttribute("src") === drawn.getAttribute("src")) drawn.replaceWith(cover);
      }
      const after = previous ? previous.nextSibling : queue.firstChild;
      if (after !== el) queue.insertBefore(el, after);
      previous = el;
    }
    if (focusedRow && !queue.contains(focused)) queue.querySelector(`[data-open="${focusedRow}"]`)?.focus();
  }

  function renderCounts() {
    const c = states => state.jobs.filter(j => states.includes(j.state)).length;
    $("#counts").innerHTML = [
      [c(["checking"]), "checking"], [c(["ready", "waiting"]), "ready"], [c(["ask"]), "need an answer"], [c(["bad"]), "not accepted"],
      [c(["working"]), "working"], [c(["done", "test"]), "done"], [c(["failed"]), "failed"], [c(["skipped"]), "skipped"],
    ].filter(([n]) => n).map(([n, l]) => `<span class="pill grey">${n} ${l}</span>`).join("") || "<span>Empty list</span>";
    const asks = c(["ask"]);
    $("#askCount").hidden = !asks;
    setHtml($("#askCount"), asks ? `${asks}<span class="sr-only"> need${asks === 1 ? "s" : ""} an answer</span>` : "");
    const ready = c(["ready"]);
    const dry = state.options.dry_run;
    $("#startBtn").disabled = !ready || state.running;
    $("#startText").textContent = state.running ? "Working…" : `${dry ? "Test" : "Upload"} ${ready} video${ready === 1 ? "" : "s"}`;
    $("#stopBtn").hidden = !state.running;
    $("#stopBtn").disabled = state.stopping;
    $("#stopBtn").textContent = state.stopping ? "Stopping after this video…" : "Stop after this video";
  }

  // -- options and settings -------------------------------------------------------------------

  function setValue(el, value) {
    if (el === document.activeElement) return;
    if (el.type === "checkbox") el.checked = !!value;
    else if (el.type === "radio") el.checked = el.value === value;
    else el.value = value ?? "";
  }

  function renderOptions() {
    const o = state.options;
    for (const el of $$("[data-opt]")) setValue(el, o[el.dataset.opt]);
    $("#tagField").hidden = !o.personal_release;
    const flags = state.flags;
    const trusted = flags ? !!flags.skips_moderation : false;
    $("#modqField").hidden = !trusted;
    $("#modqNote").hidden = !state.user || trusted || !flags;
    $("#staffPanel").hidden = !(flags && flags.internal_options);
    $("#featuredField").hidden = !(flags && flags.featured);
    $("#staffTag").textContent = state.user ? state.user.group || "" : "";
  }

  const unreachable = () => /^can't reach /.test(state.user_error || "");

  function renderStatus() {
    const s = state.settings;
    $("#version").textContent = state.version;
    const who = $("#whoStatus");
    if (s.starting) { who.textContent = "Checking your API key…"; who.className = "stat off"; }
    else if (state.user) { who.innerHTML = `Signed in as&nbsp;<b>${esc(state.user.name)}</b>&nbsp;(${esc(state.user.group)})`; who.className = "stat"; }
    else { who.textContent = !s.has_key ? "No API key (test runs only)" : unreachable() ? "ClearJAV can't be reached" : "API key refused"; who.className = "stat bad"; }
    const seed = { save: "Saves the .torrent to seed", watch: "Watch folder", qbittorrent: "qBittorrent" }[s.seed_mode];
    $("#seedStatus").textContent = seed;
    $("#seedStatus").className = "stat";
    const t = state.tools;
    const missing = Object.keys(t.errors || {});
    $("#toolStatus").textContent = s.starting ? "" : missing.length ? `Missing: ${missing.map(m => TOOL_NAMES[m]).join(", ")}` : "MediaInfo, FFmpeg";
    $("#toolStatus").className = missing.length ? "stat bad" : "stat";

    const notes = [];
    if (!s.starting && missing.length) {
      const downloads = [...new Set(missing.map(m => TOOL_DOWNLOADS[m]))];
      notes.push(`${listed(missing.map(m => TOOL_NAMES[m]))} not found. Download ${downloads.join(" and ")}, then see Settings → Programs.`);
    }
    if (!s.starting && !state.user) {
      notes.push(!s.has_key ? "Save your ClearJAV API key in Settings to upload. Test runs work without it."
        : unreachable() ? `ClearJAV can't be reached right now (${esc(state.user_error)}). Check the internet connection, then Settings → Check setup again.`
        : `ClearJAV refused the saved API key (${esc(state.user_error)}). Save it again in Settings.`);
    }
    if (!s.starting && s.has_announce && !s.announce_ok) notes.push("The saved announce URL doesn't look right: save it again in Settings.");
    setHtml($("#setupNote"), notes.join("<br>"));
    $("#setupNote").hidden = !notes.length;

    const cantSave = `Can't be saved here (${s.store_problem || "unknown reason"}): set it in the environment instead`;
    $("#keyState").textContent = !s.has_key ? (s.store ? "No key saved" : `${cantSave} (CLEARJAV_API_KEY).`)
      : `${s.key_from_env ? "From the environment variable CLEARJAV_API_KEY" : `Saved in ${s.store_where}`} · `
        + (state.user ? `signed in as ${state.user.name}` : `${unreachable() ? "not checked" : "refused"}: ${state.user_error}`);
    $("#announceState").textContent = !s.has_announce ? (s.store ? "Not saved (torrents are made without your passkey; the site adds it)" : `${cantSave} (CLEARJAV_ANNOUNCE_URL).`)
      : `${s.announce_from_env ? "From the environment variable CLEARJAV_ANNOUNCE_URL" : `Saved in ${s.store_name}`}${s.announce_ok ? "" : ", but it doesn't look right"}`;
    for (const el of $$(".storeName")) el.textContent = s.store_name;
  }

  let seedFilled = false;
  function renderSettings() {
    const s = state.settings;
    if (!seedFilled) {
      seedFilled = true;
      for (const r of $$("input[name=seed]")) r.checked = r.value === s.seed_mode;
      $("#watchDir").value = s.watch_dir;
      $("#cleanUp").checked = !!s.clean_up;
      $("#qbUrl").value = s.qbittorrent_url;
      $("#qbUser").value = s.qbittorrent_user;
      $("#qbCat").value = s.qbittorrent_category;
      $("#pathMediainfo").value = s.mediainfo;
      $("#pathFfmpeg").value = s.ffmpeg;
      $("#pathFfprobe").value = s.ffprobe;
      showSeedFields();
    }
    $("#qbPass").placeholder = s.has_qb_password ? "saved (type to replace)" : "empty if none";
    const t = state.tools;
    const row = (name, path, error) => `<span>${name}</span><span class="path">${esc(path || error || "")}</span>${path ? `<span class="ok-t">found</span>` : `<span class="bad-t">missing</span>`}`;
    $("#toolList").innerHTML = s.starting ? "<span>Looking…</span>" : row("MediaInfo", t.mediainfo, t.errors.mediainfo) + row("FFmpeg", t.ffmpeg, t.errors.ffmpeg) + row("ffprobe", t.ffprobe, t.errors.ffprobe);
    setHtml($("#toolHelp"), `Not installed? Download ${DOWNLOADS.ffmpeg} and ${DOWNLOADS.mediainfo}. The uploader doesn't include them. `
      + `Put ffmpeg, ffprobe and MediaInfo next to the uploader or on PATH, or give their places under "Use other copies", then "Check setup again".`);
    $("#filesNote").innerHTML = `Settings file: <span class="mono">${esc(s.config_path)}</span><br>Log (without keys or passkeys): <span class="mono">${esc(s.log_dir)}</span>`;
    $("#logNote").textContent = `Without keys or passkeys · ${s.log_dir}`;
  }

  function renderPage() {
    if (state.pageEpoch !== pageEpoch) return; // asked for before the member's last change: out of date
    const s = state.settings;
    if (s.theme !== appliedTheme) applyTheme(s.theme);
    coversOn = s.covers !== false;
    setValue($("#coversOn"), coversOn);
  }

  function renderLog() {
    const lines = state.log.filter(([id]) => id > lastLine);
    if (!lines.length) return;
    const box = $("#log");
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    box.insertAdjacentHTML("beforeend", lines.map(([, t, text]) => `<span class="t">${esc(t)}</span>  ${esc(text)}\n`).join(""));
    lastLine = lines[lines.length - 1][0];
    if (atEnd) box.scrollTop = box.scrollHeight;
  }

  function render() {
    renderStatus();
    renderOptions();
    renderSettings();
    renderPage();
    renderQueue();
    renderCounts();
    renderLog();
  }

  function showSeedFields() {
    const mode = ($("input[name=seed]:checked") || {}).value || "save";
    $("#watchFields").hidden = mode !== "watch";
    $("#qbFields").hidden = mode !== "qbittorrent";
    $("#qbTest").hidden = mode !== "qbittorrent";
  }

  // -- browsing folders -------------------------------------------------------------------------

  const FOLDER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  let browseMode = "add"; // add, or the option to fill with a folder (output_dir, watch_dir)
  let browsePath = "";

  async function browseTo(path) {
    try {
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`, { credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      browsePath = data.path;
      $("#browsePath").textContent = data.path || "This computer";
      $("#browseUp").disabled = data.parent == null;
      $("#browseUp").dataset.path = data.parent ?? "";
      const pickFolder = browseMode !== "add";
      $("#browseList").innerHTML = data.entries.map((e, i) => {
        const tick = !pickFolder && data.path ? `<input type="checkbox" value="${i}" aria-label="Add ${esc(e.name)}">` : "";
        const name = e.dir ? `<button type="button" class="open" data-go="${i}">${FOLDER}${esc(e.name)}</button>` : `<span class="name">${esc(e.name)}</span>`;
        if (pickFolder && !e.dir) return "";
        return `<div class="entry${e.name.toLowerCase().endsWith(".iso") ? " iso" : ""}">${tick}${name}<span class="sz">${e.dir ? "" : size(e.size)}</span></div>`;
      }).join("") || `<div class="entry"><span class="name">No folders${pickFolder ? "" : " or videos"} here.</span></div>`;
      $("#browseList").entries = data.entries;
      $("#browseOk").disabled = pickFolder && !data.path;
    } catch (err) {
      toast(err.message);
    }
  }

  function openBrowse(mode) {
    browseMode = mode;
    $("#browseTitle").textContent = mode === "add" ? "Choose videos or folders" : "Choose a folder";
    $("#browseOk").textContent = mode === "add" ? "Add selected" : "Use this folder";
    $("#browseHint").textContent = mode === "add" ? "Click a folder to open it; tick videos or folders to add them." : "Open the folder, then use it.";
    $("#browse").hidden = false;
    $("#browseCancel").focus();
    const start = mode === "add" ? ($("#addPath").value.trim() || state?.last_folder || "") : (mode === "watch_dir" ? $("#watchDir").value : state.options.output_dir) || state?.last_folder || "";
    browseTo(start);
  }

  // -- events -------------------------------------------------------------------------------------

  $$(".tabs button").forEach(b => b.addEventListener("click", () => {
    $$(".tabs button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
    ["upload", "settings", "log"].forEach(t => { $("#tab-" + t).hidden = t !== b.dataset.tab; });
    if (b.dataset.tab === "log") { const box = $("#log"); box.scrollTop = box.scrollHeight; }
  }));

  document.addEventListener("change", e => {
    const el = e.target;
    if (el.dataset.opt) {
      let value = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "radio" && !el.checked) return;
      act("/api/options", { [el.dataset.opt]: value });
    }
    if (el.dataset.source) act("/api/answer", { id: +el.dataset.source, action: "source", source: el.value });
    if (el.name === "seed") showSeedFields();
  });

  document.addEventListener("input", e => {
    if (e.target.id && $("#queue").contains(e.target)) drafts.set(e.target.id, e.target.value);
  });

  $("#queue").addEventListener("click", e => {
    const button = e.target.closest("[data-act]");
    if (button) {
      document.activeElement?.blur(); // a focused answer box holds back redraws of its video
      const id = +button.dataset.id;
      const action = button.dataset.act;
      if (action === "remove") return act("/api/remove", { id });
      const body = { id, action };
      if (action === "confirm") {
        body.dvd_id = ($(`#dvd-${id}`) || {}).value || "";
        body.date = ($(`#date-${id}`) || {}).value || "";
      }
      if (action === "content_id") body.content_id = ($(`#cid-${id}`) || {}).value || "";
      if (action === "retry") open.add(id);
      return act("/api/answer", body).then(ok => { if (ok) [`dvd-${id}`, `date-${id}`, `cid-${id}`].forEach(k => drafts.delete(k)); });
    }
    if (e.target.closest("input, button, select, a, label")) return;
    const row = e.target.closest("[data-open]");
    if (row) {
      const id = +row.dataset.open;
      open.has(id) ? open.delete(id) : open.add(id);
      rendered.delete(id);
      renderQueue();
    }
  });
  // A cover that doesn't load (DMM has none, no network): the stand-in tile stays, never a broken picture.
  $("#queue").addEventListener("error", e => {
    const img = e.target;
    if (img instanceof HTMLImageElement && img.closest(".cover")) {
      failedCovers.add(img.getAttribute("src"));
      img.remove();
    }
  }, true);
  $("#queue").addEventListener("keydown", e => {
    const row = e.target.closest("[data-open]");
    if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); row.click(); }
    const input = e.target.closest(".box input");
    if (input && e.key === "Enter") { e.preventDefault(); input.closest(".box").querySelector(".btn.primary")?.click(); }
  });

  $("#addForm").addEventListener("submit", e => {
    e.preventDefault();
    const path = $("#addPath").value.trim().replace(/^"(.*)"$/, "$1");
    if (!path) return openBrowse("add");
    act("/api/add", { paths: [path], recursive: $("#recursive").checked }, addedText)
      .then(ok => { if (ok) $("#addPath").value = ""; });
  });
  $("#browseBtn").addEventListener("click", () => openBrowse("add"));
  $$("[data-pick]").forEach(b => b.addEventListener("click", () => openBrowse(b.dataset.pick)));
  $("#browseList").addEventListener("click", e => {
    const go = e.target.closest("[data-go]");
    if (go) browseTo($("#browseList").entries[+go.dataset.go].path);
  });
  $("#browseUp").addEventListener("click", e => browseTo(e.currentTarget.dataset.path));
  $("#browseCancel").addEventListener("click", () => { $("#browse").hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#browse").hidden) $("#browse").hidden = true; });
  $("#browseOk").addEventListener("click", () => {
    const entries = $("#browseList").entries || [];
    if (browseMode === "add") {
      const paths = $$("#browseList input:checked").map(i => entries[+i.value].path);
      if (!paths.length) { if (browsePath) paths.push(browsePath); else return; }
      act("/api/add", { paths, recursive: $("#recursive").checked }, addedText);
    } else if (browseMode === "watch_dir") {
      $("#watchDir").value = browsePath;
    } else {
      act("/api/options", { [browseMode]: browsePath });
    }
    $("#browse").hidden = true;
  });

  $("#startBtn").addEventListener("click", () => act("/api/start", {}));
  $("#stopBtn").addEventListener("click", () => act("/api/stop", {}));
  $("#clearBtn").addEventListener("click", () => act("/api/clear", {}));

  $("#keySave").addEventListener("click", async () => {
    const ok = await act("/api/key", { key: $("#keyInput").value }, "API key checked and saved.");
    if (ok) $("#keyInput").value = "";
  });
  $("#keyForget").addEventListener("click", () => act("/api/key/forget", {}, "API key removed."));
  $("#announceSave").addEventListener("click", async () => {
    const ok = await act("/api/announce", { url: $("#announceInput").value }, "Announce URL saved.");
    if (ok) $("#announceInput").value = "";
  });
  $("#announceForget").addEventListener("click", () => act("/api/announce/forget", {}, "Announce URL removed."));

  const seedBody = () => ({
    seed_mode: ($("input[name=seed]:checked") || {}).value || "save", watch_dir: $("#watchDir").value,
    qbittorrent_url: $("#qbUrl").value, qbittorrent_user: $("#qbUser").value, qbittorrent_category: $("#qbCat").value,
    qbittorrent_password: $("#qbPass").value, clean_up: $("#cleanUp").checked,
  });
  $("#seedSave").addEventListener("click", async () => {
    const ok = await act("/api/seeding", seedBody(), "Seeding saved.");
    if (ok) $("#qbPass").value = "";
  });
  $("#qbTest").addEventListener("click", async () => {
    $("#seedState").textContent = "Asking qBittorrent…";
    const data = await act("/api/qbittorrent/test", seedBody());
    $("#seedState").textContent = data ? `Connected: qBittorrent ${data.version}` : "";
  });
  $("#toolsSave").addEventListener("click", () => act("/api/tools", { mediainfo: $("#pathMediainfo").value, ffmpeg: $("#pathFfmpeg").value, ffprobe: $("#pathFfprobe").value }, "Saved."));
  $("#doctorBtn").addEventListener("click", () => act("/api/doctor", {}, "Setup checked (see the Log)."));
  $("#quitBtn").addEventListener("click", async () => {
    const ok = await act("/api/quit", {});
    if (ok) { stopped = true; $("#goodbye").hidden = false; }
  });

  $("#themeBtn").addEventListener("click", () => savePage({ theme: shownDark() ? "light" : "dark" }));
  $("#coversOn").addEventListener("change", e => savePage({ covers: e.target.checked }));
  systemLight.addEventListener("change", showThemeButton);
  $("#footHost").textContent = location.host;
  showThemeButton();

  refresh();
})();
