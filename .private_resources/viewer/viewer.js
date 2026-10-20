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
  const source = carrier.textContent.startsWith("\n") ? carrier.textContent.slice(1) : carrier.textContent;
  const html = window.marked.parse(source, { gfm: true, breaks: false });
  const clean = window.DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "link", "meta", "video", "audio", "source"],
    FORBID_ATTR: ["srcdoc"],
  });
  const main = document.createElement("main");
  main.className = "workflow-document";
  const template = document.createElement("template");
  template.innerHTML = clean;
  const scriptPath = document.currentScript?.getAttribute("src") || "";
  const scriptMatch = /^((?:\.\.\/)*)\.private_resources\/viewer\/viewer\.js$/.exec(scriptPath);
  const diagramPrefix = scriptMatch ? scriptMatch[1] + ".private_resources/diagrams/" : null;
  const diagramRoot = diagramPrefix ? new URL("../diagrams/", document.currentScript.src).href : null;
  for (const image of template.content.querySelectorAll("img")) {
    const target = image.getAttribute("src") || "";
    const path = diagramPrefix && target.startsWith(diagramPrefix) ? target.slice(diagramPrefix.length) : "";
    const segments = path.split("/");
    let safe = Boolean(path && path.length <= 2048 && path.endsWith(".svg") &&
        segments.every((segment) => /^(?:[a-zA-Z0-9_.~-]|%[0-9a-fA-F]{2})+$/.test(segment)));
    if (safe) {
      try {
        safe = segments.every((segment) => {
          const decoded = decodeURIComponent(segment);
          return decoded !== "." && decoded !== ".." && !/[\\/%?#\x00-\x1f\x7f]/.test(decoded);
        }) && new URL(target, document.baseURI).href.startsWith(diagramRoot);
      } catch {
        safe = false;
      }
    }
    if (!safe) {
      image.remove();
    }
  }
  main.append(template.content);
  // Only the terminal generator-owned section participates in presentation.
  // Its image must already have passed the owner-local image guard above.
  const segment = "(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+";
  const relative = `(?:${segment}/)*${segment}`;
  const nodeLink = `- \\[([a-z][a-z0-9_]{0,63})\\]\\(${relative}\\.md\\.html\\)`;
  const generated = new RegExp(
    `\\n\\n## Generated Diagram v1\\n!\\[Workflow diagram\\]\\((${relative}\\.svg)\\)\\n\\n` +
    `\\[Open standalone diagram\\]\\(\\1\\)(?:\\n\\n${nodeLink}(?:\\n${nodeLink})*)?` +
    "\\n<!-- workflow-generated-diagram-v1:end -->\\n$").exec(source);
  const title = main.querySelector("h1");
  const headings = [...main.querySelectorAll("h2")];
  const heading = headings.at(-1);
  if (generated && title && heading?.textContent === "Generated Diagram v1") {
    const picture = heading.nextElementSibling;
    const standalone = picture?.nextElementSibling;
    const image = picture?.querySelector(":scope > img:only-child");
    const link = standalone?.querySelector(":scope > a:only-child");
    const navigation = standalone?.nextElementSibling;
    if (image && link && image.getAttribute("src") === generated[1] &&
        link.getAttribute("href") === generated[1] &&
        (!navigation || (navigation.tagName === "UL" && !navigation.nextElementSibling))) {
      const clickable = document.createElement("a");
      clickable.href = link.getAttribute("href");
      clickable.append(image);
      picture.replaceChildren(clickable);
      title.after(picture);
      if (navigation) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Diagram links";
        details.append(summary, navigation);
        picture.after(details);
      }
      heading.remove();
      standalone.remove();
    }
  }
  document.getElementById("workflow-fallback")?.remove();
  carrier.replaceWith(main);
})();
