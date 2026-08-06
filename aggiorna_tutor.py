"""Presenze: rendere evidente dove si spunta.

IL PROBLEMA
    La funzione c'era: si clicca una lezione a sinistra e a destra compare
    l'elenco degli allievi con le caselle da spuntare. Ma nel tema scuro le
    righe delle lezioni, disegnate con un bordo tenue, sembravano testo e non
    pulsanti. Chi apriva la sezione vedeva due righe e la scritta "Scegli una
    lezione", senza capire che le righe erano la cosa da cliccare.

LA CURA
    1. Le lezioni diventano righe chiaramente cliccabili, con la freccia e la
       scritta "Registra presenze", e si illuminano al passaggio.
    2. La lezione piu' vicina a oggi si apre DA SOLA: l'elenco degli allievi
       e' quindi la prima cosa che si vede entrando nella sezione.
    3. Due pulsanti pratici: "Tutti presenti" e "Azzera". In aula si spunta
       l'intera classe e si togliono i due assenti, non il contrario.
"""
import re
import shutil
from pathlib import Path

D = Path("C:/cruscotto")
GES = D / "frontend" / "gestione.js"
SW = D / "frontend" / "sw.js"

RIGA_VECCHIA = """      ${perGiorno[data].map(l => `
        <button class="btn btn-sm w-100 text-start mb-1 ${l.stato === 'svolta' ? 'btn-light border' : 'btn-outline-secondary'}"
                data-lez="${l.id}">
          <span class="fw-semibold">${l.ora_inizio}-${l.ora_fine}</span>
          <span class="pill ms-1">${esc(l.listato)}</span>
          ${l.presenti ? `<span class="badge text-bg-success float-end">${l.presenti} presenti</span>`
                       : '<span class="text-muted float-end small">da registrare</span>'}
        </button>`).join('')}"""

RIGA_NUOVA = """      ${perGiorno[data].map(l => `
        <button class="btn btn-sm w-100 text-start mb-1 riga-lezione ${l.presenti ? 'fatta' : ''}"
                data-lez="${l.id}">
          <span class="d-flex justify-content-between align-items-center gap-2">
            <span>
              <span class="fw-semibold">${l.ora_inizio}-${l.ora_fine}</span>
              <span class="pill ms-1">${esc(l.listato)}</span>
              ${l.aula ? `<span class="text-muted" style="font-size:.72rem"> ${esc(l.aula)}</span>` : ''}
            </span>
            <span class="text-nowrap">
              ${l.presenti
                ? `<span class="badge text-bg-success">${l.presenti} presenti</span>`
                : '<span class="apri-presenze">Registra presenze</span>'}
              <span class="freccia">&rsaquo;</span>
            </span>
          </span>
        </button>`).join('')}"""

ASCOLTO_VECCHIO = """  $$('#elenco-lezioni button[data-lez]').forEach(b =>
    b.addEventListener('click', () => apriLezione(+b.dataset.lez)));"""

ASCOLTO_NUOVO = """  $$('#elenco-lezioni button[data-lez]').forEach(b =>
    b.addEventListener('click', () => apriLezione(+b.dataset.lez)));

  // Si apre da sola la lezione piu' vicina a oggi: entrando nella sezione si
  // trova subito l'elenco da spuntare, invece di un riquadro che dice
  // "scegli una lezione" e lascia l'impressione che manchi qualcosa.
  if (d.lezioni.length && !S.lezione) {
    const oggi = new Date().toISOString().slice(0, 10);
    const vicina = d.lezioni.reduce((a, b) =>
      Math.abs(new Date(b.data) - new Date(oggi)) < Math.abs(new Date(a.data) - new Date(oggi)) ? b : a);
    apriLezione(vicina.id);
  }
  $$('#elenco-lezioni button[data-lez]').forEach(b =>
    b.classList.toggle('scelta', S.lezione && +b.dataset.lez === S.lezione.id));"""

BARRA_VECCHIA = """    <div class="d-flex justify-content-between align-items-center border-top pt-2">
      <span class="small text-muted">Spunta chi e' presente</span>
      <span class="small"><strong id="conta-presenti">0</strong> presenti</span>
    </div>"""

BARRA_NUOVA = """    <div class="d-flex justify-content-between align-items-center border-top pt-2 flex-wrap gap-2">
      <span class="small text-muted">Spunta chi e' presente</span>
      <span class="d-flex align-items-center gap-2">
        <button class="btn btn-sm btn-outline-success" id="btn-tutti-presenti">Tutti presenti</button>
        <button class="btn btn-sm btn-outline-secondary" id="btn-azzera-presenze">Azzera</button>
        <span class="small"><strong id="conta-presenti">0</strong> presenti</span>
      </span>
    </div>"""

MASSA_ANCORA = """  $('#btn-salva-arg').addEventListener('click', async () => {"""

MASSA_NUOVA = """  // Spunta o togli tutti in un colpo. In aula si segna la classe intera e si
  // levano i due assenti: e' l'ordine naturale delle cose, non il contrario.
  async function tutti(stato) {
    const caselle = $$('#pannello-presenze .chk');
    for (const ch of caselle) {
      if ((stato === 'presente') === ch.checked) continue;
      ch.checked = (stato === 'presente');
      try {
        await post(`/api/gestione/lezioni/${id}/presenza`,
          { utente_id: +ch.dataset.u, stato });
      } catch (e) { avviso(e.message); ch.checked = !ch.checked; }
    }
    conta();
    caricaLezioni();
  }
  $('#btn-tutti-presenti').addEventListener('click', () => tutti('presente'));
  $('#btn-azzera-presenze').addEventListener('click', () => {
    if (confirm('Togliere tutte le presenze di questa lezione?')) tutti('assente');
  });

  $('#btn-salva-arg').addEventListener('click', async () => {"""

CSS_EXTRA = """
  /* Righe delle lezioni: devono sembrare cliccabili, perche' lo sono. */
  .riga-lezione {
    background: var(--ac-superficie, #fff);
    border: 1px solid var(--ac-bordo, #e6ebf3);
    color: var(--ac-testo, #0f172a);
    border-left: 4px solid var(--ac-blu, #1d4ed8);
    transition: background .16s ease, transform .16s ease, border-color .16s ease;
  }
  .riga-lezione:hover {
    background: var(--ac-blu-chiaro, #eff4ff);
    transform: translateX(3px);
  }
  .riga-lezione.fatta { border-left-color: #059669; }
  .riga-lezione.scelta {
    background: var(--ac-blu-chiaro, #eff4ff);
    border-color: var(--ac-blu, #1d4ed8);
  }
  .riga-lezione .apri-presenze {
    font-size: .72rem; font-weight: 600; color: var(--ac-blu, #1d4ed8);
  }
  .riga-lezione .freccia {
    font-size: 1.15rem; line-height: 1; color: var(--ac-tenue, #64748b);
    margin-left: .35rem;
  }
"""


def main():
    print()
    print("  PRESENZE: DOVE SI SPUNTA")
    print("  " + "-" * 58)

    if not GES.exists():
        print("  ! non trovo frontend/gestione.js")
        return 1

    testo = GES.read_text(encoding="utf-8")
    originale = testo
    for cerca, nuovo, desc in [
        (RIGA_VECCHIA, RIGA_NUOVA, "lezioni con freccia e 'Registra presenze'"),
        (ASCOLTO_VECCHIO, ASCOLTO_NUOVO, "apertura automatica della lezione piu' vicina"),
        (BARRA_VECCHIA, BARRA_NUOVA, "pulsanti 'Tutti presenti' e 'Azzera'"),
        (MASSA_ANCORA, MASSA_NUOVA, "spunta e azzeramento in blocco"),
    ]:
        if nuovo in testo:
            print(f"  = gia' a posto  {desc}")
            continue
        if cerca not in testo:
            print(f"  ! punto non trovato: {desc}")
            continue
        testo = testo.replace(cerca, nuovo, 1)
        print(f"  v applicato     {desc}")
    if testo != originale:
        shutil.copy2(GES, GES.with_suffix(".js.bak13"))
        GES.write_text(testo, encoding="utf-8")
        print("  v gestione.js salvato")

    html = D / "frontend" / "gestione.html"
    if html.exists():
        h = html.read_text(encoding="utf-8")
        if ".riga-lezione" in h:
            print("  = stile delle righe gia' presente")
        else:
            ancora = "  .barra > span { display:block; height:100%; border-radius:999px; }"
            if ancora in h:
                shutil.copy2(html, html.with_suffix(".html.bak13"))
                html.write_text(h.replace(ancora, ancora + CSS_EXTRA, 1), encoding="utf-8")
                print("  v stile delle righe cliccabili aggiunto")
            else:
                print("  ! non trovo dove aggiungere lo stile")

    if SW.exists():
        s = SW.read_text(encoding="utf-8")
        m = re.search(r"const VERSIONE = 'cruscotto-v(\d+)'", s)
        if m:
            nuova = int(m.group(1)) + 1
            SW.write_text(s.replace(m.group(0),
                          f"const VERSIONE = 'cruscotto-v{nuova}'"), encoding="utf-8")
            print(f"  v cache rinnovata (v{nuova})")

    print()
    print("  " + "-" * 58)
    print("  Come si registra una presenza, da adesso:")
    print()
    print("   Gestione -> Presenze")
    print("   La lezione piu' vicina a oggi si apre da sola: a destra compare")
    print("   l'elenco degli allievi, ognuno con la sua casella. Spunti chi")
    print("   c'e' ed e' salvato subito, senza premere Salva.")
    print()
    print("   'Tutti presenti' spunta la classe intera, poi togli i due")
    print("   assenti. 'Azzera' pulisce tutto se hai sbagliato lezione.")
    print()
    print("   Le altre lezioni restano nell'elenco a sinistra: ora hanno la")
    print("   freccia e la scritta 'Registra presenze', si illuminano al")
    print("   passaggio e diventano verdi quando le hai compilate.")
    print()
    print("   Se non vedi lezioni: prima crea le fasce in 'Orario lezioni',")
    print("   poi premi 'Genera dall'orario'.")
    print()
    print("  Chiudi la finestra nera, riavvia Avvia.bat, CTRL+MAIUSC+R.")
    print()
    return 0


if __name__ == "__main__":
    codice = main()
    try:
        input("  Premi INVIO per chiudere...")
    except EOFError:
        pass
    raise SystemExit(codice)
