from pathlib import Path
import base64
import re

p = Path(__file__).with_name("ugt-cloud-demo.html")
text = p.read_text(encoding="utf-8")
out = Path(__file__).parent

s = text.find("<style>")
e = text.find("</style>")
(out / "extracted.css").write_text(text[s + 7 : e], encoding="utf-8")

ss = text.find("<script>")
se = text.rfind("</script>")
(out / "extracted.js").write_text(text[ss + 8 : se], encoding="utf-8")

m = re.search(r'src="data:image/png;base64,([^"]+)"', text)
if m:
    (out / "logo.png").write_bytes(base64.b64decode(m.group(1)))
    print("logo bytes", len(m.group(1)))

# extract symbols + main html without huge img
html_start = text.find("<svg class=")
html_end = text.find("<script>")
body = text[html_start:html_end]
body = re.sub(
    r'<img class="logo" src="data:image/png;base64,[^"]+"',
    '<img class="logo" src="logo.png"',
    body,
    count=1,
)
(out / "extracted-body.html").write_text(body, encoding="utf-8")
print("css", e - s)
print("js", se - ss)
print("body", len(body))
print("files written")
