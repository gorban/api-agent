/* Managed workflow diagram overlay. Runs only in finite loopback document wrappers. */
(() => {
  "use strict";

  if (window.top !== window.self) return;
  const declaration = document.querySelector('meta[name="workflow-managed-svg-routes"]');
  const staticView = !declaration && location.protocol === "file:" &&
    document.querySelector('meta[name="workflow-view"]')?.content === "v2";
  if (declaration && !document.currentScript?.src.endsWith("/.private_resources/managed-modal.js")) return;
  let admitted = null;
  let diagramRoot = null;
  if (staticView) {
    const script = document.currentScript;
    if (!script?.src.endsWith("/.private_resources/viewer/flow-modal.js")) return;
    diagramRoot = new URL("../diagrams/", script.src);
  } else {
    if (declaration?.content !== "v1") return;
    try {
      const paths = window.__apiAgentManagedSvgRoutes;
      delete window.__apiAgentManagedSvgRoutes;
      if (!Array.isArray(paths) || paths.length > 1000 ||
          !paths.every(path => typeof path === "string" && path.startsWith("/r/") && path.endsWith(".svg") &&
            !/[?#\\]/.test(path))) return;
      admitted = new Set(paths);
    } catch { return; }
  }

  const ordinary = event => event.button === 0 && !event.metaKey && !event.ctrlKey &&
    !event.altKey && !event.shiftKey;
  const safePart = part => {
    if (!/^(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2})+$/.test(part)) return false;
    try {
      const decoded = decodeURIComponent(part);
      const canonical = encodeURIComponent(decoded).replace(/[!'()*]/g,
        character => "%" + character.charCodeAt(0).toString(16).toUpperCase());
      return decoded !== "." && decoded !== ".." && !/[\\/?#\x00-\x1f\x7f]/.test(decoded) &&
        canonical === part;
    } catch { return false; }
  };
  const admittedUrl = value => {
    try {
      const url = new URL(value, location.href);
      if (url.search || url.hash) return null;
      if (!staticView) return url.origin === location.origin && admitted.has(url.pathname) ? url : null;
      if (url.protocol !== "file:" || !url.href.startsWith(diagramRoot.href) || !url.pathname.endsWith(".svg")) return null;
      const suffix = url.href.slice(diagramRoot.href.length);
      return suffix.length <= 2048 && suffix.split("/").every(safePart) ? url : null;
    } catch { return null; }
  };
  let active = null;

  function disposeFrame(state) {
    state.listenerController?.abort();
    state.listenerController = null;
    state.frame?.remove();
    state.frame = null;
  }

  function close(state) {
    if (active !== state) return;
    active = null;
    state.generation += 1;
    clearTimeout(state.timer);
    state.observer?.disconnect();
    state.messageController?.abort();
    disposeFrame(state);
    if (state.dialog.open) state.dialog.close();
    state.dialog.remove();
    if (state.focus?.isConnected) state.focus.focus();
  }

  function fit(state) {
    if (active !== state || !state.dimensions) return;
    const { width, height } = state.dimensions;
    const availableWidth = Math.max(1, state.stage.clientWidth - 24);
    const availableHeight = Math.max(1, state.stage.clientHeight - 24);
    const scale = Math.min(availableWidth / width, availableHeight / height) * state.zoom;
    const canvasWidth = width * scale;
    const canvasHeight = height * scale;
    state.canvas.style.width = `${canvasWidth}px`;
    state.canvas.style.height = `${canvasHeight}px`;
    state.frame.style.transform = `scale(${scale})`;
    state.percent.textContent = `${Math.round(state.zoom * 100)}%`;
    state.minus.disabled = state.zoom <= 0.25;
    state.plus.disabled = state.zoom >= 8;
  }

  function dimensions(frame) {
    const svg = frame.contentDocument?.documentElement;
    if (!svg || svg.localName !== "svg") return null;
    const values = svg.getAttribute("viewBox")?.trim().split(/[\s,]+/).map(Number);
    if (!values || values.length !== 4 || !values.every(Number.isFinite)) return null;
    const [,, width, height] = values;
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384 || width * height > 50_000_000) return null;
    return { width, height };
  }

  function message(state, text, target) {
    if (active !== state) return;
    state.status.hidden = false;
    state.status.setAttribute("role", "alert");
    state.status.textContent = text;
    const fallback = document.createElement("a");
    fallback.href = target.href;
    fallback.textContent = "Open standalone diagram";
    state.status.append(fallback);
    state.canvas.hidden = true;
    disposeFrame(state);
  }

  function followFrameLink(state, event) {
    if (!ordinary(event) || active !== state) return;
    const anchor = event.target.closest?.("a");
    if (!anchor || anchor.getAttribute("target") === "_blank") return;
    const href = anchor.getAttribute("href") || anchor.getAttribute("xlink:href");
    if (!href) return;
    let destination;
    try { destination = new URL(href, state.frame.contentDocument.URL); } catch { return; }
    if (destination.origin !== location.origin) return;
    if (destination.pathname === new URL(state.frame.src).pathname && destination.hash) {
      let node;
      try { node = state.frame.contentDocument.getElementById(decodeURIComponent(destination.hash.slice(1))); }
      catch { return; }
      if (!node) return;
      event.preventDefault();
      node.scrollIntoView({ block: "center", inline: "center" });
      return;
    }
    const nested = admittedUrl(destination.href);
    if (nested) {
      event.preventDefault();
      load(state, nested);
      return;
    }
    if (!destination.search && destination.pathname.startsWith("/r/") &&
        /\.(?:md\.html|json|toml)$/.test(destination.pathname)) {
      event.preventDefault();
      close(state);
      location.assign(destination.href);
    }
  }

  function load(state, url) {
    if (active !== state) return;
    const generation = ++state.generation;
    clearTimeout(state.timer);
    state.dimensions = state.staticDimensions;
    state.zoom = 1;
    state.canvas.hidden = true;
    state.status.hidden = false;
    state.status.setAttribute("role", "status");
    state.status.textContent = "Loading diagram…";
    disposeFrame(state);
    const frame = document.createElement("iframe");
    const listenerController = new AbortController();
    state.listenerController = listenerController;
    frame.title = "Interactive workflow diagram";
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("scrolling", "no");
    state.frame = frame;
    frame.addEventListener("load", () => {
      if (active !== state || state.generation !== generation) return;
      if (staticView) return; // The isolated file frame must send its trusted ready signal.
      if (frame.contentDocument?.URL !== url.href) return;
      clearTimeout(state.timer);
      const size = state.staticDimensions || dimensions(frame);
      if (!size) { message(state, "Diagram dimensions are unavailable.", url); return; }
      state.dimensions = size;
      frame.width = String(size.width);
      frame.height = String(size.height);
      if (!staticView) {
        frame.contentDocument.addEventListener("click", event => followFrameLink(state, event),
          { capture: true, signal: listenerController.signal });
        frame.contentDocument.addEventListener("keydown", event => {
          if (event.key === "Escape") { event.preventDefault(); close(state); }
        }, { signal: listenerController.signal });
      }
      state.status.hidden = true;
      state.canvas.hidden = false;
      fit(state);
    }, { signal: listenerController.signal });
    frame.addEventListener("error", () => {
      if (active === state && state.generation === generation) {
        clearTimeout(state.timer);
        message(state, "Diagram could not be loaded.", url);
      }
    }, { signal: listenerController.signal });
    state.timer = setTimeout(() => {
      if (active === state && state.generation === generation) {
        message(state, "Diagram loading timed out.", url);
      }
    }, 20000);
    frame.src = url.href;
    state.canvas.append(frame);
  }

  function open(url, focus, image) {
    if (typeof ResizeObserver !== "function" || typeof AbortController !== "function" ||
        typeof HTMLDialogElement !== "function" ||
        typeof HTMLDialogElement.prototype.showModal !== "function" ||
        typeof HTMLDialogElement.prototype.close !== "function") return false;
    if (active) close(active);
    const dialog = document.createElement("dialog");
    dialog.className = "workflow-flow-modal";
    dialog.setAttribute("aria-label", "Workflow diagram");
    dialog.innerHTML = '<div class="workflow-flow-stage"><div class="workflow-flow-canvas" hidden></div></div>' +
      '<p class="workflow-flow-status" role="status">Loading diagram…</p>' +
      '<div class="workflow-flow-controls" aria-label="Diagram controls">' +
      '<button type="button" data-action="minus" aria-label="Zoom out">−</button>' +
      '<output aria-label="Zoom relative to fitted size">100%</output>' +
      '<button type="button" data-action="plus" aria-label="Zoom in">+</button>' +
      '<button type="button" data-action="fit" aria-label="Fit diagram">Fit</button>' +
      '<button type="button" data-action="close" aria-label="Close diagram">×</button></div>';
    document.body.append(dialog);
    const stage = dialog.querySelector(".workflow-flow-stage");
    if (getComputedStyle(stage).position !== "absolute") { dialog.remove(); return false; }
    const state = { dialog, stage, canvas: dialog.querySelector(".workflow-flow-canvas"),
      status: dialog.querySelector(".workflow-flow-status"),
      percent: dialog.querySelector("output"),
      minus: dialog.querySelector('[data-action="minus"]'), plus: dialog.querySelector('[data-action="plus"]'),
      frame: null, listenerController: null, dimensions: null, generation: 0, zoom: 1,
      focus, timer: null, observer: null, messageController: null,
      staticDimensions: staticView ? { width: image.naturalWidth, height: image.naturalHeight } : null };
    active = state;
    try {
      state.observer = new ResizeObserver(() => fit(state));
      if (staticView) {
        state.messageController = new AbortController();
        window.addEventListener("message", event => {
          if (active !== state || event.source !== state.frame?.contentWindow) return;
          if (event.data === "workflow-modal-close-v1") close(state);
          else if (event.data === "workflow-modal-ready-v1" && state.frame && state.dimensions) {
            clearTimeout(state.timer);
            state.frame.width = String(state.dimensions.width);
            state.frame.height = String(state.dimensions.height);
            state.status.hidden = true;
            state.canvas.hidden = false;
            fit(state);
          }
        }, { signal: state.messageController.signal });
      }
      dialog.addEventListener("cancel", event => { event.preventDefault(); close(state); });
      dialog.addEventListener("click", event => {
        if (event.target === dialog || event.target === stage) { close(state); return; }
        const action = event.target.closest?.("button[data-action]")?.dataset.action;
        if (action === "close") close(state);
        else if (action === "fit") { state.zoom = 1; fit(state); }
        else if (action === "minus" || action === "plus") {
          state.zoom = Math.max(0.25, Math.min(8, state.zoom * (action === "plus" ? 1.25 : 0.8)));
          fit(state);
        }
      });
      dialog.showModal();
      state.observer.observe(stage);
      dialog.querySelector('[data-action="close"]').focus();
      load(state, url);
      return true;
    } catch {
      close(state);
      return false;
    }
  }

  document.addEventListener("click", event => {
    if (event.defaultPrevented || !ordinary(event) || active && active.dialog.contains(event.target)) return;
    const anchor = event.target.closest?.("main a[href]");
    if (!anchor || anchor.target === "_blank") return;
    const image = staticView ? anchor.querySelector(':scope > img[data-workflow-admitted-svg="v1"]') : null;
    if (staticView && (!image || !image.complete || !image.naturalWidth || !image.naturalHeight ||
        image.src !== anchor.href || image.naturalWidth > 16384 || image.naturalHeight > 16384 ||
        image.naturalWidth * image.naturalHeight > 50_000_000)) return;
    const url = admittedUrl(anchor.href);
    if (!url) return;
    if (open(url, anchor, image)) event.preventDefault();
  }, true);
})();
