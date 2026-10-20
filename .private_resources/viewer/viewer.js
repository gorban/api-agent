/* Static workflow presentation. The document's plaintext carrier is inert. */
(() => {
  "use strict";
  const carrier = document.querySelector("plaintext");
  if (!carrier || !window.marked || !window.DOMPurify) {
    if (!document.getElementById("workflow-fallback")) {
      document.body.textContent = "Workflow viewer assets are unavailable. Run ./launch.py --view-flow from the repository root.";
    }
    return;
  }
  const source = carrier.textContent;
  const root = source.startsWith("# Workflow v2\n") || source.startsWith("# Workflow v3\n");
  const html = window.marked.parse(source, { gfm: true, breaks: false });
  const clean = window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "link", "meta", "video", "audio", "source"],
    FORBID_ATTR: ["srcdoc"],
  });
  const main = document.createElement("main");
  main.className = "workflow-document";
  main.innerHTML = clean;
  for (const image of main.querySelectorAll("img")) {
    const source = image.getAttribute("src") || "";
    if (!/^(?:\.\.\/)*\.private_resources\/diagrams\/[a-zA-Z0-9_./-]+\.svg$/.test(source)
        || source.split("/").includes("..") && !source.startsWith("../")) {
      image.remove();
    }
  }
  if (root && !main.querySelector("h2[data-viewer-help]")) {
    const help = document.createElement("aside");
    help.className = "viewer-help";
    const heading = document.createElement("h2");
    heading.textContent = "Viewing this file";
    const note = document.createElement("p");
    note.textContent = "Python 3.13 or newer is required for the managed workflow viewer.";
    const command = document.createElement("code");
    command.textContent = "./launch.py --view-flow";
    help.append(heading, note, command);
    main.prepend(help);
  }
  document.getElementById("workflow-fallback")?.remove();
  carrier.replaceWith(main);
})();
