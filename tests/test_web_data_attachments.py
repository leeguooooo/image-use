"""Execute the real composer JS against synthetic attachment states."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest

loader = importlib.machinery.SourceFileLoader("image_use_upload_fixture", str(Path(__file__).resolve().parents[1] / "image-use"))
spec = importlib.util.spec_from_loader(loader.name, loader)
image_use = importlib.util.module_from_spec(spec)
loader.exec_module(image_use)


@unittest.skipUnless(shutil.which("node"), "Node executes the page JS fixture")
class DataAttachmentState(unittest.TestCase):
    def test_data_previews_require_tray_decode_enabled_send_and_no_pending_upload(self):
        cases = [
            ({}, (1, 1)),
            ({"busy": True}, (1, 0)),
            ({"disabled": True}, (1, 0)),
            ({"ariaDisabled": True}, (1, 0)),
            ({"decoded": False}, (1, 0)),
            ({"inTray": False}, (0, 0)),
            ({"src": "blob:https://chatgpt.com/preview"}, (1, 0)),
            ({"src": "https://chatgpt.com/backend-api/estuary/content?id=fake"}, (1, 1)),
        ]
        harness = r'''
const {script, cases, selectors} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const results = cases.map(cfg => {
  const img = {src: cfg.src || 'data:image/png;base64,fixture',
    complete: cfg.decoded !== false, naturalWidth: cfg.decoded === false ? 0 : 1254,
    closest: () => cfg.inTray === false ? null : {}};
  const send = {disabled: !!cfg.disabled,
    getAttribute: () => cfg.ariaDisabled ? 'true' : null};
  const form = {querySelectorAll: () => [img],
    querySelector: s => s === selectors.send ? send : (cfg.busy ? {} : null)};
  const composer = {value: 'fixture prompt', childNodes: [], closest: () => form};
  const document = {querySelector: () => composer, querySelectorAll: () => []};
  return JSON.parse(require('vm').runInNewContext(script, {document}, {timeout: 1000}));
});
process.stdout.write(JSON.stringify(results));
'''
        selectors = image_use._js_composer_selectors()
        result = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"script": image_use._JS_WEB_COMPOSER_STATE % selectors,
                              "selectors": json.loads(selectors), "cases": [c for c, _ in cases]}),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for (case, expected), state in zip(cases, json.loads(result.stdout)):
            with self.subTest(case=case):
                self.assertEqual((state["attachments"], state["ready"]), expected)
