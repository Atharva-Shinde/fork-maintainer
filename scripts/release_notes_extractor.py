import json
import os
import re
import requests

class JSON_Builder:

  def __init__(self, title: str, body: str, level: int = 2):
    data = {}

    if level < 4:
      data = self.parse(body, level=level + 1)

    # Strip any child headings (### or ####) from this section's direct content
    content = re.sub(
        r"\n#{2,4}(?!#)\s+.*", "", body, flags=re.DOTALL
    ).strip()

    data = {
        "title": title.strip(),
        "content": content,
        "subsections": data,
    }
    self.data = data

  @classmethod
  def parse(cls, text: str, level: int = 2) -> dict:
    sections = {}
    heading_hashes = level * "#"
    pattern = re.compile(
        rf"^{heading_hashes}(?!#)\s*(?P<title>[^{{\r\n]+?)(?:\s*\{{#(?P<anchor>[^}}]+)\}})?\s*\n(?P<body_text>.*?)(?=\n{heading_hashes}(?!#)|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )

    for m in pattern.finditer(text):
      title = m.group("title")
      key = m.group("anchor") or title.strip()
      body = m.group("body_text")
      sections[key] = cls(title, body, level=level).data

    return sections


all_versions = {}
versions = ["1.18", "1.19","1.20", "1.21", "1.22", "1.23", "1.24", "1.25", "1.26", "1.27"]

for version in versions:
  raw_url = f"https://raw.githubusercontent.com/golang/website/master/_content/doc/go{version}.md"
  content = requests.get(raw_url).text
  all_versions[version] = JSON_Builder.parse(content)

os.makedirs("configs", exist_ok=True)
output_path = os.path.join("configs", "release_notes.json")
with open(output_path, "w", encoding="utf-8") as f:
  json.dump(all_versions, f, indent=2, ensure_ascii=False)

print(f"Saved successfully to {output_path}")
