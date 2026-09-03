"""Classi: il gruppo con cui l'allievo segue il corso.

PERCHE' ESISTE
    Prima l'autoscuola aveva un elenco solo, tutti gli allievi insieme, e ogni
    videolezione era visibile a chiunque stesse preparando quella patente. In
    una scuola vera invece i corsi partono a ondate - "Serale B ottobre", "CQC
    sabato mattina" - e il materiale di un corso non deve finire sotto gli occhi
    di un altro, se non altro perche' confonde.

COME E' FATTO
    Un allievo sta in UNA classe per volta (`utenti.classe_id`, che puo' essere
    NULL: iscritto ma non ancora assegnato). E' come funziona in aula, e tiene
    semplici sia la segreteria sia i conteggi.

    Una lezione invece puo' essere aperta a PIU' classi (tabella
    `video_classe`): la stessa registrazione serve spesso a due corsi, e
    duplicarla sarebbe uno spreco. Una lezione senza nessuna classe assegnata
    non la vede nessun allievo - la scelta e' voluta: meglio un video che non
    compare finche' non lo si assegna, che un video finito per sbaglio davanti
    a tutti.

    Cancellare una classe non cancella gli allievi: tornano semplicemente senza
    classe, e le loro schede, risposte e statistiche restano intatte.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..rbac import Principal, require_admin, require_staff

router = APIRouter(prefix="/api/gestione/classi", tags=["classi"])


def classe_di(utente_id: int) -> int | None:
    r = db.query_one("SELECT classe_id FROM utenti WHERE id = ?", (utente_id,))
    return r["classe_id"] if r else None


def utenti_di_classi(autoscuola_id: int, classe_ids: list[int]) -> list[int]:
    """Allievi attivi che stanno in una di quelle classi.

    Serve per avvisare le persone giuste quando si programma una diretta: senza,
    la notifica partiva a tutti gli allievi della patente, classe o non classe.
    """
    if not classe_ids:
        return []
    segnaposto = ",".join("?" * len(classe_ids))
    righe = db.query(
        f"SELECT u.id FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        f"WHERE u.autoscuola_id = ? AND r.codice = 'allievo' AND u.attivo = 1 "
        f"  AND u.classe_id IN ({segnaposto})", (autoscuola_id, *classe_ids))
    return [r["id"] for r in righe]


def _verifica_classi(autoscuola_id: int, ids: list[int]) -> list[int]:
    """Tiene solo le classi che esistono e appartengono a questa autoscuola.

    Non e' pignoleria: senza il controllo, un id qualsiasi passato dall'esterno
    renderebbe un video visibile alla classe di un'altra scuola.
    """
    # I None si scartano qui: "nessuna classe" arriva come None dal modulo
    # dell'allievo, e int(None) farebbe cadere l'iscrizione.
    puliti = sorted({int(i) for i in (ids or []) if i not in (None, "", 0)})
    if not puliti:
        return []
    segnaposto = ",".join("?" * len(puliti))
    righe = db.query(f"SELECT id FROM classi WHERE autoscuola_id = ? AND id IN ({segnaposto})",
                     (autoscuola_id, *puliti))
    return [r["id"] for r in righe]


class ClasseIn(BaseModel):
    nome: str = Field(min_length=1, max_length=60)
    descrizione: str | None = None
    listato_target: str | None = None
    colore: str | None = None
    attiva: bool = True


@router.get("")
def elenco(includi_inattive: bool = True, p: Principal = Depends(require_staff)):
    """Le classi della scuola, con quanti allievi e quante lezioni ciascuna."""
    sql = ("SELECT c.id, c.nome, c.descrizione, c.listato_target, c.colore, c.attiva,"
           "  (SELECT COUNT(*) FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
           "    WHERE u.classe_id = c.id AND r.codice = 'allievo' AND u.attivo = 1) AS n_allievi,"
           "  (SELECT COUNT(*) FROM video_classe vc WHERE vc.classe_id = c.id) AS n_lezioni "
           "FROM classi c WHERE c.autoscuola_id = ?")
    if not includi_inattive:
        sql += " AND c.attiva = 1"
    sql += " ORDER BY c.attiva DESC, c.nome"
    classi = db.rows_to_dicts(db.query(sql, (p.autoscuola_id,)))
    senza = db.query_one(
        "SELECT COUNT(*) AS n FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        "WHERE u.autoscuola_id = ? AND r.codice = 'allievo' AND u.attivo = 1 "
        "  AND u.classe_id IS NULL", (p.autoscuola_id,))["n"]
    return {"classi": classi, "senza_classe": senza}


@router.post("")
def crea(body: ClasseIn, p: Principal = Depends(require_admin)):
    if db.query_one("SELECT 1 FROM classi WHERE autoscuola_id = ? AND nome = ?",
                    (p.autoscuola_id, body.nome.strip())):
        raise HTTPException(409, "Esiste gia' una classe con questo nome")
    cur = db.execute(
        "INSERT INTO classi(autoscuola_id, nome, descrizione, listato_target, colore, attiva) "
        "VALUES(?,?,?,?,?,?)",
        (p.autoscuola_id, body.nome.strip(), body.descrizione, body.listato_target,
         body.colore, 1 if body.attiva else 0))
    return {"id": cur.lastrowid, "nome": body.nome.strip()}


@router.put("/{classe_id}")
def modifica(classe_id: int, body: ClasseIn, p: Principal = Depends(require_admin)):
    cur = db.execute(
        "UPDATE classi SET nome = ?, descrizione = ?, listato_target = ?, colore = ?, attiva = ? "
        "WHERE id = ? AND autoscuola_id = ?",
        (body.nome.strip(), body.descrizione, body.listato_target, body.colore,
         1 if body.attiva else 0, classe_id, p.autoscuola_id))
    if not cur.rowcount:
        raise HTTPException(404, "Classe non trovata")
    return {"ok": True}


@router.delete("/{classe_id}")
def elimina(classe_id: int, p: Principal = Depends(require_admin)):
    """Elimina la classe. Gli allievi non si toccano: restano senza classe."""
    if not db.query_one("SELECT 1 FROM classi WHERE id = ? AND autoscuola_id = ?",
                        (classe_id, p.autoscuola_id)):
        raise HTTPException(404, "Classe non trovata")
    liberati = db.execute("UPDATE utenti SET classe_id = NULL WHERE classe_id = ?",
                          (classe_id,)).rowcount
    db.execute("DELETE FROM classi WHERE id = ?", (classe_id,))
    return {"ok": True, "allievi_senza_classe": liberati}


@router.get("/{classe_id}/allievi")
def allievi(classe_id: int, p: Principal = Depends(require_staff)):
    if not db.query_one("SELECT 1 FROM classi WHERE id = ? AND autoscuola_id = ?",
                        (classe_id, p.autoscuola_id)):
        raise HTTPException(404, "Classe non trovata")
    return db.rows_to_dicts(db.query(
        "SELECT u.id, u.nome, u.cognome, u.username, u.listato_target, u.data_esame "
        "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        "WHERE u.classe_id = ? AND r.codice = 'allievo' AND u.attivo = 1 "
        "ORDER BY u.cognome, u.nome", (classe_id,)))


class AssegnaIn(BaseModel):
    utenti: list[int] = Field(default_factory=list)


@router.post("/{classe_id}/allievi")
def assegna(classe_id: int, body: AssegnaIn, p: Principal = Depends(require_admin)):
    """Mette in questa classe gli allievi indicati, togliendoli da dov'erano.

    Passare `classe_id = 0` li lascia senza classe: e' la via per svuotare senza
    dover cancellare la classe.
    """
    if classe_id and not db.query_one("SELECT 1 FROM classi WHERE id = ? AND autoscuola_id = ?",
                                      (classe_id, p.autoscuola_id)):
        raise HTTPException(404, "Classe non trovata")
    ids = sorted({int(i) for i in body.utenti})
    if not ids:
        return {"spostati": 0}
    segnaposto = ",".join("?" * len(ids))
    # Il filtro sull'autoscuola non e' ridondante: senza, un id di un'altra
    # scuola finirebbe in questa classe.
    cur = db.execute(
        f"UPDATE utenti SET classe_id = ? WHERE id IN ({segnaposto}) AND autoscuola_id = ? "
        f"  AND ruolo_id = (SELECT id FROM ruoli WHERE codice = 'allievo')",
        (classe_id or None, *ids, p.autoscuola_id))
    return {"spostati": cur.rowcount}
