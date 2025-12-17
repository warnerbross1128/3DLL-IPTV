import requests, re

PLAYLISTS_MD_RAW = "https://raw.githubusercontent.com/iptv-org/iptv/master/PLAYLISTS.md"
MD_ITEM_RE = re.compile(r"^\s*(?:-\s*)?(.+?)\s+\d*\s*`(https?://[^`]+\.m3u8?)`")

text = requests.get(PLAYLISTS_MD_RAW, timeout=15).text
print("L0:", text.splitlines()[0])
print("Contains category heading?", "### Grouped by category" in text)
print("Contains language heading?", "### Grouped by language" in text)

# petit parsing rapide
buckets = {"Category": [], "Language": [], "Country": [], "Subdivision/City": []}
section = None
for line in text.splitlines():
    l = line.strip()
    if "### Grouped by category" in l:
        section = "Category"; continue
    if "### Grouped by language" in l:
        section = "Language"; continue
    if "#### Countries" in l:
        section = "Country"; continue
    if "### Grouped by broadcast area" in l:
        section = None; continue

    m = MD_ITEM_RE.match(line)
    if m and section:
        name = m.group(1).strip()
        url = m.group(2).strip()
        if section == "Country" and ("/subdivisions/" in url or "/cities/" in url):
            buckets["Subdivision/City"].append((name, url))
        else:
            buckets[section].append((name, url))

print("Counts:",
      "Category", len(buckets["Category"]),
      "Language", len(buckets["Language"]),
      "Country", len(buckets["Country"]),
      "Subdivision/City", len(buckets["Subdivision/City"]))

print("First category items:", buckets["Category"][:5])
print("First language items:", buckets["Language"][:5])
