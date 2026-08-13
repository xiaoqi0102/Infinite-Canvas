const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const canvasJs = fs.readFileSync(path.join(root, 'static', 'js', 'canvas.js'), 'utf8');
const canvasHtml = fs.readFileSync(path.join(root, 'static', 'canvas.html'), 'utf8');
const canvasCss = fs.readFileSync(path.join(root, 'static', 'css', 'canvas.css'), 'utf8');

assert.match(canvasJs, /if\(type === 'prompt'\) return \{w:310, h:250\};/, 'prompt nodes should have a fixed default height');
assert.match(canvasJs, /editor\.addEventListener\('dblclick',[\s\S]*openPromptEditorModal\(node\.id\)/, 'double-click should open the prompt editor');
assert.match(canvasJs, /function savePromptEditorModal\(\)[\s\S]*node\.promptRichText = \{version:1, parts\};[\s\S]*refreshNodes\(\[node\.id\]\)/, 'saving should preserve rich prompt parts and refresh the node');
assert.match(canvasHtml, /id="promptEditorModal"[\s\S]*id="promptEditorInput"[\s\S]*id="promptEditorSave"/, 'prompt editor dialog controls should exist');
assert.match(canvasCss, /\.prompt-editor-dialog-input \{[^}]*height:100%;[^}]*min-height:0;/, 'dialog editor should use the available height and scroll internally');

console.log('canvas prompt editor checks passed');
