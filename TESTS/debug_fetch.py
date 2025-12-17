import requests

URL = "https://raw.githubusercontent.com/iptv-org/iptv/master/PLAYLISTS.md"
r = requests.get(URL, timeout=20)

print("STATUS:", r.status_code)
print("FINAL URL:", r.url)
print("CONTENT-TYPE:", r.headers.get("content-type"))
print("LENGTH:", len(r.text))

print("\n--- FIRST 40 LINES ---")
for i, line in enumerate(r.text.splitlines()[:40]):
    print(f"{i:02d}:", line)

print("\n--- CONTAINS ---")
needles = [
    "iptv-org.github.io/iptv/",
    "`https://iptv-org.github.io/iptv/",
    "categories/",
    "languages/",
    "countries/",
]
for n in needles:
    print(n, "=>", (n in r.text))
