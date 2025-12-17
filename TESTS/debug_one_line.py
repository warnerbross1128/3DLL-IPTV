import requests, re

PLAYLISTS_MD_RAW = "https://raw.githubusercontent.com/iptv-org/iptv/master/PLAYLISTS.md"
text = requests.get(PLAYLISTS_MD_RAW, timeout=15).text

# trouve 5 lignes candidates
candidates = [l for l in text.splitlines() if "iptv-org.github.io/iptv/" in l and "`http" in l]
print("Nb candidates:", len(candidates))
for i, l in enumerate(candidates[:5]):
    print("LINE", i, "repr:", repr(l))
