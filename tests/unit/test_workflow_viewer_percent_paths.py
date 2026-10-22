"""Execute the actual browser assets against bounded URL/DOM substitutes."""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
_HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const location = new URL(input.base);
const images = input.paths.map(path => ({removed:false, dataset:{}, getAttribute: () => path, remove() {this.removed=true;}}));
const links = input.paths.map(path => ({href:input.prefix + path, getAttribute() {return this.href;}, getAttributeNS() {return this.href;}, setAttribute(name,value) {this.href=value;}, setAttributeNS(ns,name,value) {this.xlink=value;}}));
const carrier = {textContent:'# Workflow v2\n', before() {}};
const main = {append() {}, querySelector() {return null;}, querySelectorAll() {return [];}};
const content = {querySelectorAll(selector) {return selector === 'img' ? images : [];}};
const script = {src:new URL('.private_resources/viewer/viewer.js', input.base).href, getAttribute(name) {return name === 'src' ? '.private_resources/viewer/viewer.js' : name === 'data-workflow-links' ? 'v1' : JSON.stringify({[input.prefix]: input.relative});}};
const document = {baseURI:input.base, readyState:'complete', currentScript:script,
 querySelector(selector) {return selector.startsWith('plaintext') ? carrier : null;},
 querySelectorAll(selector) {return selector === 'a' ? links : selector.startsWith('script') ? [script] : [];},
 createElement(name) {return name === 'template' ? {content} : main;}, getElementById() {return null;}};
const window = {marked:{lexer() {return [];}, parse() {return '';}}, DOMPurify:{sanitize() {return '';}}};
window.top = window; window.self = window; window.parent = window;
vm.runInNewContext(fs.readFileSync(input.asset, 'utf8'), {document,window,location,URL});
process.stdout.write(JSON.stringify(input.mode === 'image' ? images.map(image => !image.removed) :
 input.mode === 'image-marker' ? images.map(image => image.dataset.workflowAdmittedSvg || null) :
 links.map(link => ({href:link.href,xlink:link.xlink}))));
'''


def run_asset(mode, paths, *, protocol="file", relative="../../"):
    base = ("file:///tmp/owner/flow.md.html" if mode.startswith("image") else
            "file:///tmp/owner/.private_resources/diagrams/probe.svg")
    if protocol == "http":
        base = base.replace("file:///tmp/owner/", "http://127.0.0.1:8080/r/base/")
    asset = "viewer.js" if mode.startswith("image") else "svg-links.js"
    payload = {"mode": mode, "paths": paths, "base": base, "relative": relative,
               "prefix": "//github.com/example/workflow/blob/_____switch_branch_____/",
               "asset": str(ROOT / ".private_resources/viewer" / asset)}
    result = subprocess.run(["node", "-e", _HARNESS], input=json.dumps(payload), text=True,
                            capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize("protocol", ["file", "http"])
@pytest.mark.parametrize("encoded", ["a%25b", "%25", "%252e%252e", "%252f", "%255c", "a%2525b", "a%20b", "caf%C3%A9", "%21"])
def test_image_guard_preserves_single_decoded_literal_path_segments(protocol, encoded):
    path = f".private_resources/diagrams/{encoded}/{encoded}.svg"
    assert run_asset("image", [path], protocol=protocol) == [True]


@pytest.mark.parametrize("protocol", ["file", "http"])
@pytest.mark.parametrize("encoded", ["a%25b", "%25", "%252e%252e", "%252f", "%255c", "a%2525b", "a%20b", "caf%C3%A9", "%21"])
def test_svg_adapter_keeps_original_percent_encoding_in_local_targets(protocol, encoded):
    path = f"workflows/{encoded}/{encoded}.md.html"
    expected = ("file:///tmp/owner/" if protocol == "file" else
                "http://127.0.0.1:8080/r/base/") + path
    assert run_asset("adapter", [path], protocol=protocol) == [{"href": expected, "xlink": expected}]


@pytest.mark.parametrize("encoded", ["..", "%2E%2E", "%2F", "%5C", "%00", "%1F", "%7F", "%23", "%3F", "%FF", "%C0%AF", "%ED%A0%80", "%", "%2", "%GG", "%61", "%c3%a9"])
def test_image_guard_rejects_traversal_separators_controls_invalid_or_noncanonical_encoding(encoded):
    path = f".private_resources/diagrams/{encoded}/target.svg"
    assert run_asset("image", [path]) == [False]


@pytest.mark.parametrize("encoded", ["..", "%2E%2E", "%2F", "%5C", "%00", "%1F", "%7F", "%23", "%3F", "%FF", "%C0%AF", "%ED%A0%80", "%", "%2", "%GG", "%61", "%c3%a9"])
def test_svg_adapter_leaves_hostile_or_noncanonical_targets_unmapped(encoded):
    path = f"workflows/{encoded}/target.md.html"
    original = "//github.com/example/workflow/blob/_____switch_branch_____/" + path
    assert run_asset("adapter", [path]) == [{"href": original}]


def test_svg_adapter_single_decodes_percent_bearing_owner_mapping():
    result = run_asset("adapter", ["flow.md.html"], relative="../../other%25owner/")
    assert result == [{"href": "file:///tmp/owner/other%25owner/flow.md.html",
                       "xlink": "file:///tmp/owner/other%25owner/flow.md.html"}]


def test_only_owner_local_canonical_diagram_images_receive_modal_marker():
    assert run_asset("image-marker", [".private_resources/diagrams/a%20b.svg",
                                      ".private_resources/diagrams/%2E%2E/outside.svg",
                                      "../sibling/.private_resources/diagrams/other.svg"]) == ["v1", None, None]
