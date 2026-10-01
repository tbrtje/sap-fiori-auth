# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
#   "truststore",
#   "requests-gssapi; sys_platform != 'win32'",
#   "requests-negotiate-sspi; sys_platform == 'win32'",
#   "playwright",
#   "pypdf",
# ]
# ///
"""zeit – CLI für die SAP-Zeiterfassung (CATS / HCM_TIMESHEET_MAN_SRV) mit Kerberos-SSO.

Anmeldung: SPNEGO am NetWeaver-Portal liefert das SSO-Cookie MYSAPSSO2, das auch
vom Fiori-Gateway akzeptiert wird. Es werden keine Passwörter benötigt oder gespeichert.

Alternativ (auth = "browser", z. B. SAP BTP mit M365-Login): Ein eigenes Browser-Profil hält die
Microsoft-Anmeldung. Jeder Aufruf holt darüber headless die Session des App-Routers.
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import truststore
from requests.adapters import HTTPAdapter

DEFAULTS = {
    "auth": "kerberos",
    "portal_url": "https://portal.btc-ag.com/irj/portal",
    "service_url": "https://bgp.btcsap.btc-ag.com:44300/sap/opu/odata/sap/HCM_TIMESHEET_MAN_SRV",
    "sap_client": "300",
    "pernr": None,
    "default_awart": "0800",
    "default_bemot": "01",
    "aliases": {},
    "login_url": None,
    "account": None,
    "browser": None,
    "system": None,
    "systems": {},
}
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sap-zeit" / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "sap-zeit"
SERVICE_RE = re.compile(r"^(https://[^?#]+?/sap/opu/odata/sap/[A-Z0-9_]*TIMESHEET[A-Z0-9_]*)(?=[/?;]|$)")

# Das Portal akzeptiert nur ein Browser-ähnliches User-Agent.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

BEMOT = {"01": "abr.", "02": "n.abr.", "05": "Reise"}
STATUS = {"MSAVE": "gespeichert", "MACTION": "freigegeben", "MAPPROVED": "genehmigt", "MREJECTED": "abgelehnt"}
WDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
LTXA1_MAX = 40


class ZeitError(Exception):
    pass


class AuthError(ZeitError):
    """Session abgelaufen (401/403 oder Umleitung zur Anmeldung). Neu verbinden hilft."""


class LoginRequired(ZeitError):
    """Nur eine interaktive Anmeldung hilft (zeit login)."""

    def __init__(self, msg: str, system: str | None = None):
        super().__init__(msg)
        self.system = system


# ---------------------------------------------------------------- Konfiguration

def read_config_file() -> dict:
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def load_config(system: str | None = None) -> dict:
    """Config-Datei über den Defaults, darüber die Einstellungen des gewählten Systems.
    Ohne System (oder mit "kerberos") gelten nur die Werte der obersten Ebene, also Kerberos am Portal."""
    cfg = dict(DEFAULTS)
    cfg.update(read_config_file())
    name = system or os.environ.get("ZEIT_SYSTEM") or cfg["system"]
    cfg["system"] = None if name == "kerberos" and name not in cfg["systems"] else name
    cfg.update(cfg["systems"].get(cfg["system"], {}))
    return cfg


def save_config(raw: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- HTTP / SAP

class _TLSAdapter(HTTPAdapter):
    """System-Trust-Store (interne BTC-CA) und der vom Portal benötigte RSA-Cipher."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.set_ciphers("DEFAULT:AES128-GCM-SHA256")
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


def _negotiate_auth(host: str) -> requests.auth.AuthBase:
    """Kerberos/SPNEGO mit dem Ticket der Betriebssystem-Anmeldung (Windows: SSPI, sonst GSSAPI)."""
    if sys.platform == "win32":
        from requests_negotiate_sspi import HttpNegotiateAuth
        # host explizit, sonst würde der Name per DNS kanonisiert und ggf. ein falscher SPN angefragt
        return HttpNegotiateAuth(host=host)
    from requests_gssapi import OPTIONAL, HTTPSPNEGOAuth
    return HTTPSPNEGOAuth(mutual_authentication=OPTIONAL)


def _default_browser() -> str | None:
    """Playwright-Channel des Standardbrowsers, falls es Chrome oder Edge ist (Safari/Firefox gehen nicht)."""
    ident = ""
    try:
        if sys.platform == "darwin":
            import plistlib
            plist = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
            handlers = plistlib.loads(plist.read_bytes()).get("LSHandlers", [])
            ident = next((h.get("LSHandlerRoleAll", "") for h in handlers if h.get("LSHandlerURLScheme") == "https"), "")
        elif sys.platform == "win32":
            import winreg
            key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                ident = winreg.QueryValueEx(k, "ProgId")[0]
        else:
            import subprocess
            ident = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True).stdout
    except Exception:
        return None
    ident = ident.lower()
    return "chrome" if "chrome" in ident else "msedge" if "edge" in ident or "msedgehtm" in ident else None


def _browser_profile(system: str, channel: str) -> Path:
    """Eigenes Profil je System und Browser: Chrome kann die (per Schlüsselbund verschlüsselten) Cookies
    eines Edge-Profils nicht lesen und umgekehrt."""
    base = DATA_DIR / "browser" / system
    if (base / "Local State").exists():
        # Altes Layout (ein Edge-Profil direkt unter browser/<system>) einmalig nach browser/<system>/msedge.
        tmp = base.with_name(base.name + ".migrate")
        base.rename(tmp)
        base.mkdir(parents=True)
        tmp.rename(base / "msedge")
    return base / channel


def _browser_context(p, cfg: dict, headless: bool):
    """Startet den Standardbrowser (sonst Chrome, Edge, Chromium) mit eigenem Profil des Systems, nicht dem des Users.
    Fest wählen lässt er sich mit "browser" in der Config."""
    from playwright.sync_api import Error as PlaywrightError
    order = [cfg["browser"]] if cfg.get("browser") else list(dict.fromkeys(
        [c for c in [_default_browser()] if c] + ["chrome", "msedge", "chromium"]))
    errors = []
    for ch in order:
        profile = _browser_profile(cfg["system"] or "default", ch)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            return p.chromium.launch_persistent_context(profile, channel=None if ch == "chromium" else ch,
                                                        headless=headless, no_viewport=not headless)
        except PlaywrightError as e:
            errors.append(f"{ch}: {str(e).strip().splitlines()[0]}")
    raise ZeitError("Kein Browser startbar (läuft schon ein 'zeit login'?):\n  " + "\n  ".join(errors))


def _on_host(url: str, host: str) -> bool:
    u = urlparse(url)
    return u.hostname == host and "/login/callback" not in u.path


def _account_tiles(page) -> list[str]:
    """E-Mail-Adressen der Kacheln in Microsofts „Konto auswählen“ (data-test-id = Adresse in Kleinbuchstaben)."""
    ids = [el.get_attribute("data-test-id") or "" for el in page.query_selector_all("[data-test-id][role=button]")]
    return [i for i in ids if "@" in i and not i.endswith("-menu-dots")]


def _browser_session(cfg: dict, target: str) -> tuple[list[dict], str, str | None]:
    """Öffnet target headless im Browser-Profil; die Microsoft-Anmeldung läuft dort ohne Rückfrage durch.
    Sind mehrere Microsoft-Konten angemeldet, fragt Microsoft nach dem Konto. Dann wird das konfigurierte
    gewählt, sonst werden die Konten der Reihe nach probiert. Liefert Cookies, User-Agent und das gewählte Konto."""
    import time
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    host = urlparse(target).hostname
    wanted = (cfg.get("account") or "").lower()
    with sync_playwright() as p:
        ctx = _browser_context(p, cfg, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ua = page.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            ctx.set_extra_http_headers({"User-Agent": ua})
            page.goto(target, timeout=60_000)
            tried: list[str] = []
            chosen, since, deadline = None, time.monotonic(), time.monotonic() + 90
            while not _on_host(page.url, host):
                now = time.monotonic()
                if now > deadline:
                    break
                try:
                    tiles = _account_tiles(page)
                    if tiles:
                        todo = [t for t in ([wanted] if wanted in tiles else []) + tiles if t not in tried]
                        if not todo:
                            break
                        chosen = todo[0]
                        tried.append(chosen)
                        page.click(f'[data-test-id="{chosen}"]', timeout=5_000)
                        since = now
                    elif page.query_selector("#KmsiCheckboxField"):
                        page.click("#idSIButton9", timeout=5_000)  # „Angemeldet bleiben?“ → Ja
                    elif chosen and now - since > 15:
                        # Falsches Konto (z. B. Gast in fremdem Tenant): Anmeldung neu starten, nächstes probieren.
                        page.goto(target, timeout=60_000)
                        since = now
                except PlaywrightError:
                    pass  # Seite lädt gerade neu
                page.wait_for_timeout(500)
            if not _on_host(page.url, host):
                hint = f" Angemeldete Konten: {', '.join(tried)}." if tried else ""
                raise LoginRequired(f"Browser-Anmeldung abgelaufen (hängt bei {urlparse(page.url).hostname}).{hint} "
                                    f"Neu anmelden mit: zeit login {cfg['system'] or ''}".rstrip(), cfg["system"])
            return ctx.cookies([target]), ua, chosen
        finally:
            ctx.close()


class Timesheet:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.svc = cfg["service_url"].rstrip("/")
        self.s = requests.Session()
        self.s.mount("https://", _TLSAdapter())
        self.s.headers["User-Agent"] = USER_AGENT
        self._csrf = None
        self._pernr = cfg.get("pernr")
        self.s.hooks["response"].append(self._check_session)
        self._login()

    def _login(self) -> None:
        if self.cfg["auth"] == "browser":
            return self._browser_login()
        if self.cfg["auth"] != "kerberos":
            raise ZeitError(f"Unbekannte Anmeldemethode {self.cfg['auth']!r} (kerberos oder browser).")
        try:
            # Kurzer Connect-Timeout: außerhalb des Firmennetzes schnell auf die Browser-Anmeldung wechseln.
            r = self.s.get(self.cfg["portal_url"], auth=_negotiate_auth(urlparse(self.cfg["portal_url"]).hostname), timeout=(5, 30))
        except requests.RequestException as e:
            raise ZeitError(f"Portal nicht erreichbar: {e}") from e
        if r.status_code == 401 or "MYSAPSSO2" not in self.s.cookies:
            raise ZeitError("Kerberos-Anmeldung fehlgeschlagen. Gültiges Ticket vorhanden? (klist / kinit)")

    def _browser_login(self) -> None:
        if not self.cfg.get("service_url") or self.cfg["service_url"] == DEFAULTS["service_url"]:
            raise LoginRequired(f"Service-URL für System {self.cfg['system']!r} unbekannt. Einrichten mit: "
                                "zeit login NAME --url LAUNCHPAD-URL", self.cfg["system"])
        cookies, ua, account = _browser_session(self.cfg, f"{self.svc}/{self._client_qs()}")
        if account and account != (self.cfg.get("account") or "").lower():
            # Funktionierendes Konto merken, damit beim nächsten Mal nicht erst probiert wird.
            raw = read_config_file()
            raw.setdefault("systems", {}).setdefault(self.cfg["system"], {})["account"] = account
            save_config(raw)
        self.s.headers["User-Agent"] = ua
        for c in cookies:
            self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"], secure=c["secure"])

    def _check_session(self, r: requests.Response, **_) -> None:
        """Abgelaufene Session erkennen. SAP antwortet dann oft nicht mit 401: Der BTP-App-Router leitet zur
        Anmeldung um oder liefert mit HTTP 200 eine HTML-Anmeldeseite (fragmentAfterLogin), das Gateway
        ebenso eine HTML-Logon-Seite. OData liefert sonst nie HTML."""
        u = urlparse(r.url)
        if self.cfg["auth"] == "browser" and u.hostname != urlparse(self.svc).hostname:
            raise AuthError(f"Nicht angemeldet (Weiterleitung zu {u.hostname}).")
        if "/sap/opu/odata/" in u.path and r.headers.get("content-type", "").startswith("text/html"):
            raise AuthError(f"Session abgelaufen (Anmeldeseite statt Daten, HTTP {r.status_code}).")

    def _request(self, method: str, url: str, write: bool = False, **kw) -> requests.Response:
        """Verbindungsfehler (z. B. nach WLAN-Wechsel) gelten als verlorene Session (AuthError): Wer die Session
        länger hält, kann dann neu verbinden. Bei Schreibzugriffen nur, wenn die Anfrage SAP nachweislich nicht
        erreicht hat, sonst wäre ein zweiter Versuch eine Doppelbuchung."""
        try:
            return self.s.request(method, url, **kw)
        except requests.RequestException as e:
            unreached = isinstance(e, requests.ConnectTimeout) or any(
                s in str(e) for s in ("NameResolutionError", "NewConnectionError", "Failed to resolve", "ConnectTimeoutError"))
            if not write or unreached:
                raise AuthError(f"Verbindung zu SAP unterbrochen: {e.__class__.__name__}") from e
            raise ZeitError("Verbindung beim Speichern abgebrochen. Ob SAP gespeichert hat, ist unklar, "
                            "bitte neu laden und prüfen.") from e

    def _client(self) -> dict:
        # Auf der BTP setzt oft die Destination den Mandanten, dann ist sap_client leer.
        return {"sap-client": self.cfg["sap_client"]} if self.cfg["sap_client"] else {}

    def _client_qs(self) -> str:
        return f"?sap-client={self.cfg['sap_client']}" if self.cfg["sap_client"] else ""

    def _params(self, extra: dict | None = None) -> dict:
        p = {**self._client(), "$format": "json"}
        p.update(extra or {})
        return p

    def get(self, entity_set: str, flt: str | None = None) -> list[dict]:
        r = self._request("GET", f"{self.svc}/{entity_set}", params=self._params({"$filter": flt} if flt else None), timeout=60)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if r.status_code in (401, 403):
            raise AuthError(f"{entity_set}: HTTP {r.status_code}, Session abgelaufen?")
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
        # RecordNumber beginnt je Tag wieder bei 1, ist über mehrere Tage also nicht eindeutig. Die Zeilen einer
        # Buchung kommen zusammenhängend: neue Buchung, sobald RecordNumber wechselt oder ein Feld erneut auftaucht.
        records: list[dict] = []
        prev = None
        for row in self.get("TimeDataList", self._flt(start, end)):
            if row["RecordNumber"] != prev or row["FieldName"] in records[-1]:
                records.append({})
                prev = row["RecordNumber"]
            records[-1][row["FieldName"]] = row["FieldValue"]
        out = [r for r in records if r.get("COUNTER")]
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
            r = self._request("GET", self.svc + "/", params=self._client(), headers={"X-CSRF-Token": "Fetch"}, timeout=30)
            self._csrf = r.headers.get("x-csrf-token")
            if r.status_code in (401, 403):
                raise AuthError(f"CSRF-Token: HTTP {r.status_code}")
            if not self._csrf:
                raise ZeitError(f"Kein CSRF-Token erhalten (HTTP {r.status_code}).")
        return self._csrf

    def _send(self, method: str, path: str, **kw) -> requests.Response:
        """Schreibzugriff mit CSRF-Token. Ein abgelaufenes Token wird einmal neu geholt."""
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        for retry in (False, True):
            r = self._request(method, f"{self.svc}/{path}", write=True, params=self._client(), timeout=120,
                              headers={**headers, "X-CSRF-Token": self._csrf_token()}, **kw)
            if r.status_code == 403 and r.headers.get("x-csrf-token", "").lower() == "required" and not retry:
                self._csrf = None
                continue
            break
        if r.status_code in (401, 403):
            raise AuthError(f"{path}: HTTP {r.status_code} {_sap_message(r.text)}")
        return r

    def submit(self, entries: list[dict]) -> list[dict]:
        """Sendet TimeEntries in einem $batch, je Buchung ein Changeset (Teilerfolg möglich)."""
        batch, changeset = f"batch_{uuid.uuid4().hex}", f"changeset_{uuid.uuid4().hex}"
        parts = []
        for e in entries:
            body = json.dumps(e, ensure_ascii=False).encode()
            parts.append(
                f"--{changeset}\r\nContent-Type: application/http\r\nContent-Transfer-Encoding: binary\r\n\r\n"
                f"POST TimeEntries{self._client_qs()} HTTP/1.1\r\n"
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
        r = self._send("POST", "$batch", data=payload, headers={"Content-Type": f"multipart/mixed; boundary={batch}"})
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

    # -- Favoriten der Fiori-App (Anlegen/Umbenennen/Löschen wie die Standard-App, ohne $batch)

    def favorites(self) -> list[dict]:
        return self.get("Favorites", f"Pernr eq '{self.pernr}'")

    def _fav_path(self, fid: str) -> str:
        return f"Favorites(ID='{fid.strip()}',Pernr='{self.pernr}')"

    def create_favorite(self, name: str, fields: dict) -> dict:
        r = self._send("POST", "Favorites", json={"Pernr": self.pernr, "Name": name, "FavoriteDataFields": fields})
        if r.status_code != 201:
            raise ZeitError(f"Favorit anlegen: HTTP {r.status_code} {_sap_message(r.text)}")
        return r.json()["d"]

    def rename_favorite(self, fid: str, name: str) -> None:
        # PUT übernimmt nur den Namen, FavoriteDataFields ignoriert SAP dabei.
        r = self._send("PUT", self._fav_path(fid), json={"ID": fid.strip(), "Pernr": self.pernr, "Name": name})
        if r.status_code >= 400:
            raise ZeitError(f"Favorit umbenennen: HTTP {r.status_code} {_sap_message(r.text)}")

    def delete_favorite(self, fid: str) -> None:
        r = self._send("DELETE", self._fav_path(fid))
        if r.status_code >= 400:
            raise ZeitError(f"Favorit löschen: HTTP {r.status_code} {_sap_message(r.text)}")

    def time_statement(self, start: dt.date, end: dt.date) -> bytes:
        """Monatlicher Zeitnachweis als PDF (Fiori „Meine Formulare“, wie die Browser-Erweiterung Clockmate)."""
        base = self.svc.rsplit("/", 1)[0] + "/HCMFAB_MYFORMS_SRV"
        params = f"BEGDA%3D{start:%Y%m%d}%40%3BENDDA%3D{end:%Y%m%d}"
        r = self._request("GET", f"{base}/FormDisplaySet(EmployeeNumber='{self.pernr}',FormType='SAP_INT_TIM_STM',"
                          f"ParametersValues='{params}')/$value", params=self._client(), timeout=120)
        if r.status_code in (401, 403):
            raise AuthError(f"Zeitnachweis: HTTP {r.status_code}")
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise ZeitError(f"Zeitnachweis {start:%m/%Y}: HTTP {r.status_code} {_sap_message(r.text)}")
        return r.content

    def value_help(self, field: str) -> dict[str, str]:
        return {r["FieldId"]: r["FieldValue"] for r in self.get("ValueHelpList", f"Pernr eq '{self.pernr}' and FieldName eq '{field}'")}


FLEX_LABELS = {"end": ("Total Flextime Balance", "GLZ-Saldo aktuell"),
               "previous": ("Flextime Balance for Preceding Period", "GLZ-Saldo Vorperiode")}


def parse_time_statement(pdf: bytes) -> dict[str, float]:
    """Liest die Gleitzeitsalden aus dem Zeitnachweis, z. B. „Total Flextime Balance    58.00“.
    SAP schreibt negative Werte je nach Formular als „- 3.00“ oder „3.00-“."""
    import io
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)
    out = {}
    for key, labels in FLEX_LABELS.items():
        for label in labels:
            if m := re.search(re.escape(label) + r"\s*(-?)\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+)(-?)", text):
                num = m.group(2)
                num = num.replace(".", "").replace(",", ".") if "," in num[-3:] else num.replace(",", "")
                out[key] = -float(num) if "-" in (m.group(1) + m.group(3)) else float(num)
                break
    if "end" not in out:
        raise ZeitError("Gleitzeitsaldo im Zeitnachweis nicht gefunden.")
    return out


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


def release_entry(pernr: str, e: dict) -> dict:
    """Update einer gelesenen Buchung (TimeDataList) mit Freigabe."""
    return time_entry(pernr, "U", counter=e["COUNTER"], release=True, day=dt.datetime.strptime(e["WORKDATE"], "%Y%m%d").date(),
                      start=e["STARTTIME"], end=e["ENDTIME"], posid=e["POSID"],
                      awart=e["AWART"], bemot=e["BEMOT"], text=e.get("LTXA1", ""))


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
    ts.submit([release_entry(ts.pernr, e) for e in todo])
    print(f"{len(todo)} Buchung(en) freigegeben.")


def cmd_alias(ts_factory, cfg: dict, a) -> None:
    aliases = cfg["aliases"]
    if a.aktion == "add":
        ts = ts_factory()
        proj, _ = resolve_project(ts, cfg, a.projekt, dt.date.today())
        aliases[a.name] = {k: v for k, v in {"posid": proj["posid"], "bemot": a.bemot, "awart": a.awart}.items() if v}
        save_config({**read_config_file(), "aliases": aliases})
        print(f"Alias {a.name} → {proj['posid']} {proj['text']}")
    elif a.aktion == "rm":
        if aliases.pop(a.name, None) is None:
            raise ZeitError(f"Alias {a.name!r} existiert nicht.")
        save_config({**read_config_file(), "aliases": aliases})
        print(f"Alias {a.name} entfernt.")
    else:
        if not aliases:
            print("Keine Aliase. Anlegen mit: zeit alias add NAME PROJEKT [--bemot 02]")
        for name, v in sorted(aliases.items()):
            extra = " ".join(f"{k}={v[k]}" for k in ("bemot", "awart") if k in v)
            print(f"{name:<12} {v['posid']:<20} {extra}")


def cmd_login(cfg: dict, a) -> None:
    """Interaktive M365-Anmeldung im eigenen Browser-Profil; erkennt dabei die Service-URL der Zeiterfassung."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    name = a.name or cfg["system"]
    if not name:
        raise ZeitError("Systemname fehlt: zeit login NAME --url LAUNCHPAD-URL")
    raw = read_config_file()
    sysc = raw.setdefault("systems", {}).setdefault(name, {"auth": "browser"})
    if a.url:
        sysc["login_url"] = a.url
    if a.konto:
        sysc["account"] = a.konto.strip().lower()
    if sysc.get("auth") != "browser" or not sysc.get("login_url"):
        raise ZeitError(f"System {name!r} hat keine Browser-Anmeldung. Einrichten mit: zeit login {name} --url LAUNCHPAD-URL")
    lcfg = {**DEFAULTS, **raw, **sysc, "system": name}
    known = sysc.get("service_url")
    found: dict[str, str] = {}

    def on_request(req) -> None:
        if m := SERVICE_RE.match(req.url):
            found.setdefault("service_url", m.group(1))
            if client := parse_qs(urlparse(req.url).query).get("sap-client"):
                found.setdefault("sap_client", client[0])

    print("Browserfenster öffnet sich. Melde dich mit deinem M365-Konto an ('Angemeldet bleiben': Ja)"
          + ("." if known else " und öffne dann die App „Meine Zeiterfassung“.")
          + " Das Fenster schließt sich danach von selbst.", file=sys.stderr)
    with sync_playwright() as p:
        ctx = _browser_context(p, lcfg, headless=False)
        ctx.on("request", on_request)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        done = lambda: "service_url" in found or bool(known) and any(_on_host(pg.url, urlparse(known).hostname) for pg in ctx.pages)
        ok = False
        try:
            page.goto(sysc["login_url"], timeout=0)
            while ctx.pages and not (ok := done()):
                ctx.pages[0].wait_for_timeout(500)
            if ok:
                ctx.pages[0].wait_for_timeout(3000)  # weitere Requests der App abwarten (sap-client)
        except PlaywrightError:
            ok = ok or "service_url" in found  # Fenster oder Browser wurde geschlossen
        finally:
            ctx.close()
    if not ok:
        raise ZeitError("Fenster geschlossen, bevor die Zeiterfassung geöffnet wurde. Bitte erneut: zeit login " + name)
    if "service_url" in found and found["service_url"] != known:
        sysc["service_url"] = found["service_url"]
        sysc["sap_client"] = found.get("sap_client", "")
        print(f"Service erkannt: {sysc['service_url']}" + (f" (sap-client {sysc['sap_client']})" if sysc["sap_client"] else ""))
        if not sysc["service_url"].endswith("/HCM_TIMESHEET_MAN_SRV"):
            print("Warnung: Das ist nicht HCM_TIMESHEET_MAN_SRV. zeit unterstützt nur diesen Service, "
                  "Aufrufe können daher fehlschlagen.", file=sys.stderr)
    if a.standard:
        raw["system"] = name
    save_config(raw)
    ts = Timesheet(load_config(name))
    print(f"Angemeldet an {name!r}, Personalnummer {ts.pernr}."
          + ("" if raw.get("system") == name else f" Nutzen mit: zeit -s {name} show (oder zeit login {name} --standard)"))


def cmd_favorites(ts: Timesheet, cfg: dict, a) -> None:
    favs = [fav_out(f) for f in ts.favorites()]
    if not favs:
        print("Keine Favoriten. Anlegen in der Fiori-App „Meine Zeiterfassung“.")
    for f in favs:
        span = f"{f['start']}-{f['end']}" if f["start"] else "           "
        print(f"{f['name']:<24} {span}  {f['posid']:<20} {BEMOT.get(f['bemot'], f['bemot']):<6}  „{f['text']}“")


# ---------------------------------------------------------------- Favoriten und Gleitzeit

def _hhmm(t: str | None) -> str:
    return f"{t[:2]}:{t[2:4]}" if t and len(t) >= 4 and t.strip("0") else ""


def fav_out(f: dict) -> dict:
    d = f["FavoriteDataFields"]
    return {"id": f["ID"].strip(), "name": f["Name"], "info": f.get("Field_Text", ""), "posid": d.get("POSID", ""),
            "text": d.get("LTXA1", ""), "awart": d.get("AWART", ""), "bemot": d.get("BEMOT", ""),
            "start": _hhmm(d.get("BEGUZ")), "end": _hhmm(d.get("ENDUZ")), "hours": float(d.get("CATSHOURS") or 0)}


def _months(s: dt.date, e: dt.date):
    while s <= e:
        nxt = (s.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield s, min(nxt - dt.timedelta(days=1), e)
        s = nxt


def day_sums(ts: Timesheet, s: dt.date, e: dt.date) -> list[dict]:
    """Tagessummen für Gleitzeit und Faktura, wie sie die Zeitauswertung rechnet.
    SAP erzeugt Abwesenheiten als Buchungen mit AWART 9xxx (z. B. „generiert - 9001 - Urlaub“, 8 h). Sie sind keine
    Arbeitszeit, reduzieren aber das Soll (target = planned - Abwesenheit). Ein Gleittag (9003) reduziert es nicht,
    er verbraucht also Gleitzeit. Saldo eines Tages: work - target."""
    with ThreadPoolExecutor(4) as ex:
        entries = ex.submit(ts.entries, s, e)
        # WorkCalendars liefert bei Zeiträumen ab etwa 4 Monaten an Abwesenheitstagen schon reduziertes Soll,
        # bei kürzeren das geplante. Monatsweise abfragen, damit es immer das geplante ist.
        cals = [ex.submit(ts.calendar, a, b) for a, b in _months(s, e)]
        cal = {k: v for f in cals for k, v in f.result().items()}
        entries = entries.result()
    days = {k: {"date": f"{k[:4]}-{k[4:6]}-{k[6:8]}", "planned": float(c.get("TargetHours") or 0),
                "work": 0.0, "billable": 0.0, "flexoff": 0.0, "absence": 0.0} for k, c in cal.items()}
    for x in entries:
        d = days.get(x["WORKDATE"])
        if d is None:
            continue
        h, awart = float(x.get("TIME") or 0), x.get("AWART", "")
        if awart == "9003":
            d["flexoff"] += h
        elif awart.startswith("9"):
            d["absence"] += h
        else:
            d["work"] += h
            if x.get("BEMOT") == "01":
                d["billable"] += h
    for d in days.values():
        d["target"] = max(d["planned"] - d["absence"], 0.0)
    return [days[k] for k in sorted(days)]


def flextime(ts: Timesheet, months_back: int = 6, cache: dict | None = None) -> dict:
    """Gleitzeitkonto wie im SAP-Zeitnachweis. Der Nachweis zählt nur genehmigte Buchungen. Deshalb gilt der
    Saldo des letzten Monats, dessen Buchungen alle genehmigt sind, danach die eigene Tagesrechnung bis gestern.
    cache hält bereits gelesene Zeitnachweise (abgeschlossene Monate ändern sich nicht)."""
    cache = {} if cache is None else cache
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    first = today.replace(day=1)
    months = []
    for _ in range(months_back):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        months.append(first)
    entries = ts.entries(months[-1], yesterday)
    open_months = {e["WORKDATE"][:6] for e in entries if e.get("STATUS") != "DONE" and not e.get("AWART", "").startswith("9")}
    errors = []
    for m in months:
        if f"{m:%Y%m}" in open_months:
            continue
        end = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        key = f"{m:%Y%m}"
        try:
            if key not in cache:
                cache[key] = parse_time_statement(ts.time_statement(m, end))
        except ZeitError as e:
            errors.append(str(e))
            continue
        st = cache[key]
        days = day_sums(ts, end + dt.timedelta(days=1), yesterday) if end < yesterday else []
        return {"statement_start": f"{m:%Y-%m-%d}", "statement_end": f"{end:%Y-%m-%d}", "balance": st["end"],
                "previous": st.get("previous"), "days": days,
                "open_months": sorted(f"{k[:4]}-{k[4:]}" for k in open_months)}
    raise ZeitError("Kein Zeitnachweis mit vollständig genehmigten Buchungen in den letzten "
                    f"{months_back} Monaten." + (f" ({errors[0]})" if errors else ""))


def cmd_flex(ts: Timesheet, cfg: dict, a) -> None:
    r = flextime(ts)
    since = sum(d["work"] - d["target"] for d in r["days"])
    fmt = lambda h: f"{h:+.2f} h".replace(".", ",")
    print(f"Gleitzeit: {fmt(r['balance'] + since)} (Stand gestern)")
    print(f"  Zeitnachweis bis {dt.date.fromisoformat(r['statement_end']):%d.%m.%Y}: {fmt(r['balance'])}")
    print(f"  seitdem aus der Zeiterfassung: {fmt(since)}")
    if r["open_months"]:
        print(f"  noch nicht genehmigt: {', '.join(r['open_months'])}")


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zeit", description="SAP-Zeiterfassung per Kerberos-SSO.",
                                epilog="Datumsangaben: heute/h, gestern/g, morgen, mo..so (aktuelle Woche), -2, 30.09., 2026-09-30")
    p.add_argument("-s", "--system", help="System aus 'systems' der Config, z. B. btp; 'kerberos' = Portal-Anmeldung "
                                          "(Default: $ZEIT_SYSTEM, sonst 'system' der Config, sonst kerberos)")
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

    s = sub.add_parser("login", help="Browser-Anmeldung (M365) für ein System einrichten oder erneuern",
                       description="Beispiel: zeit login btp --url https://…launchpad.cfapps.eu20.hana.ondemand.com/site?siteId=…")
    s.add_argument("name", nargs="?", help="Systemname in der Config (Default: gewähltes System)")
    s.add_argument("--url", help="Launchpad-URL (nur beim ersten Mal nötig)")
    s.add_argument("--standard", action="store_true", help="System als Standard statt Kerberos setzen")
    s.add_argument("--konto", help="Microsoft-Konto (E-Mail), falls im Browser mehrere angemeldet sind")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("favoriten", aliases=["fav"], help="Favoriten der Fiori-App anzeigen")
    s.set_defaults(func=cmd_favorites)

    s = sub.add_parser("gleitzeit", aliases=["glz"], help="Gleitzeitkonto (Zeitnachweis + Buchungen seitdem)")
    s.set_defaults(func=cmd_flex)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows schreibt umgeleitete Ausgaben sonst in cp1252 und scheitert an ✓, „“ usw.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.system)
        if cfg["system"] and cfg["system"] not in cfg["systems"] and args.func is not cmd_login:
            raise ZeitError(f"System {cfg['system']!r} ist nicht konfiguriert. Einrichten mit: zeit login {cfg['system']} --url LAUNCHPAD-URL")
        if args.func is cmd_login:
            args.func(cfg, args)
        elif args.func is cmd_alias:
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
