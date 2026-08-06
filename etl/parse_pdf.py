"""
ETL - Parser dei listati ministeriali in formato PDF.

I PDF del MIT (AM, Superiori, CQC, CAP, Revisioni) condividono lo stesso
layout tabellare:

    Quesito n. <ID> - <Titolo capitolo/argomento>
    | Numero domanda | Testo domanda | Risposta Corretta | Immagine |
    | 26067          | Il pannello...| VERO              | <raster> |

Strategia:
  * pdfplumber  -> estrazione delle tabelle e delle bounding-box di riga
  * PyMuPDF     -> estrazione delle immagini raster con la loro bbox,
                   associate alla riga per sovrapposizione verticale

Output: un JSON normalizzato per listato + immagini deduplicate (SHA-1).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber

QUESITO_RE = re.compile(r"Quesito\s+n[°º]\s*(\d+)\s*[-–]\s*(.+)", re.IGNORECASE)
HEADER_TOKENS = {"numero domanda", "testo domanda", "risposta corretta",
                 "immagine", "numero"}
TRUE_TOKENS = {"VERO", "V", "TRUE"}
FALSE_TOKENS = {"FALSO", "F", "FALSE"}


@dataclass
class Domanda:
    codice: str | None
    testo: str
    corretta: bool
    immagine: str | None = None


@dataclass
class Quesito:
    codice: str
    titolo: str
    pagina: int
    domande: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Immagini
# --------------------------------------------------------------------------- #

def estrai_immagini(pdf_path: Path, media_dir: Path, prange=None) -> dict:
    """{n_pagina: [{'bbox': (x0,y0,x1,y1), 'file': 'ab12.png'}]}

    Le immagini identiche (lo stesso segnale ripetuto su centinaia di domande)
    vengono deduplicate con l'hash del payload binario: si risparmiano ordini
    di grandezza su disco e si abilita il caching HTTP immutabile lato CDN.
    """
    media_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    out = {}
    lo, hi = prange or (0, doc.page_count)
    for pno in range(lo, min(hi, doc.page_count)):
        page = doc[pno]
        entries = []
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            if not xref:
                continue
            try:
                raw = doc.extract_image(xref)
            except Exception:
                continue
            payload, ext = raw["image"], raw["ext"]
            bbox = info["bbox"]
            # logo MIT in testata: piccolo e nel margine alto -> scartato
            if bbox[3] < 80 and bbox[1] < 80:
                continue
            digest = hashlib.sha1(payload).hexdigest()[:16]
            fname = f"{digest}.{ext}"
            fpath = media_dir / fname
            if not fpath.exists():
                fpath.write_bytes(payload)
            entries.append({"bbox": bbox, "file": fname})
        if entries:
            out[pno] = entries
    doc.close()
    return out


def _immagine_per_riga(imgs, top, bottom):
    best, best_ov = None, 0.0
    for im in imgs:
        _, y0, _, y1 = im["bbox"]
        ov = min(bottom, y1) - max(top, y0)
        if ov > best_ov and ov > 0:
            best, best_ov = im["file"], ov
    return best


# --------------------------------------------------------------------------- #
# Parsing tabellare
# --------------------------------------------------------------------------- #

def _norm(cell):
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


def _risposta(val):
    v = _norm(val).upper()
    if v in TRUE_TOKENS:
        return True
    if v in FALSE_TOKENS:
        return False
    return None


def parse_pdf(pdf_path: Path, media_dir: Path, prange=None):
    immagini = estrai_immagini(pdf_path, media_dir, prange)
    quesiti = []
    corrente = None

    with pdfplumber.open(pdf_path) as pdf:
        lo, hi = prange or (0, len(pdf.pages))
        for pno, page in enumerate(pdf.pages):
            if pno < lo or pno >= hi:
                continue
            testo = page.extract_text() or ""
            titoli = [(m.group(1), m.group(2).strip())
                      for m in QUESITO_RE.finditer(testo)]
            posizioni = [w["top"] for w in page.extract_words(use_text_flow=True)
                         if w["text"].lower().startswith("quesito")]
            titoli_pos = []
            for i, (cod, tit) in enumerate(titoli):
                titoli_pos.append((posizioni[i] if i < len(posizioni) else -1.0, cod, tit))

            imgs = immagini.get(pno, [])

            for table in page.find_tables():
                righe_bbox = [r.bbox for r in table.rows]
                dati = table.extract()
                for rb, riga in zip(righe_bbox, dati):
                    celle = [_norm(c) for c in riga]
                    if not celle or all(not c for c in celle):
                        continue
                    if any(c.lower() in HEADER_TOKENS for c in celle):
                        continue

                    corretta, idx_risp = None, None
                    for i, c in enumerate(celle):
                        r = _risposta(c)
                        if r is not None:
                            corretta, idx_risp = r, i
                            break
                    if corretta is None:
                        continue

                    codice = celle[0] if celle[0].isdigit() else None
                    testo_cells = celle[1:idx_risp] if codice else celle[:idx_risp]
                    testo_dom = " ".join(t for t in testo_cells if t).strip()
                    if len(testo_dom) < 5:
                        continue

                    _, top, _, bottom = rb
                    img = _immagine_per_riga(imgs, top, bottom)

                    titolo_att = None
                    for tp, cod, tit in titoli_pos:
                        if tp <= top:
                            titolo_att = (cod, tit)
                    if titolo_att and (corrente is None or corrente.codice != titolo_att[0]):
                        corrente = Quesito(titolo_att[0], titolo_att[1], pno + 1)
                        quesiti.append(corrente)
                    if corrente is None:
                        corrente = Quesito(f"p{pno}", "Non classificato", pno + 1)
                        quesiti.append(corrente)

                    corrente.domande.append(
                        Domanda(codice, testo_dom, corretta, img))

    return [q for q in quesiti if q.domande]


def main(argv):
    if len(argv) < 4:
        print("uso: parse_pdf.py <input.pdf> <output.json> <media_dir> [etichetta] [da:a]")
        return 1
    pdf_path, out_json, media_dir = Path(argv[1]), Path(argv[2]), Path(argv[3])
    etichetta = argv[4] if len(argv) > 4 else pdf_path.stem
    prange = None
    if len(argv) > 5 and ":" in argv[5]:
        a, b = argv[5].split(":")
        prange = (int(a), int(b))
        out_json = out_json.with_suffix(f".{a}-{b}.part.json")

    quesiti = parse_pdf(pdf_path, media_dir, prange)
    n_dom = sum(len(q.domande) for q in quesiti)
    n_img = sum(1 for q in quesiti for d in q.domande if d.immagine)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(
        {"listato": etichetta, "sorgente": pdf_path.name,
         "quesiti": [asdict(q) for q in quesiti]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{etichetta:12s} quesiti={len(quesiti):5d} domande={n_dom:6d} con_immagine={n_img:6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
