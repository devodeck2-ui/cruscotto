"""Unisce i frammenti prodotti dal parsing a blocchi in un unico JSON per listato.

Il parsing a pagine e' ripristinabile: ogni blocco produce <nome>.<da>-<a>.part.json.
La fusione riordina per pagina e ricuce i quesiti spezzati a cavallo di due
blocchi (stesso codice ministeriale -> stesso quesito).
"""
import json
import sys
from pathlib import Path


def merge(base: Path, etichetta: str) -> None:
    parti = sorted(base.parent.glob(base.stem + ".*.part.json"),
                   key=lambda p: int(p.name.split(".")[-3].split("-")[0]))
    quesiti, indice = [], {}
    for p in parti:
        d = json.loads(p.read_text(encoding="utf-8"))
        for q in d["quesiti"]:
            key = q["codice"]
            if key in indice:
                visti = {(x["codice"], x["testo"]) for x in indice[key]["domande"]}
                for dom in q["domande"]:
                    if (dom["codice"], dom["testo"]) not in visti:
                        indice[key]["domande"].append(dom)
            else:
                indice[key] = q
                quesiti.append(q)
    out = {"listato": etichetta, "sorgente": base.name, "quesiti": quesiti}
    base.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(q["domande"]) for q in quesiti)
    print(f"{etichetta:8s} parti={len(parti)} quesiti={len(quesiti)} domande={n} -> {base.name}")
    for p in parti:
        p.unlink()


if __name__ == "__main__":
    merge(Path(sys.argv[1]), sys.argv[2])
