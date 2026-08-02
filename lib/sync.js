/**
 * HubSync — persist per-app JSON state to the hub's own GitHub repo
 * via the Contents API, so state follows you across devices.
 *
 * Usage:
 *   HubSync.configure({ owner: "walter", repo: "fantasy-hub", token: "github_pat_..." });
 *   const state = await HubSync.load("draft");        // null if none
 *   await HubSync.save("draft", { picks: [...] });    // queued/throttled
 *
 * State lives at data/state/<appId>.json on branch main.
 * Config (including the token) is stored only in this browser's localStorage.
 */
(function () {
  "use strict";

  const CONFIG_KEY = "hub-sync-config";
  const BRANCH = "main";
  const API = "https://api.github.com";

  // ---- config -------------------------------------------------------------

  function readConfig() {
    try {
      const raw = localStorage.getItem(CONFIG_KEY);
      if (!raw) return null;
      const cfg = JSON.parse(raw);
      if (cfg && cfg.owner && cfg.repo && cfg.token) return cfg;
      return null;
    } catch (e) {
      return null;
    }
  }

  function configure(cfg) {
    if (!cfg || !cfg.owner || !cfg.repo || !cfg.token) {
      throw new Error("HubSync.configure requires {owner, repo, token}");
    }
    localStorage.setItem(
      CONFIG_KEY,
      JSON.stringify({
        owner: String(cfg.owner).trim(),
        repo: String(cfg.repo).trim(),
        token: String(cfg.token).trim(),
      })
    );
    refreshBadges();
  }

  function configured() {
    return readConfig() !== null;
  }

  function clearConfig() {
    localStorage.removeItem(CONFIG_KEY);
    refreshBadges();
  }

  // ---- base64 helpers (unicode-safe) --------------------------------------

  function encodeB64(str) {
    const bytes = new TextEncoder().encode(str);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function decodeB64(b64) {
    const bin = atob(b64.replace(/\s/g, ""));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  // ---- API plumbing -------------------------------------------------------

  function contentsUrl(cfg, appId) {
    return (
      API +
      "/repos/" +
      encodeURIComponent(cfg.owner) +
      "/" +
      encodeURIComponent(cfg.repo) +
      "/contents/data/state/" +
      encodeURIComponent(appId) +
      ".json"
    );
  }

  function headers(cfg) {
    return {
      Authorization: "Bearer " + cfg.token,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
  }

  /** GET current file; returns {sha, content} | null on 404. Throws otherwise. */
  async function getFile(cfg, appId) {
    const url =
      contentsUrl(cfg, appId) + "?ref=" + BRANCH + "&_=" + Date.now();
    const res = await fetch(url, { headers: headers(cfg), cache: "no-store" });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error("HubSync GET failed: HTTP " + res.status);
    const body = await res.json();
    return { sha: body.sha, content: decodeB64(body.content || "") };
  }

  // ---- load ---------------------------------------------------------------

  async function load(appId) {
    const cfg = readConfig();
    if (!cfg) return null;
    try {
      const file = await getFile(cfg, appId);
      if (!file) return null;
      return JSON.parse(file.content);
    } catch (e) {
      console.warn("HubSync.load(" + appId + "):", e.message || e);
      return null;
    }
  }

  // ---- save (throttled: latest queued while one is in flight) -------------

  const pending = {}; // appId -> { inFlight: bool, queued: obj|null }

  async function save(appId, obj) {
    const cfg = readConfig();
    if (!cfg) throw new Error("HubSync is not configured");

    const slot = (pending[appId] = pending[appId] || {
      inFlight: false,
      queued: null,
    });

    if (slot.inFlight) {
      // A save is running; remember only the latest state.
      slot.queued = obj;
      return;
    }

    slot.inFlight = true;
    try {
      await putState(cfg, appId, obj);
    } finally {
      slot.inFlight = false;
    }

    if (slot.queued !== null) {
      const next = slot.queued;
      slot.queued = null;
      return save(appId, next);
    }
  }

  async function putState(cfg, appId, obj, isRetry) {
    const existing = await getFile(cfg, appId).catch(function () {
      return null;
    });

    const payload = {
      message: "sync: " + appId + " " + new Date().toISOString(),
      branch: BRANCH,
      content: encodeB64(JSON.stringify(obj, null, 2) + "\n"),
    };
    if (existing && existing.sha) payload.sha = existing.sha;

    const res = await fetch(contentsUrl(cfg, appId), {
      method: "PUT",
      headers: Object.assign({ "Content-Type": "application/json" }, headers(cfg)),
      body: JSON.stringify(payload),
    });

    if (res.status === 409 && !isRetry) {
      // sha went stale under us — re-fetch and retry once.
      return putState(cfg, appId, obj, true);
    }
    if (!res.ok) {
      throw new Error("HubSync save failed: HTTP " + res.status);
    }
  }

  // ---- optional UI badge --------------------------------------------------

  const badges = [];

  function renderBadge(el) {
    const on = configured();
    el.innerHTML = "";
    el.style.cssText =
      "display:inline-flex;align-items:center;gap:6px;cursor:pointer;" +
      "font-size:12px;color:" + (on ? "#34d399" : "#64748b") + ";" +
      "user-select:none;";
    const dot = document.createElement("span");
    dot.style.cssText =
      "width:8px;height:8px;border-radius:50%;background:currentColor;display:inline-block;";
    const label = document.createElement("span");
    label.textContent = on ? "sync on" : "sync off";
    el.appendChild(dot);
    el.appendChild(label);
    el.title = on
      ? "Device sync configured. Click to reconfigure."
      : "Click to set up device sync.";
  }

  function defaultConfigFlow() {
    const cur = readConfig() || {};
    const owner = prompt("GitHub owner (username):", cur.owner || "");
    if (owner === null) return;
    const repo = prompt("Repo name:", cur.repo || "");
    if (repo === null) return;
    const token = prompt(
      "Fine-grained personal access token (Contents: read/write).\n" +
        "Stored only in this browser.",
      ""
    );
    if (token === null) return;
    if (owner && repo && token) {
      configure({ owner: owner, repo: repo, token: token });
    } else if (!owner && !repo && !token) {
      clearConfig();
    } else {
      alert("Sync not configured: all three fields are required.");
    }
  }

  function mountBadge(el, onClickConfigure) {
    badges.push(el);
    renderBadge(el);
    el.addEventListener("click", function () {
      if (typeof onClickConfigure === "function") onClickConfigure();
      else defaultConfigFlow();
      renderBadge(el);
    });
  }

  function refreshBadges() {
    badges.forEach(renderBadge);
  }

  // ---- export -------------------------------------------------------------

  const api = {
    configure: configure,
    configured: configured,
    load: load,
    save: save,
    clearConfig: clearConfig,
    mountBadge: mountBadge,
  };

  if (typeof window !== "undefined") window.HubSync = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
