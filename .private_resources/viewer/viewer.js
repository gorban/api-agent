/* Static workflow presentation. The document's plaintext carrier is inert. */
(() => {
  "use strict";
  const carrier = document.querySelector("plaintext#workflow-source, plaintext");
  if (carrier) carrier.hidden = true;
  if (!carrier || !window.marked || !window.DOMPurify ||
      (document.querySelector('meta[name="workflow-view"]')?.content === "v2" && !window.markedGfmHeadingId)) {
    if (!document.getElementById("workflow-fallback")) {
      const diagnostic = document.createElement("p");
      diagnostic.className = "workflow-viewer-error";
      diagnostic.textContent = "Workflow viewer assets are unavailable. Run ./launch.py --view-flow from the repository root.";
      document.body.append(diagnostic);
    }
    return;
  }
  const local = location.protocol === "file:" ||
    ((location.protocol === "http:" || location.protocol === "https:") &&
      ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname));
  const source = carrier.textContent.startsWith("\n") ? carrier.textContent.slice(1) : carrier.textContent;
  // Only the finite generator-owned alternatives disappear locally. The saved
  // linked image remains in its authored position for all renderers.
  const segmentV2 = "(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+";
  const pathV2 = `(?:\\.\\./)*(?:${segmentV2}/)*${segmentV2}`;
  const blockV2 = new RegExp(
    `<!-- workflow-generated-diagram-v2:start -->\\n` +
    `\\[!\\[Workflow diagram\\]\\((${pathV2}\\.svg)\\)\\]\\(\\1\\)\\n\\n` +
    `<details open>\\n<summary>Diagram links</summary>\\n\\n` +
    `\\[Open standalone diagram\\]\\(\\1\\)(?:\\n\\n- \\[[a-z][a-z0-9_]{0,63}\\]\\(${pathV2}\\.md\\.html\\)(?:\\n- \\[[a-z][a-z0-9_]{0,63}\\]\\(${pathV2}\\.md\\.html\\))*)?` +
    `\\n\\n</details>\\n<!-- workflow-generated-diagram-v2:end -->`, "g");
  let tokenOffset = 0;
  const codeRanges = [];
  for (const token of window.marked.lexer(source)) {
    if (token.type === "code") codeRanges.push([tokenOffset, tokenOffset + token.raw.length]);
    tokenOffset += token.raw.length;
  }
  const presentation = !local ? source : source.replace(blockV2, (block, target, offset) =>
    codeRanges.some(([start, end]) => offset >= start && offset < end) ? block :
      `[![Workflow diagram](${target})](${target})`);
  const headingIds = [];
  let headingMarker = "data-workflow-heading";
  while (presentation.toLowerCase().includes(headingMarker)) headingMarker += "x";
  if (window.markedGfmHeadingId) {
    const extension = window.markedGfmHeadingId.gfmHeadingId();
    const renderHeading = extension.renderer.heading;
    extension.renderer.heading = function (token) {
      const rendered = renderHeading.call(this, token);
      const heading = window.markedGfmHeadingId.getHeadingList().at(-1);
      const index = headingIds.push({ level: heading.level, id: heading.id }) - 1;
      return rendered.replace(/^<h([1-6]) id="[^"]*">/, `<h$1 ${headingMarker}="${index}">`);
    };
    window.marked.use(extension);
  }
  const html = window.marked.parse(presentation, { gfm: true, breaks: false });
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
  for (const heading of template.content.querySelectorAll(`[${headingMarker}]`)) {
    const index = heading.getAttribute(headingMarker);
    const entry = /^(?:0|[1-9][0-9]*)$/.test(index) ? headingIds[Number(index)] : null;
    if (entry && heading.tagName === `H${entry.level}`) heading.id = entry.id;
    heading.removeAttribute(headingMarker);
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
  if (local && generated && title && heading?.textContent === "Generated Diagram v1") {
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
      navigation?.remove();
      heading.remove();
      standalone.remove();
    }
  }
  document.getElementById("workflow-fallback")?.remove();
  const documentHeading = main.querySelector("h1,h2,h3,h4,h5,h6");
  if (documentHeading) document.title = documentHeading.textContent.trim();
  carrier.before(main);
  // IDs become available after parsing; resolve an initial fragment explicitly.
  if (location.hash) {
    try { document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView(); }
    catch { /* A malformed fragment does not prevent viewing the document. */ }
  }
})();
