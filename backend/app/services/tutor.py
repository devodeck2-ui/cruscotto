"""AI Tutor - spiegazione degli errori, con due fornitori intercambiabili.

FORNITORI
    anthropic : API Messages di Claude. Qualita' di riferimento, a consumo.
    gemini    : API generateContent di Google AI Studio. Ha un piano gratuito
                senza scadenza e legge le immagini, che qui e' indispensabile:
                oltre la meta' delle domande del listato mostra un segnale o
                un incrocio, e senza vision il tutor spiegherebbe alla cieca.

    La scelta e' automatica in base alla chiave configurata (vedi config.py).
    Il resto dell'applicazione non sa quale sia attivo: parla solo con
    spiega_errore() e follow_up().

CACHE
    Una domanda ministeriale ha risposta binaria, quindi esistono solo DUE
    spiegazioni possibili per domanda. Dopo poche settimane di esercizio la
    cache satura e il costo marginale tende a zero.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .. import db
from ..config import settings

API_ANTHROPIC = "https://api.anthropic.com/v1/messages"
API_GEMINI = "https://generativelanguage.googleapis.com/v1beta"

MIME_AMMESSI = ("image/png", "image/jpeg", "image/gif", "image/webp")


class TutorError(RuntimeError):
    pass


SYSTEM_PROMPT = """Sei un istruttore di teoria di autoscuola italiano con vent'anni di esperienza nella preparazione all'esame di teoria per la patente. Parli con un allievo che ha appena sbagliato una domanda ufficiale del listato ministeriale e deve capire il proprio errore.

## Il tuo compito
Spiegare PERCHE' la risposta data dall'allievo e' sbagliata e perche' quella corretta e' corretta, fondando la spiegazione sul Codice della Strada italiano (D.Lgs. 285/1992) e sul relativo Regolamento di esecuzione (D.P.R. 495/1992).

## Come devi rispondere
1. **Verdetto in una riga.** Apri dichiarando qual e' la risposta esatta e sintetizza il concetto in una frase.
2. **La regola.** Enuncia la norma o il principio tecnico applicabile in italiano semplice. Cita l'articolo del Codice della Strada solo se sei certo del numero; se non lo sei, descrivi la regola senza citare.
3. **Applicazione al caso.** Collega la regola al testo esatto della domanda. Se e' presente un'immagine, DESCRIVI cio' che vedi (forma, colore, simboli, disposizione dell'incrocio, presenza di segnali o linee) e spiega quali elementi visivi determinano la risposta.
4. **L'inganno.** Le domande ministeriali sono costruite su trabocchetti ricorrenti: parole assolute ("sempre", "mai", "tutti"), confusione fra obbligo e facolta', fra preavviso e prescrizione, fra divieto di sosta e divieto di fermata, fra massa e portata. Indica quale trappola ha fatto cadere l'allievo in questo caso specifico.
5. **Regola pratica.** Chiudi con una frase-memoria breve che l'allievo possa richiamare all'esame.

## Vincoli
- Massimo 200 parole. All'allievo serve chiarezza, non un trattato.
- Tono diretto e incoraggiante: mai umiliante, mai paternalistico.
- Rigore tecnico assoluto: se la normativa distingue casi, dillo. Non semplificare fino a diventare inesatto.
- Usa il "tu".
- Non inventare MAI numeri di articolo, limiti di velocita', misure, distanze o sanzioni di cui non sei certo.
- Se la domanda e' ambigua o la formulazione ministeriale e' datata, dichiaralo apertamente.
- Resta nel dominio: educazione stradale e preparazione all'esame di teoria.
- Rispondi in italiano.
- Non usare emoji.

## Formato
Markdown essenziale: grassetto per i concetti chiave, al massimo un elenco puntato. Nessun titolo di sezione: un testo scorrevole di tre o quattro paragrafi brevi."""


def _template_utente(domanda, risposta_data):
    tronco = "\nTronco del quesito: " + domanda["tronco"] if domanda.get("tronco") else ""
    img = "\nAll'affermazione e' allegata l'immagine che ti invio: analizzala." if domanda.get("immagine") else ""
    corretta = "VERO" if domanda["risposta"] else "FALSO"
    data = "VERO" if risposta_data else "FALSO"
    return (
        "Domanda ufficiale del listato " + str(domanda["listato"])
        + " - capitolo \"" + str(domanda.get("capitolo") or "n/d")
        + "\", argomento \"" + str(domanda.get("argomento") or "n/d") + "\"." + tronco
        + "\n\nAFFERMAZIONE DA VALUTARE:\n\"" + domanda["testo"] + "\"\n\n"
        + "Risposta corretta secondo il Ministero: " + corretta + "\n"
        + "Risposta data dall'allievo: " + data + " - quindi SBAGLIATA." + img
        + "\n\nSpiega all'allievo perche' ha sbagliato.")


def _immagine(percorso):
    """Ritorna (mime, base64) oppure None se assente o di formato non gestito."""
    if not percorso:
        return None
    fpath = settings.media_dir / percorso
    if not fpath.exists():
        return None
    mime = mimetypes.guess_type(fpath.name)[0] or "image/png"
    if mime not in MIME_AMMESSI:
        return None
    return mime, base64.standard_b64encode(fpath.read_bytes()).decode()


def payload_anthropic(domanda, risposta_data, cronologia=None):
    contenuto = []
    img = _immagine(domanda.get("immagine"))
    if img:
        contenuto.append({"type": "image",
                          "source": {"type": "base64", "media_type": img[0], "data": img[1]}})
    contenuto.append({"type": "text", "text": _template_utente(domanda, risposta_data)})
    messaggi = [{"role": "user", "content": contenuto}]
    for m in (cronologia or []):
        messaggi.append({"role": "assistant" if m["ruolo"] == "assistente" else "user",
                         "content": [{"type": "text", "text": m["testo"]}]})
    return {
        "model": settings.ai_model,
        "max_tokens": settings.ai_max_tokens,
        "temperature": 0.2,
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": messaggi,
    }


def payload_gemini(domanda, risposta_data, cronologia=None):
    parti = []
    img = _immagine(domanda.get("immagine"))
    if img:
        parti.append({"inline_data": {"mime_type": img[0], "data": img[1]}})
    parti.append({"text": _template_utente(domanda, risposta_data)})
    contenuti = [{"role": "user", "parts": parti}]
    for m in (cronologia or []):
        contenuti.append({"role": "model" if m["ruolo"] == "assistente" else "user",
                          "parts": [{"text": m["testo"]}]})
    categorie = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                 "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
    return {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contenuti,
        "generationConfig": {"temperature": 0.2,
                             "maxOutputTokens": settings.ai_max_tokens},
        "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in categorie],
    }


def _http(url, payload, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        corpo = e.read()[:400].decode(errors="replace")
        raise TutorError("HTTP " + str(e.code) + ": " + corpo) from e
    except Exception as e:
        raise TutorError("servizio non raggiungibile: " + str(e)) from e


def _chiama_anthropic(payload):
    dati = _http(API_ANTHROPIC, payload,
                 {"x-api-key": settings.anthropic_api_key,
                  "anthropic-version": "2023-06-01"})
    testo = "".join(b.get("text", "") for b in dati.get("content", [])
                    if b.get("type") == "text").strip()
    uso = dati.get("usage", {})
    return testo, {"input": uso.get("input_tokens", 0),
                   "output": uso.get("output_tokens", 0)}, dati.get("model", settings.ai_model)


_modello_gemini_valido = None


def _modelli_gemini_disponibili():
    url = API_GEMINI + "/models?key=" + urllib.parse.quote(settings.gemini_api_key)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            dati = json.loads(resp.read())
    except Exception as e:
        raise TutorError("impossibile elencare i modelli Google: " + str(e)) from e
    return [m["name"].split("/")[-1] for m in dati.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]


def _scegli_modello_gemini():
    """Sceglie un modello valido se quello configurato non esiste piu'.

    I nomi dei modelli Google cambiano spesso e un nome obsoleto restituisce
    404. Invece di lasciare il tutor rotto, si interroga l'elenco e si prende
    un modello della famiglia Flash, l'unica compresa nel piano gratuito.
    """
    disponibili = _modelli_gemini_disponibili()
    if not disponibili:
        raise TutorError("nessun modello Google disponibile per questa chiave")
    if settings.gemini_model in disponibili:
        return settings.gemini_model
    flash = [m for m in disponibili if "flash" in m.lower()
             and "thinking" not in m.lower() and "image" not in m.lower()]
    return (flash or disponibili)[0]


def _chiama_gemini(payload):
    global _modello_gemini_valido
    modello = _modello_gemini_valido or settings.gemini_model
    url = (API_GEMINI + "/models/" + modello + ":generateContent?key="
           + urllib.parse.quote(settings.gemini_api_key))
    try:
        dati = _http(url, payload, {})
    except TutorError as e:
        if "HTTP 404" not in str(e) or _modello_gemini_valido:
            raise
        modello = _scegli_modello_gemini()
        _modello_gemini_valido = modello
        url = (API_GEMINI + "/models/" + modello + ":generateContent?key="
               + urllib.parse.quote(settings.gemini_api_key))
        dati = _http(url, payload, {})
    _modello_gemini_valido = modello
    candidati = dati.get("candidates") or []
    if not candidati:
        blocco = (dati.get("promptFeedback") or {}).get("blockReason")
        raise TutorError("risposta vuota" + (" (bloccata: " + str(blocco) + ")" if blocco else ""))
    parti = (candidati[0].get("content") or {}).get("parts") or []
    testo = "".join(p.get("text", "") for p in parti).strip()
    uso = dati.get("usageMetadata", {})
    return testo, {"input": uso.get("promptTokenCount", 0),
                   "output": uso.get("candidatesTokenCount", 0)}, modello


def _genera(domanda, risposta_data, cronologia=None):
    provider = settings.provider_attivo()
    if provider == "anthropic":
        return _chiama_anthropic(payload_anthropic(domanda, risposta_data, cronologia))
    if provider == "gemini":
        return _chiama_gemini(payload_gemini(domanda, risposta_data, cronologia))
    raise TutorError("nessuna chiave configurata: imposta ANTHROPIC_API_KEY "
                     "oppure GEMINI_API_KEY nel file .env")


def _finestra():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _rate_limit(autoscuola_id):
    r = db.query_one("SELECT n_chiamate FROM ai_consumo WHERE autoscuola_id=? AND finestra=?",
                     (autoscuola_id, _finestra()))
    if r and r["n_chiamate"] >= settings.ai_rate_limit_ora:
        raise TutorError("limite orario di richieste AI raggiunto per questa autoscuola")


def _contabilizza(autoscuola_id, uso):
    db.execute(
        "INSERT INTO ai_consumo(autoscuola_id, finestra, n_chiamate, token_in, token_out) "
        "VALUES(?,?,1,?,?) ON CONFLICT(autoscuola_id, finestra) DO UPDATE SET "
        " n_chiamate = n_chiamate + 1, token_in = token_in + ?, token_out = token_out + ?",
        (autoscuola_id, _finestra(), uso.get("input", 0), uso.get("output", 0),
         uso.get("input", 0), uso.get("output", 0)))


def spiega_errore(domanda, risposta_data, autoscuola_id):
    """Ritorna {'testo', 'origine': 'cache'|'llm', 'modello'}."""
    cached = db.query_one(
        "SELECT id, testo, modello FROM ai_spiegazioni "
        "WHERE domanda_id = ? AND risposta_data = ? AND prompt_ver = ?",
        (domanda["id"], 1 if risposta_data else 0, settings.prompt_version))
    if cached:
        db.execute("UPDATE ai_spiegazioni SET n_hit = n_hit + 1 WHERE id = ?", (cached["id"],))
        return {"testo": cached["testo"], "origine": "cache", "modello": cached["modello"]}

    _rate_limit(autoscuola_id)
    testo, uso, modello = _genera(domanda, risposta_data)
    if not testo:
        raise TutorError("risposta del modello vuota")

    db.execute(
        "INSERT INTO ai_spiegazioni(domanda_id, risposta_data, testo, modello, prompt_ver,"
        " token_in, token_out) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(domanda_id, risposta_data, prompt_ver) DO UPDATE SET testo = excluded.testo",
        (domanda["id"], 1 if risposta_data else 0, testo, modello,
         settings.prompt_version, uso.get("input", 0), uso.get("output", 0)))
    _contabilizza(autoscuola_id, uso)
    return {"testo": testo, "origine": "llm", "modello": modello}


def follow_up(domanda, risposta_data, cronologia, messaggio, autoscuola_id):
    """Chat di approfondimento.

    La cronologia viene ricostruita lato server dalla tabella ai_conversazioni:
    il client non puo' iniettare messaggi arbitrari nel contesto del modello.
    """
    _rate_limit(autoscuola_id)
    storia = list(cronologia or [])[-6:]
    storia.append({"ruolo": "utente", "testo": messaggio[:1000]})
    testo, uso, _ = _genera(domanda, risposta_data, cronologia=storia)
    if not testo:
        raise TutorError("risposta del modello vuota")
    _contabilizza(autoscuola_id, uso)
    return testo


def stato():
    """Diagnostica esposta da /api/salute e dallo script di prova."""
    return {"provider": settings.provider_attivo(),
            "modello": _modello_gemini_valido or settings.modello_attivo(),
            "descrizione": settings.descrizione_ai(),
            "prompt_version": settings.prompt_version}
