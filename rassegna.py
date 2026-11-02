#!/usr/bin/env python3
"""Rassegna stampa quotidiana da feed RSS/Atom, in stile magazine. Nessuna dipendenza esterna, nessuna AI.

Uso:
    python3 rassegna.py                      # legge feeds.json, scrive docs/index.html
    python3 rassegna.py -c altro.json -o out.html
"""
import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "Mozilla/5.0 (compatible; RassegnaStampa/1.0)"
TZ = ZoneInfo("Europe/Rome")
MIN = datetime.min.replace(tzinfo=timezone.utc)
IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def testo(el):
    return (el.text or "").strip() if el is not None else ""


def pulisci(s, n=230):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > n:
        s = s[:n].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def parse_data(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def https(url):
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url if url.startswith("https://") else ""


def trova_immagine(item, grezzo):
    for c in item.iter():
        k = local(c.tag)
        u = c.get("url") or ""
        if k in ("thumbnail", "content", "enclosure") and u:
            tipo, medium = c.get("type", ""), c.get("medium", "")
            if k == "thumbnail" or tipo.startswith("image") or medium == "image" or IMG_RE.search(u):
                if https(u):
                    return https(u)
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", html.unescape(grezzo or ""), re.I)
    return https(m.group(1)) if m else ""


def scarica(feed):
    req = urllib.request.Request(feed["url"], headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def leggi_feed(feed):
    """Restituisce (feed, articoli, errore)."""
    try:
        root = ET.fromstring(scarica(feed))
    except Exception as e:  # rete, XML malformato, ecc.
        return feed, [], f"{type(e).__name__}: {e}"
    articoli = []
    for it in root.iter():
        if local(it.tag) not in ("item", "entry"):
            continue
        campi, link = {}, ""
        for c in it:
            k = local(c.tag)
            if k == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                elif not link and testo(c):
                    link = testo(c)
            elif k in ("title", "description", "summary", "pubDate", "published", "updated", "date", "encoded", "content"):
                campi.setdefault(k, testo(c))
        titolo = pulisci(campi.get("title"), 220)
        link = link.strip()
        if not titolo or not link.startswith(("http://", "https://")):
            continue
        data = parse_data(campi.get("pubDate") or campi.get("published") or campi.get("updated") or campi.get("date"))
        grezzo = campi.get("description") or campi.get("summary") or campi.get("encoded") or campi.get("content") or ""
        sommario = pulisci(grezzo)
        if sommario.lower().startswith(titolo.lower()[:40]):
            sommario = ""
        articoli.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "titolo": titolo, "url": link, "sommario": sommario,
            "img": trova_immagine(it, campi.get("encoded", "") + grezzo),
            "data": data, "fonte": feed["nome"], "lingua": feed["lingua"],
        })
    return feed, articoli, None


def chiave(a):
    return re.sub(r"\W+", "", a["titolo"].lower())[:70]


def seleziona(articoli, cfg):
    totale, quota_it, tetto = cfg["notizie_per_argomento"], cfg["quota_italiane"], cfg["max_per_testata"]
    limite = datetime.now(timezone.utc) - timedelta(hours=cfg["ore_massime"])

    visti_url, visti_titoli, unici = set(), set(), []
    for a in sorted(articoli, key=lambda x: x["data"] or MIN, reverse=True):
        if a["url"] in visti_url or chiave(a) in visti_titoli:
            continue
        visti_url.add(a["url"])
        visti_titoli.add(chiave(a))
        unici.append(a)

    recenti = [a for a in unici if a["data"] and a["data"] >= limite]
    altri = [a for a in unici if a not in recenti]
    scelti, per_fonte = [], {}

    def prendi(lingua, quanti, pool, tetto_fonte):
        n = 0
        for a in pool:
            if n >= quanti or len(scelti) >= totale:
                break
            if a in scelti or (lingua and a["lingua"] != lingua):
                continue
            if per_fonte.get(a["fonte"], 0) >= tetto_fonte:
                continue
            scelti.append(a)
            per_fonte[a["fonte"]] = per_fonte.get(a["fonte"], 0) + 1
            n += 1

    prendi("it", quota_it, recenti, tetto)
    prendi("en", totale - quota_it, recenti, tetto)
    for t in (tetto, tetto + 2, 99):
        prendi(None, totale, recenti, t)
    for t in (tetto + 2, 99):
        prendi(None, totale, altri, t)

    scelti.sort(key=lambda x: x["data"] or MIN, reverse=True)
    return scelti


CSS = """
:root{--bg:#f4f1ea;--fg:#17171a;--mut:#6a6a64;--card:#fff;--line:#e2ddd0;--acc:#b8300c;--it:#166534;--en:#1d4ed8;--sh:0 1px 2px rgba(0,0,0,.05),0 6px 18px -8px rgba(0,0,0,.12)}
@media(prefers-color-scheme:dark){:root{--bg:#111112;--fg:#efefec;--mut:#9c9c95;--card:#1b1b1d;--line:#2c2c2e;--acc:#ff8a5c;--it:#4ade80;--en:#8aa9ff;--sh:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 Georgia,'Times New Roman',serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 18px 60px}
.testata{text-align:center;padding:30px 0 16px;border-bottom:3px double var(--fg)}
.testata h1{margin:0;font-size:clamp(2.1rem,6vw,3.6rem);letter-spacing:-.02em;font-weight:700}
.testata p{margin:6px 0 0;color:var(--mut);font:13px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.12em}
.bar{display:flex;flex-wrap:wrap;gap:10px 18px;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 0;border-bottom:1px solid var(--line);font-family:system-ui,sans-serif}
.tabs,.filtri{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.sep{width:1px;height:22px;background:var(--line);margin:0 4px}
button{font:600 14px system-ui,sans-serif;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:7px 14px;border-radius:999px;cursor:pointer}
button:hover{border-color:var(--fg)}
button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.griglia{display:grid;gap:20px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));margin-top:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:var(--sh);border-top:4px solid var(--acc)}
.card.tecnologia{border-top-color:#2563eb}.card.turismo{border-top-color:#0d9488}
.thumb{display:block;aspect-ratio:16/10;background:var(--line);overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .4s}
.card:hover .thumb img{transform:scale(1.04)}
.corpo{padding:14px 16px 16px;display:flex;flex-direction:column;gap:6px;flex:1}
.meta{display:flex;gap:8px;align-items:center;font:12px system-ui,sans-serif;color:var(--mut)}
.fonte{font-weight:700;color:var(--acc);text-transform:uppercase;letter-spacing:.04em}
.lang{font-weight:700;font-size:10.5px;padding:0 5px;border-radius:4px;border:1px solid currentColor}
.lang.it{color:var(--it)}.lang.en{color:var(--en)}
.quando{margin-left:auto;white-space:nowrap}
.card h3{font-size:1.2rem;line-height:1.25;margin:2px 0 0}
.card h3 a{color:inherit;text-decoration:none}.card h3 a:hover{text-decoration:underline}
.sum{margin:0;color:var(--mut);font-size:.95rem;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.azioni{margin-top:auto;padding-top:10px;display:flex;justify-content:space-between;align-items:center;font-family:system-ui,sans-serif}
.azioni a{font-size:13px;color:var(--mut)}
.salva{padding:5px 12px;font-size:13px}
.salva[aria-pressed=true]{background:#f5b301;border-color:#f5b301;color:#1b1b1b}
.hero{grid-column:span 2}
.hero h3{font-size:1.7rem}
.hero .thumb{aspect-ratio:16/8}
@media(max-width:620px){.bar{position:static}.hero{grid-column:auto}.hero h3{font-size:1.35rem}.hero .thumb{aspect-ratio:16/10}}
.vuoto{grid-column:1/-1;text-align:center;color:var(--mut);padding:50px 10px;font-family:system-ui,sans-serif}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--mut);font:13px system-ui,sans-serif}
footer button{font-size:13px;padding:5px 12px;margin-right:6px}
"""

JS = r"""
const D=JSON.parse(document.getElementById('dati').textContent);
const KEYS=Object.keys(D.argomenti), CH='rassegna.salvate.v1';
let mem={};
function leggi(){try{return JSON.parse(localStorage.getItem(CH)||'{}')}catch(e){return mem}}
function scrivi(o){mem=o;try{localStorage.setItem(CH,JSON.stringify(o))}catch(e){}}
let salvate=leggi(), vista=KEYS[0], lingua='all';
const sicuro=u=>/^https?:\/\//i.test(u||'');
const giorno=d=>d.toLocaleDateString('sv-SE',{timeZone:'Europe/Rome'});
function quando(iso){
  if(!iso) return '';
  const d=new Date(iso), ora=d.toLocaleTimeString('it-IT',{timeZone:'Europe/Rome',hour:'2-digit',minute:'2-digit'});
  if(giorno(d)===giorno(new Date())) return 'oggi, '+ora;
  if(giorno(d)===giorno(new Date(Date.now()-864e5))) return 'ieri, '+ora;
  return d.toLocaleDateString('it-IT',{timeZone:'Europe/Rome',day:'numeric',month:'short'})+', '+ora;
}
function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!==undefined)e.textContent=txt;return e}
function scheda(a,arg,hero){
  const c=el('article','card '+arg+(hero?' hero':''));
  if(a.img&&sicuro(a.img)){
    const t=el('a','thumb');t.href=a.url;t.target='_blank';t.rel='noopener noreferrer';t.tabIndex=-1;
    const i=document.createElement('img');i.src=a.img;i.alt='';i.loading='lazy';i.referrerPolicy='no-referrer';
    i.onerror=()=>t.remove();t.appendChild(i);c.appendChild(t);
  }
  const b=el('div','corpo');
  const m=el('div','meta');m.append(el('span','fonte',a.fonte),el('span','lang '+a.lingua,(a.lingua||'').toUpperCase()),el('span','quando',quando(a.data)));
  const h=el('h3'),l=el('a',null,a.titolo);l.href=sicuro(a.url)?a.url:'#';l.target='_blank';l.rel='noopener noreferrer';h.appendChild(l);
  b.append(m,h);
  if(a.sommario) b.appendChild(el('p','sum',a.sommario));
  const az=el('div','azioni'), s=el('button','salva');
  const on=!!salvate[a.id];
  s.setAttribute('aria-pressed',on);s.textContent=on?'★ Salvata':'☆ Salva';
  s.onclick=()=>{
    if(salvate[a.id]) delete salvate[a.id]; else salvate[a.id]=Object.assign({},a,{argomento:arg,salvata:new Date().toISOString()});
    scrivi(salvate);disegna();
  };
  const leg=el('a',null,'Leggi →');leg.href=l.href;leg.target='_blank';leg.rel='noopener noreferrer';
  az.append(s,leg);b.appendChild(az);c.appendChild(b);return c;
}
function elenco(){
  if(vista.startsWith('salvate-')){
    const k=vista.slice(8);
    return Object.values(salvate).filter(a=>a.argomento===k).sort((x,y)=>(y.salvata||'').localeCompare(x.salvata||'')).map(a=>[a,k]);
  }
  return D.argomenti[vista].articoli.map(a=>[a,vista]);
}
function disegna(){
  const g=document.getElementById('griglia');g.replaceChildren();
  const lista=elenco().filter(([a])=>lingua==='all'||a.lingua===lingua);
  if(!lista.length){
    g.appendChild(el('div','vuoto',vista.startsWith('salvate-')
      ?'Ancora nessuna notizia salvata qui. Premi "☆ Salva" su una notizia per ritrovarla in questo elenco.'
      :'Nessuna notizia disponibile con questo filtro.'));
  }
  const prima=!vista.startsWith('salvate-')&&lingua==='all'?lista.findIndex(([a])=>a.img):-1;
  if(prima>0) lista.unshift(lista.splice(prima,1)[0]);
  lista.forEach(([a,k],i)=>g.appendChild(scheda(a,k,i===0&&prima>=0)));
  document.querySelectorAll('[data-vista]').forEach(b=>{
    b.setAttribute('aria-pressed',b.dataset.vista===vista);
    const v=b.dataset.vista;
    if(v.startsWith('salvate-')){const k=v.slice(8);b.textContent='★ '+D.argomenti[k].titolo+' ('+Object.values(salvate).filter(a=>a.argomento===k).length+')'}
  });
  document.querySelectorAll('[data-lingua]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.lingua===lingua));
}
document.querySelectorAll('[data-vista]').forEach(b=>b.onclick=()=>{vista=b.dataset.vista;try{localStorage.setItem('rassegna.vista',vista)}catch(e){}disegna();window.scrollTo({top:0})});
document.querySelectorAll('[data-lingua]').forEach(b=>b.onclick=()=>{lingua=b.dataset.lingua;disegna()});
try{const v=localStorage.getItem('rassegna.vista');if(v&&document.querySelector('[data-vista="'+v+'"]'))vista=v}catch(e){}
document.getElementById('esporta').onclick=()=>{
  const blob=new Blob([JSON.stringify(salvate,null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='notizie-salvate.json';a.click();
};
document.getElementById('importa').onchange=ev=>{
  const f=ev.target.files[0];if(!f)return;
  const r=new FileReader();
  r.onload=()=>{try{
    const o=JSON.parse(r.result);let n=0;
    for(const [id,a] of Object.entries(o)){
      if(a&&typeof a.titolo==='string'&&sicuro(a.url)&&KEYS.includes(a.argomento)){
        salvate[id]={id:String(id),titolo:a.titolo,url:a.url,sommario:a.sommario||'',img:sicuro(a.img)?a.img:'',data:a.data||null,fonte:String(a.fonte||''),lingua:a.lingua==='en'?'en':'it',argomento:a.argomento,salvata:a.salvata||new Date().toISOString()};n++;
      }
    }
    scrivi(salvate);disegna();alert('Importate '+n+' notizie.');
  }catch(e){alert('File non valido.')}};
  r.readAsText(f);ev.target.value='';
};
disegna();
"""


def costruisci_html(sezioni, errori):
    adesso = datetime.now(TZ)
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
            "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    data_lunga = f"{giorni[adesso.weekday()]} {adesso.day} {mesi[adesso.month - 1]} {adesso.year}"

    dati = {"generata": adesso.isoformat(), "argomenti": {
        k: {"titolo": v["titolo"], "articoli": [
            {**a, "data": a["data"].isoformat() if a["data"] else None} for a in v["articoli"]]}
        for k, v in sezioni.items()}}
    dati_json = json.dumps(dati, ensure_ascii=False).replace("<", "\\u003c")

    tab_oggi = "".join(f'<button data-vista="{k}" aria-pressed="false">{html.escape(v["titolo"])}</button>' for k, v in sezioni.items())
    tab_salv = "".join(f'<button data-vista="salvate-{k}" aria-pressed="false"></button>' for k in sezioni)
    note = ""
    if errori:
        note = "<br>Feed non raggiunti oggi: " + html.escape(", ".join(errori))

    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rassegna stampa, {data_lunga}</title>
<style>{CSS}</style></head><body><div class="wrap">
<div class="testata"><h1>Rassegna stampa</h1><p>{data_lunga} · aggiornata alle {adesso:%H:%M}</p></div>
<div class="bar"><div class="tabs">{tab_oggi}<span class="sep"></span>{tab_salv}</div>
<div class="filtri"><button data-lingua="all" aria-pressed="true">Tutte</button><button data-lingua="it" aria-pressed="false">Italiane</button><button data-lingua="en" aria-pressed="false">Estere</button></div></div>
<main class="griglia" id="griglia"></main>
<footer>Notizie da feed RSS pubblici: i titoli rimandano alle testate originali. Le notizie salvate restano in questo browser.<br>
<button id="esporta">Esporta salvate</button><label><button onclick="this.nextElementSibling.click()">Importa salvate</button><input id="importa" type="file" accept=".json" hidden></label>{note}</footer>
</div><script id="dati" type="application/json">{dati_json}</script><script>{JS}</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(Path(__file__).with_name("feeds.json")))
    ap.add_argument("-o", "--output", default="docs/index.html")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    imp = cfg["impostazioni"]
    tutti = [(k, f) for k, v in cfg["argomenti"].items() for f in v["feed"]]

    with ThreadPoolExecutor(max_workers=12) as ex:
        risultati = list(ex.map(lambda kf: (kf[0], *leggi_feed(kf[1])), tutti))

    per_argomento, errori = {k: [] for k in cfg["argomenti"]}, []
    for k, feed, arts, err in risultati:
        if err:
            errori.append(feed["nome"])
            print(f"[KO] {feed['nome']}: {err}", file=sys.stderr)
        else:
            print(f"[ok] {feed['nome']}: {len(arts)} articoli")
            per_argomento[k].extend(arts)

    sezioni = {k: {"titolo": cfg["argomenti"][k]["titolo"], "articoli": seleziona(per_argomento[k], imp)}
               for k in cfg["argomenti"]}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(costruisci_html(sezioni, errori), encoding="utf-8")
    for k, v in sezioni.items():
        it = sum(a["lingua"] == "it" for a in v["articoli"])
        print(f"{v['titolo']}: {len(v['articoli'])} notizie ({it} italiane, {len(v['articoli']) - it} estere)")
    print(f"Scritto {out}")


if __name__ == "__main__":
    main()
