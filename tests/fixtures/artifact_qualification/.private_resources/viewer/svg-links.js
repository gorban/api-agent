/* Local navigation for independently generated standalone workflow graphics. */
(() => {
  "use strict";
  const applyLinks = () => {
  const local = location.protocol === "file:" ||
    ((location.protocol === "http:" || location.protocol === "https:") &&
      ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname));
  if (!local) return;
  const scripts = document.querySelectorAll("script[data-workflow-links]");
  if (scripts.length !== 1 || scripts[0].getAttribute("data-workflow-links") !== "v1") return;
  let mappings;
  try { mappings = JSON.parse(scripts[0].getAttribute("data-workflow-roots")); }
  catch { return; }
  if (!mappings || typeof mappings !== "object" || Array.isArray(mappings)) return;
  const entries = Object.entries(mappings);
  if (!entries.length || entries.length > 64) return;
  const safePart = part => {
    if (!/^(?:[A-Za-z0-9_.~-]|%[0-9a-fA-F]{2})+$/.test(part)) return false;
    try {
      const decoded = decodeURIComponent(part);
      // Decode exactly once. A literal percent remains encoded in the URL;
      // it must never become another decoding pass or a path separator.
      const canonical = encodeURIComponent(decoded).replace(/[!'()*]/g,
        character => "%" + character.charCodeAt(0).toString(16).toUpperCase());
      return decoded !== "." && decoded !== ".." &&
        !/[\\/?#\x00-\x1f\x7f]/.test(decoded) && canonical === part;
    } catch { return false; }
  };
  const roots = [];
  for (const [prefix, relative] of entries) {
    if (!/^\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/blob\/_____switch_branch_____\/(?:[A-Za-z0-9_.~-]+\/)*$/.test(prefix) ||
        typeof relative !== "string" || relative.length > 2048 ||
        !/^(?:\.\.\/|\.\/)*(?:(?:[A-Za-z0-9_.~-]|%[0-9a-fA-F]{2})+\/)*$/.test(relative) ||
        !relative.split("/").slice(0, -1).every(part => part === "." || part === ".." || safePart(part))) return;
    const root = new URL(relative || "./", document.baseURI);
    if (root.protocol !== location.protocol || root.host !== location.host) return;
    roots.push([prefix, root]);
  }
  roots.sort((a, b) => b[0].length - a[0].length);
  for (const link of document.querySelectorAll("a")) {
    const target = link.getAttribute("href") || link.getAttributeNS("http://www.w3.org/1999/xlink", "href") || "";
    for (const [prefix, root] of roots) {
      if (!target.startsWith(prefix)) continue;
      const suffix = target.slice(prefix.length);
      const [path, fragment, ...extra] = suffix.split("#");
      const documentTarget = path.endsWith(".md.html");
      const resourceTarget = path.endsWith(".json") || path.endsWith(".toml");
      if (extra.length || !(documentTarget || (resourceTarget && fragment === undefined)) ||
          path.length > 2048 ||
          !path.split("/").every(safePart)) break;
      const localTarget = new URL(path, root);
      if (!localTarget.href.startsWith(root.href)) break;
      if (fragment !== undefined) localTarget.hash = fragment;
      link.setAttribute("href", localTarget.href);
      link.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", localTarget.href);
      break;
    }
  }
  };
  const notifyReady = () => {
    applyLinks();
    if (location.protocol === "file:" && window.parent !== window)
      window.parent.postMessage("workflow-modal-ready-v1", "*");
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", notifyReady, { once: true });
  else notifyReady();
  if (location.protocol === "file:" && window.parent !== window) {
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        window.parent.postMessage("workflow-modal-close-v1", "*");
      }
    });
  }
})();
