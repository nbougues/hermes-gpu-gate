#!/usr/bin/env python3
"""Met à jour les réglages gpu-gate d'un config.yaml Hermes, idempotemment.

    python3 apply-settings.py ~/.hermes/config.yaml [autre config.yaml ...]
    python3 apply-settings.py --check ~/.hermes/config.yaml      # sans rien écrire

Pourquoi ces deux valeurs seulement, et pas un réagencement complet :

  * `default_mode: low` → `normal`. `low` fait attendre la session tant que l'arbitre
    ne dit pas `free`. Avec l'ancienne règle (`max_inflight: 1`), deux sessions
    interactives s'attendaient mutuellement alors que le moteur, lui, a 4 slots et
    sert de front — et surtout : depuis le passage du pool KV à 1,57 M de positions,
    5 sessions de 150k restent à 100 % de rétention (mesuré). Sérialiser l'interactif
    ne protège plus rien, ça ajoute de la latence.

  * `mode_by_platform.cron: ultralow` → `low`. `ultralow` exige 30 s de silence total
    du moteur à l'entrée de tour : un cron attendait le calme plat alors que ses tours
    sont courts et n'écrasent personne. `iaslot` reste `ultralow` — ce sont les gros
    prefills froids, la seule vraie contention GPU qui reste.

Tout le reste est laissé tel quel : `verdict_url`, `engine_prefixes`, `mode_by_cwd`,
`unattended_mode`. Le script ne touche que l'intérieur du bloc `gpu-gate:` et ne
réécrit jamais le fichier entier (pas de reformatage, pas de perte de commentaires).
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sys
import time

# (regex, remplacement, description) — appliqués uniquement dans le bloc gpu-gate.
# `[ \t]*` et non `\s*` avant `$` : `\s` inclut `\n`, et en fin de bloc la règle
# avalait alors le saut de ligne suivant en collant la clé du dessous
# (`default_mode: normalhooks_auto_accept: false` — vérifié, d'où ce commentaire).
EDITS = [
    (re.compile(r"^([ \t]*)default_mode:[ \t]*low[ \t]*$", re.M),
     r"\1default_mode: normal",
     "default_mode: low -> normal"),
    (re.compile(r"^([ \t]*)cron:[ \t]*ultralow[ \t]*$", re.M),
     r"\1cron: low",
     "mode_by_platform.cron: ultralow -> low"),
]


def block_span(text: str) -> tuple[int, int] | None:
    """Positions du bloc `gpu-gate:` (depuis la clé jusqu'à la clé de même niveau)."""
    m = re.search(r"^([ \t]*)gpu-gate:\s*$", text, re.M)
    if not m:
        return None
    indent = len(m.group(1))
    start = m.start()
    end = len(text)
    for line in re.finditer(r"^([ \t]*)\S.*$", text[m.end():], re.M):
        if len(line.group(1)) <= indent:
            end = m.end() + line.start()
            break
    return start, end


def apply_to(path: str, check: bool = False) -> int:
    original = open(path, encoding="utf-8").read()
    span = block_span(original)
    if span is None:
        print("%s: SKIPPED (pas de bloc gpu-gate:)" % path)
        return 1
    start, end = span
    body = original[start:end]
    new_body, applied = body, []
    for rx, repl, label in EDITS:
        candidate, n = rx.subn(repl, new_body)
        if n:
            new_body = candidate
            applied.append("%s (%d)" % (label, n))
    if new_body == original[start:end]:
        print("%s: déjà à jour" % path)
        return 0
    new_text = original[:start] + new_body + original[end:]
    diff = "".join(difflib.unified_diff(
        original.splitlines(True), new_text.splitlines(True),
        fromfile=path, tofile=path + " (nouveau)"))
    if check:
        print("%s: À METTRE À JOUR\n%s" % (path, diff))
        return 10
    shutil.copy2(path, "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print("%s: %s" % (path, "; ".join(applied) or "modifié"))
    print(diff.rstrip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--check", action="store_true", help="ne rien écrire, signaler seulement")
    args = ap.parse_args()
    rc = 0
    for p in args.paths:
        try:
            rc |= apply_to(p, args.check)
        except OSError as exc:
            print("%s: ERREUR %s" % (p, exc))
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
