import argparse
from pathlib import Path

from TESTS.epg_runner import epg_grab, find_site_for_tvg_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help=r'Chemin vers le repo epg (ex: "C:\...\epg")')
    ap.add_argument("--site", default="", help='Nom du site (doit exister dans SITES.md, ex: "tvhebdo.com")')
    ap.add_argument("--out", default="guide.xml", help='Fichier XML de sortie (relatif = dans le repo)')
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=600)

    ap.add_argument("--find-tvg", default="", help='Optionnel: tvg-id à rechercher pour deviner le site')
    args = ap.parse_args()

    repo = Path(args.repo)

    if args.find_tvg:
        hits = find_site_for_tvg_id(repo, args.find_tvg)
        if not hits:
            print(f"[FIND] Aucun match pour tvg-id: {args.find_tvg}")
            print("Essaye un autre tvg-id (ex: CFUTDT.ca@SD, CIVMDT.ca@SD, etc.)")
            return
        print(f"[FIND] tvg-id trouvé dans {len(hits)} fichier(s):")
        for p in hits[:50]:
            print(" -", p)
        print("\n➡️ Regarde le dossier après `sites\\` dans le chemin: c'est souvent ton --site.")
        return

    if not args.site:
        print("Erreur: --site manquant. Utilise --find-tvg pour t'aider, ou choisis un site dans SITES.md.")
        return

    rc = epg_grab(repo=repo, site=args.site, out_xml=args.out, days=args.days, timeout_s=args.timeout)
    print(f"[DONE] exit_code={rc}")


if __name__ == "__main__":
    main()
