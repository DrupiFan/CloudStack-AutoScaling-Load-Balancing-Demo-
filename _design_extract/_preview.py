from pathlib import Path
import re
body = Path("_body.html").read_text(encoding="utf-8")
preview = re.sub(r"data:[^\"']+", "DATA", body)
Path("_body_preview.html").write_text(preview, encoding="utf-8")
print("preview chars", len(preview))
