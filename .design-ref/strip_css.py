from pathlib import Path
import re

css = Path(__file__).with_name("extracted.css").read_text(encoding="utf-8")
stripped = re.sub(r"@font-face\{[^}]+\}", "", css)
Path(__file__).with_name("extracted-layout.css").write_text(stripped, encoding="utf-8")
print("layout css chars", len(stripped))
print("lines", stripped.count("\n") + 1)
