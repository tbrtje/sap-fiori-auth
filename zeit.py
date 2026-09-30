"""zeit – CLI für die SAP-Zeiterfassung (CATS / HCM_TIMESHEET_MAN_SRV) mit Kerberos-SSO.

Anmeldung: SPNEGO am NetWeaver-Portal liefert das SSO-Cookie MYSAPSSO2, das auch
vom Fiori-Gateway akzeptiert wird. Es werden keine Passwörter benötigt oder gespeichert.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import uuid
from collections import OrderedDict, defaultdict
from pathlib import Path

import requests
import truststore
from requests.adapters import HTTPAdapter
from requests_gssapi import OPTIONAL, HTTPSPNEGOAuth

DEFAULTS = {
    "portal_url": "https://portal.btc-ag.com/irj/portal",
    "service_url": "https://bgp.btcsap.btc-ag.com:44300/sap/opu/odata/sap/HCM_TIMESHEET_MAN_SRV",
    "sap_client": "300",
    "pernr": None,
    "default_awart": "0800",
    "default_bemot": "01",
    "aliases": {},
}
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sap-zeit" / "config.json"

# Das Portal akzeptiert nur ein Browser-ähnliches User-Agent.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

BEMOT = {"01": "abr.", "02": "n.abr.", "05": "Reise"}
STATUS = {"MSAVE": "gespeichert", "MACTION": "freigegeben", "MAPPROVED": "genehmigt", "MREJECTED": "abgelehnt"}
WDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
LTXA1_MAX = 40


class ZeitError(Exception):
    pass


# ---------------------------------------------------------------- Konfiguration

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stored = {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v or k == "aliases"}
    CONFIG_PATH.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- HTTP / SAP

class _TLSAdapter(HTTPAdapter):
    """System-Trust-Store (interne BTC-CA) und der vom Portal benötigte RSA-Cipher."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.set_ciphers("DEFAULT:AES128-GCM-SHA256")
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


class Timesheet:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.svc = cfg["service_url"].rstrip("/")
        self.s = requests.Session()
        self.s.mount("https://", _TLSAdapter())
        self.s.headers["User-Agent"] = USER_AGENT
        self._csrf = None
        self._pernr = cfg.get("pernr")
        self._login()

    def _login(self) -> None:
        try:
            r = self.s.get(self.cfg["portal_url"], auth=HTTPSPNEGOAuth(mutual_authentication=OPTIONAL), timeout=30)
        except requests.RequestException as e:
            raise ZeitError(f"Portal nicht erreichbar: {e}") from e
        if r.status_code == 401 or "MYSAPSSO2" not in self.s.cookies:
            raise ZeitError("Kerberos-Anmeldung fehlgeschlagen. Gültiges Ticket vorhanden? (klist / kinit)")

    def _params(self, extra: dict | None = None) -> dict:
        p = {"sap-client": self.cfg["sap_client"], "$format": "json"}
        p.update(extra or {})
        return p

    def get(self, entity_set: str, flt: str | None = None) -> list[dict]:
        r = self.s.get(f"{self.svc}/{entity_set}", params=self._params({"$filter": flt} if flt else None), timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if r.status_code != 200 or data is None:
            raise ZeitError(f"{entity_set}: HTTP {r.status_code} {_sap_message(r.text)}")
        return data["d"]["results"]

    @property
    def pernr(self) -> str:
        if not self._pernr:
            rows = self.get("ConcurrentEmploymentSet")
            if not rows:
                raise ZeitError("Personalnummer nicht ermittelbar – bitte 'pernr' in der Config setzen.")
            self._pernr = rows[0]["Pernr"]
        return self._pernr

    def _flt(self, start: dt.date, end: dt.date) -> str:
        return f"Pernr eq '{self.pernr}' and StartDate eq '{start:%Y%m%d}' and EndDate eq '{end:%Y%m%d}'"

    # -- Lesen

    def entries(self, start: dt.date, end: dt.date) -> list[dict]:
        records: OrderedDict[str, dict] = OrderedDict()
        for row in self.get("TimeDataList", self._flt(start, end)):
            records.setdefault(row["RecordNumber"], {})[row["FieldName"]] = row["FieldValue"]
        out = [r for r in records.values() if r.get("COUNTER")]
        out.sort(key=lambda r: (r["WORKDATE"], r.get("STARTTIME", "")))
        return out

    def calendar(self, start: dt.date, end: dt.date) -> dict[str, dict]:
        return {r["Date"]: r for r in self.get("WorkCalendars", self._flt(start, end))}

    def worklist(self, day: dt.date) -> list[dict]:
        start, end = _week(day)
        grouped: dict[int, dict] = defaultdict(dict)
        for row in self.get("WorkListCollection", self._flt(start, end)):
            grouped[row["RecordNumber"]][row["FieldName"]] = row["FieldValue"]
        items = []
        for rec in grouped.values():
            if rec.get("POSID"):
                items.append({"posid": rec["POSID"], "text": rec.get("CPR_OBJTEXT") or rec.get("DISPTEXTW1") or rec["POSID"]})
        return items

    def find_entry(self, counter: str, around: dt.date, weeks: int = 8) -> dict:
        counter = counter.zfill(12)
        start = around - dt.timedelta(weeks=weeks)
        for e in self.entries(start, around + dt.timedelta(weeks=2)):
            if e["COUNTER"] == counter:
                return e
        raise ZeitError(f"Buchung {counter} nicht gefunden (gesucht: {start:%d.%m.%Y} bis +2 Wochen, ggf. --date angeben).")

    # -- Schreiben

    def _csrf_token(self) -> str:
        if not self._csrf:
            r = self.s.get(self.svc + "/", params={"sap-client": self.cfg["sap_client"]},
                           headers={"X-CSRF-Token": "Fetch"}, timeout=30)
            self._csrf = r.headers.get("x-csrf-token")
            if not self._csrf:
                raise ZeitError(f"Kein CSRF-Token erhalten (HTTP {r.status_code}).")
        return self._csrf

    def submit(self, entries: list[dict]) -> list[dict]:
        """Sendet TimeEntries in einem $batch, je Buchung ein Changeset (Teilerfolg möglich)."""
        batch, changeset = f"batch_{uuid.uuid4().hex}", f"changeset_{uuid.uuid4().hex}"
        parts = []
        for e in entries:
            body = json.dumps(e, ensure_ascii=False).encode()
            parts.append(
                f"--{changeset}\r\nContent-Type: application/http\r\nContent-Transfer-Encoding: binary\r\n\r\n"
                f"POST TimeEntries?sap-client={self.cfg['sap_client']} HTTP/1.1\r\n"
                f"Content-Type: application/json\r\nAccept: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                + body + b"\r\n"
            )
        # Das Gateway erlaubt nur eine Operation pro Changeset – daher ein Changeset je Buchung.
        payload = b"".join(
            f"--{batch}\r\nContent-Type: multipart/mixed; boundary={changeset}{i}\r\n\r\n".encode()
            + part.replace(f"--{changeset}".encode(), f"--{changeset}{i}".encode(), 1)
            + f"--{changeset}{i}--\r\n".encode()
            for i, part in enumerate(parts)
        ) + f"--{batch}--\r\n".encode()
        r = self.s.post(f"{self.svc}/$batch", params={"sap-client": self.cfg["sap_client"]}, data=payload, timeout=120,
                        headers={"Content-Type": f"multipart/mixed; boundary={batch}",
                                 "X-CSRF-Token": self._csrf_token(), "Accept": "application/json"})
        if r.status_code != 202:
            raise ZeitError(f"$batch: HTTP {r.status_code} {_sap_message(r.text)}")
        results, errors = [], []
        for i, resp in enumerate(_batch_parts(r)):
            status = re.search(r"^HTTP/1\.1 (\d{3})", resp, re.M)
            if status and int(status.group(1)) < 400:
                body = re.search(r'^(\{"d":.*\})\s*$', resp, re.M)
                results.append(json.loads(body.group(1))["d"] if body else {})
            else:
                errors.append(f"#{i + 1}: {_sap_message(resp)}")
        if errors or len(results) != len(entries):
            raise ZeitError(f"{len(entries) - len(results)} von {len(entries)} Operation(en) von SAP abgelehnt"
                            f" ({len(results)} erfolgreich):\n  " + "\n  ".join(errors or ["unerwartete Antwort"]))
        return results


def _batch_parts(r: requests.Response) -> list[str]:
    """Teilt die $batch-Antwort in die Antworten der einzelnen Changesets auf."""
    boundary = re.search(r"boundary=([^;\s]+)", r.headers.get("content-type", "")).group(1)
    return [p for p in r.text.split(f"--{boundary}")[1:] if p.strip() and not p.startswith("--")]


def _sap_message(text: str) -> str:
    msgs = re.findall(r'"message"\s*:\s*\{[^{}]*?"value"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    details = re.findall(r'"errordetails"\s*:\s*\[(.*?)\]', text, re.S)
    for d in details:
        msgs += re.findall(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', d)
    uniq = list(dict.fromkeys(json.loads(f'"{m}"') for m in msgs))
    return " | ".join(uniq) if uniq else text[:300]


def time_entry(pernr: str, op: str, *, counter: str = "", release: bool = False, day: dt.date | None = None,
               start: str = "", end: str = "", posid: str = "", awart: str = "",
               bemot: str = "", text: str = "") -> dict:
    fields = {"WORKDATE": f"{day:%Y-%m-%d}T00:00:00", "CATSHOURS": "0.00"}
    if op != "D":
        fields.update({"CATSHOURS": f"{_hours(start, end):.2f}", "BEGUZ": start, "ENDUZ": end,
                       "POSID": posid, "AWART": awart, "BEMOT": bemot, "LTXA1": text})
    return {"Pernr": pernr, "Counter": counter, "TimeEntryOperation": op,
            "TimeEntryRelease": "X" if release else " ", "TimeEntryDataFields": fields}


# ---------------------------------------------------------------- Parsing

def parse_date(s: str | None, today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    if not s:
        return today
    t = s.strip().lower()
    rel = {"heute": 0, "h": 0, "gestern": -1, "g": -1, "vorgestern": -2, "morgen": 1}
    if t in rel:
        return today + dt.timedelta(days=rel[t])
    if t[:2] in WEEKDAYS and (len(t) == 2 or t.isalpha()):
        return _week(today)[0] + dt.timedelta(days=WEEKDAYS[t[:2]])
    if re.fullmatch(r"[+-]\d+", t):
        return today + dt.timedelta(days=int(t))
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t):
        return dt.date(*map(int, m.groups()))
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", t):
        d, mo, y = m.groups()
        year = today.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        return dt.date(year, int(mo), int(d))
    raise ZeitError(f"Unbekanntes Datum: {s!r} (z. B. heute, gestern, mo, 30.09., 2026-09-30)")


def parse_time(s: str) -> str:
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?", s.strip())
    if not m:
        raise ZeitError(f"Unbekannte Uhrzeit: {s!r} (z. B. 8, 8:30, 0830)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 24 and 0 <= mi < 60) or (h == 24 and mi):
        raise ZeitError(f"Ungültige Uhrzeit: {s!r}")
    return f"{h:02d}{mi:02d}00"


def parse_range(s: str) -> tuple[str, str]:
    if "-" not in s:
        raise ZeitError(f"Zeitraum als VON-BIS angeben, z. B. 8:30-10:00 (war: {s!r})")
    a, b = s.split("-", 1)
    start, end = parse_time(a), parse_time(b)
    _hours(start, end)
    return start, end


def _hours(start: str, end: str) -> float:
    mins = lambda t: int(t[:2]) * 60 + int(t[2:4])
    diff = mins(end) - mins(start)
    if diff <= 0:
        raise ZeitError("Endzeit muss nach der Startzeit liegen.")
    return diff / 60


def _week(day: dt.date) -> tuple[dt.date, dt.date]:
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def _fmt_time(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}" if t and len(t) >= 4 else "     "


# ---------------------------------------------------------------- Projekte

def resolve_project(ts: Timesheet, cfg: dict, query: str, day: dt.date) -> tuple[dict, dict]:
    """Liefert (Arbeitsvorrat-Eintrag, Alias-Defaults) für Alias, PSP-Element oder Suchbegriff."""
    alias = cfg["aliases"].get(query, {})
    wl = ts.worklist(day)
    needle = alias.get("posid", query)
    exact = [w for w in wl if w["posid"].lower() == needle.lower()]
    if exact:
        return exact[0], alias
    if alias:
        return {"posid": alias["posid"], "text": alias["posid"]}, alias
    words = query.lower().split()
    hits = [w for w in wl if all(x in f"{w['posid']} {w['text']}".lower() for x in words)]
    if len(hits) == 1:
        return hits[0], {}
    if not hits:
        raise ZeitError(f"Kein Projekt im Arbeitsvorrat passt zu {query!r}. Siehe: zeit projekte")
    lines = "\n".join(f"  {w['posid']:<20} {w['text']}" for w in hits[:15])
    raise ZeitError(f"{len(hits)} Projekte passen zu {query!r} – bitte genauer angeben:\n{lines}")


# ---------------------------------------------------------------- Befehle

def cmd_show(ts: Timesheet, cfg: dict, a) -> None:
    day = parse_date(a.datum)
    start, end = (day, day) if a.tag else _week(day)
    if a.bis:
        start, end = day, parse_date(a.bis)
    entries, cal = ts.entries(start, end), ts.calendar(start, end)
    by_day = defaultdict(list)
    for e in entries:
        by_day[e["WORKDATE"]].append(e)
    total = target = 0.0
    d = start
    while d <= end:
        key = f"{d:%Y%m%d}"
        items, c = by_day.get(key, []), cal.get(key, {})
        soll = float(c.get("TargetHours") or 0)
        ist = sum(float(e["TIME"]) for e in items)
        total, target = total + ist, target + soll
        if items or soll:
            mark = "✓" if ist >= soll > 0 else ("·" if soll == 0 else "!")
            print(f"\n{mark} {WDAY_NAMES[d.weekday()]} {d:%d.%m.%Y}   {ist:5.2f} / {soll:4.2f} h")
            for e in items:
                print(f"   {e['COUNTER']}  {_fmt_time(e.get('STARTTIME'))}-{_fmt_time(e.get('ENDTIME'))}"
                      f"  {float(e['TIME']):5.2f}  {BEMOT.get(e.get('BEMOT'), e.get('BEMOT', '')):<6}"
                      f"  {e.get('DISPTEXT1', '')[:34]:<34}  {e.get('LTXA1', '')[:40]:<40}"
                      f"  {STATUS.get(e.get('STATUS'), e.get('STATUS', ''))}")
        d += dt.timedelta(days=1)
    print(f"\nSumme {start:%d.%m.}–{end:%d.%m.%Y}: {total:.2f} / {target:.2f} h")


def cmd_projects(ts: Timesheet, cfg: dict, a) -> None:
    words = [w.lower() for w in a.suche]
    by_posid = {v["posid"]: k for k, v in cfg["aliases"].items()}
    for w in ts.worklist(dt.date.today()):
        if all(x in f"{w['posid']} {w['text']}".lower() for x in words):
            alias = f"[{by_posid[w['posid']]}]" if w["posid"] in by_posid else ""
            print(f"{w['posid']:<20} {w['text']:<45} {alias}")


def cmd_add(ts: Timesheet, cfg: dict, a) -> None:
    day = parse_date(a.datum)
    start, end = parse_range(a.zeit)
    text = " ".join(a.text)
    if len(text) > LTXA1_MAX:
        raise ZeitError(f"Kurztext ist {len(text)} Zeichen lang, erlaubt sind {LTXA1_MAX}.")
    proj, alias = resolve_project(ts, cfg, a.projekt, day)
    entry = time_entry(ts.pernr, "C", release=a.freigeben, day=day, start=start, end=end, posid=proj["posid"], text=text,
                       awart=a.awart or alias.get("awart") or cfg["default_awart"],
                       bemot=a.bemot or alias.get("bemot") or cfg["default_bemot"])
    summary = (f"{WDAY_NAMES[day.weekday()]} {day:%d.%m.%Y} {_fmt_time(start)}-{_fmt_time(end)} ({_hours(start, end):.2f} h)  "
               f"{proj['posid']} {proj['text']}  BEMOT {entry['TimeEntryDataFields']['BEMOT']}  „{text}“")
    if a.dry_run:
        print("[dry-run]", summary)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return
    res = ts.submit([entry])
    counter = res[0]["Counter"] if res else "?"
    print(f"Gebucht ({'freigegeben' if a.freigeben else 'gespeichert'}): {summary}  [{counter}]")


def cmd_edit(ts: Timesheet, cfg: dict, a) -> None:
    around = parse_date(a.date)
    old = ts.find_entry(a.counter, around)
    day = parse_date(a.datum) if a.datum else parse_date(old["WORKDATE"][:4] + "-" + old["WORKDATE"][4:6] + "-" + old["WORKDATE"][6:])
    start, end = parse_range(a.zeit) if a.zeit else (old["STARTTIME"], old["ENDTIME"])
    text = " ".join(a.text) if a.text else old.get("LTXA1", "")
    if len(text) > LTXA1_MAX:
        raise ZeitError(f"Kurztext ist {len(text)} Zeichen lang, erlaubt sind {LTXA1_MAX}.")
    proj, alias = resolve_project(ts, cfg, a.projekt or old["POSID"], day)
    entry = time_entry(ts.pernr, "U", counter=old["COUNTER"], release=a.freigeben, day=day, start=start, end=end,
                       posid=proj["posid"], text=text,
                       awart=a.awart or alias.get("awart") or old.get("AWART") or cfg["default_awart"],
                       bemot=a.bemot or alias.get("bemot") or old.get("BEMOT") or cfg["default_bemot"])
    if a.dry_run:
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return
    ts.submit([entry])
    print(f"Geändert: {old['COUNTER']}  {day:%d.%m.%Y} {_fmt_time(start)}-{_fmt_time(end)}  {proj['posid']}  „{text}“")


def cmd_delete(ts: Timesheet, cfg: dict, a) -> None:
    around = parse_date(a.date)
    olds = [ts.find_entry(c, around) for c in a.counter]
    for o in olds:
        print(f"  {o['COUNTER']}  {o['WORKDATE']}  {_fmt_time(o.get('STARTTIME'))}-{_fmt_time(o.get('ENDTIME'))}"
              f"  {o.get('DISPTEXT1', '')}  „{o.get('LTXA1', '')}“")
    if not a.ja and input(f"{len(olds)} Buchung(en) löschen? [j/N] ").strip().lower() not in ("j", "ja", "y"):
        print("Abgebrochen.")
        return
    ts.submit([time_entry(ts.pernr, "D", counter=o["COUNTER"], day=dt.datetime.strptime(o["WORKDATE"], "%Y%m%d").date())
               for o in olds])
    print(f"{len(olds)} Buchung(en) gelöscht.")


def cmd_release(ts: Timesheet, cfg: dict, a) -> None:
    day = parse_date(a.datum)
    start, end = (day, day) if a.tag else _week(day)
    todo = [e for e in ts.entries(start, end) if e.get("STATUS") == "MSAVE"]
    if not todo:
        print(f"Keine gespeicherten Buchungen zwischen {start:%d.%m.} und {end:%d.%m.%Y}.")
        return
    for e in todo:
        print(f"  {e['COUNTER']}  {e['WORKDATE']}  {float(e['TIME']):5.2f} h  {e.get('DISPTEXT1', '')}  „{e.get('LTXA1', '')}“")
    if not a.ja and input(f"{len(todo)} Buchung(en) freigeben? [j/N] ").strip().lower() not in ("j", "ja", "y"):
        print("Abgebrochen.")
        return
    ts.submit([time_entry(ts.pernr, "U", counter=e["COUNTER"], release=True,
                          day=dt.datetime.strptime(e["WORKDATE"], "%Y%m%d").date(),
                          start=e["STARTTIME"], end=e["ENDTIME"], posid=e["POSID"],
                          awart=e["AWART"], bemot=e["BEMOT"], text=e.get("LTXA1", ""))
               for e in todo])
    print(f"{len(todo)} Buchung(en) freigegeben.")


def cmd_alias(ts_factory, cfg: dict, a) -> None:
    aliases = cfg["aliases"]
    if a.aktion == "add":
        ts = ts_factory()
        proj, _ = resolve_project(ts, cfg, a.projekt, dt.date.today())
        aliases[a.name] = {k: v for k, v in {"posid": proj["posid"], "bemot": a.bemot, "awart": a.awart}.items() if v}
        save_config(cfg)
        print(f"Alias {a.name} → {proj['posid']} {proj['text']}")
    elif a.aktion == "rm":
        if aliases.pop(a.name, None) is None:
            raise ZeitError(f"Alias {a.name!r} existiert nicht.")
        save_config(cfg)
        print(f"Alias {a.name} entfernt.")
    else:
        if not aliases:
            print("Keine Aliase. Anlegen mit: zeit alias add NAME PROJEKT [--bemot 02]")
        for name, v in sorted(aliases.items()):
            extra = " ".join(f"{k}={v[k]}" for k in ("bemot", "awart") if k in v)
            print(f"{name:<12} {v['posid']:<20} {extra}")


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zeit", description="SAP-Zeiterfassung per Kerberos-SSO.",
                                epilog="Datumsangaben: heute/h, gestern/g, morgen, mo..so (aktuelle Woche), -2, 30.09., 2026-09-30")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", aliases=["woche", "w"], help="Buchungen der Woche (oder eines Tages) anzeigen")
    s.add_argument("datum", nargs="?", help="Tag innerhalb der Woche (Default: heute)")
    s.add_argument("-t", "--tag", action="store_true", help="nur diesen Tag")
    s.add_argument("--bis", help="Zeitraum von DATUM bis BIS")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("projekte", aliases=["p"], help="Arbeitsvorrat (PSP-Elemente) durchsuchen")
    s.add_argument("suche", nargs="*", help="Suchbegriffe (alle müssen passen)")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("add", aliases=["buche", "b"], help="Zeit buchen",
                       description="Beispiel: zeit add heute 8:30-10 csirt-op CSIRT-71")
    s.add_argument("datum")
    s.add_argument("zeit", help="VON-BIS, z. B. 8:30-10:00")
    s.add_argument("projekt", help="Alias, PSP-Element oder Suchbegriff")
    s.add_argument("text", nargs="+", help=f"Kurztext (max. {LTXA1_MAX} Zeichen)")
    s.add_argument("--bemot", help="Berechnungsmotiv: 01 abrechenbar, 02 nicht abrechenbar, 05 Reisezeit")
    s.add_argument("--awart", help="Ab-/Anwesenheitsart (Default 0800 Projekt)")
    s.add_argument("-f", "--freigeben", action="store_true", help="direkt freigeben statt nur speichern")
    s.add_argument("-n", "--dry-run", action="store_true", help="nur anzeigen, nichts senden")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("edit", aliases=["e"], help="Buchung ändern (nur angegebene Felder)")
    s.add_argument("counter", help="Buchungsnummer aus 'zeit show'")
    s.add_argument("--datum", help="neues Datum")
    s.add_argument("--zeit", help="neuer Zeitraum VON-BIS")
    s.add_argument("--projekt")
    s.add_argument("--text", nargs="+")
    s.add_argument("--bemot")
    s.add_argument("--awart")
    s.add_argument("--date", help="Suchzeitraum: 8 Wochen vor diesem Datum (Default: heute)")
    s.add_argument("-f", "--freigeben", action="store_true")
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("rm", aliases=["del", "loesche"], help="Buchung(en) löschen")
    s.add_argument("counter", nargs="+")
    s.add_argument("--date", help="Suchzeitraum: 8 Wochen vor diesem Datum (Default: heute)")
    s.add_argument("-y", "--ja", action="store_true", help="ohne Rückfrage")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("freigeben", aliases=["release"], help="gespeicherte Buchungen der Woche freigeben")
    s.add_argument("datum", nargs="?")
    s.add_argument("-t", "--tag", action="store_true", help="nur diesen Tag")
    s.add_argument("-y", "--ja", action="store_true", help="ohne Rückfrage")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("alias", help="Kurznamen für Projekte verwalten")
    s.add_argument("aktion", nargs="?", choices=["list", "add", "rm"], default="list")
    s.add_argument("name", nargs="?")
    s.add_argument("projekt", nargs="?", help="PSP-Element oder Suchbegriff (bei add)")
    s.add_argument("--bemot")
    s.add_argument("--awart")
    s.set_defaults(func=cmd_alias)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    try:
        if args.func is cmd_alias:
            if args.aktion in ("add", "rm") and not args.name or args.aktion == "add" and not args.projekt:
                raise ZeitError("Verwendung: zeit alias add NAME PROJEKT | zeit alias rm NAME")
            args.func(lambda: Timesheet(cfg), cfg, args)
        else:
            args.func(Timesheet(cfg), cfg, args)
    except ZeitError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
