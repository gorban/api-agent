# Bundled Markdown libraries

- Marked 18.1.1: `public/marked.js` is the unchanged `lib/marked.esm.js` from the official npm tarball. MIT license: [marked-LICENSE.txt](marked-LICENSE.txt). Source: https://github.com/markedjs/marked
- DOMPurify 3.4.17: `public/dompurify.js` is the unchanged `dist/purify.es.mjs` from the official npm tarball. Apache-2.0 or MPL-2.0: [dompurify-LICENSE.txt](dompurify-LICENSE.txt). Source: https://github.com/cure53/DOMPurify

Both tarball SHA-512 digests were verified against npm registry integrity metadata when vendored. The upstream notices are preserved in the files. There is no runtime CDN access or download. The root locked `npm ci` copies these original builds into ignored generated paths and verifies their reviewed hashes. Updates must review the package lock, asset manifest and corresponding license together and rerun browser sanitization tests.

The existing `public/Geist.woff2` and [Geist-OFL.txt](Geist-OFL.txt) remain tracked byte-for-byte as a temporary exception: an exact match in the inspected official npm font releases has not been established. Do not replace its appearance or remove its attribution without verified provenance.
