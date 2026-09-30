# zeit – SAP-Zeiterfassung per CLI

CLI für die Fiori-App „Meine Zeiterfassung“ (CATS, OData-Service `HCM_TIMESHEET_MAN_SRV`).
Die Anmeldung läuft per Kerberos: SPNEGO am Portal liefert das SSO-Cookie `MYSAPSSO2`,
das auch das Fiori-Gateway akzeptiert. Die CLI speichert keine Passwörter und keine Cookies.
Für Systeme ohne Kerberos (z. B. SAP BTP mit M365-Login) gibt es alternativ eine
[Browser-Anmeldung](#zweites-system-mit-m365-anmeldung-sap-btp).

## Installation

Es muss nichts vorinstalliert sein. Auf einem Firmen-Mac liegt das Kerberos-Ticket schon mit der
Anmeldung vor (prüfen mit `klist`). Fehlt [`uv`](https://docs.astral.sh/uv/), lädt `bin/zeit` es beim ersten
Aufruf mit Prüfsummen-Check nach `~/.local/share/sap-zeit/bin`. uv holt dann Python und die Pakete.
Der erste Start braucht deshalb etwa 10 Sekunden und Internetzugriff (GitHub, PyPI, zusammen ca. 120 MB),
danach dauert ein Aufruf rund 2 Sekunden. Shell-Profile und System bleiben unverändert.

**Plattformen:** macOS (getestet), Windows und Linux (umgesetzt, aber ungetestet). Unter Windows läuft
`bin/zeit` in Git Bash. Die nutzt Claude Code dort ohnehin. Die Anmeldung erfolgt per SSPI mit der
Windows-Domänenanmeldung, `klist` ist dafür nicht nötig.

### Als Claude-Code-Plugin

```
/plugin marketplace add git@ssh.dev.azure.com:v3/btc-cloud-aws/KI%20Hackathon/gruppe7
/plugin install sap-zeit@gruppe7
```

Danach kann Claude die Zeiterfassung direkt bedienen, zum Beispiel „zeig meine Woche“ oder
„buch heute 9–10 CSIRT-71 auf CSIRT Operation“. Der Skill `sap-zeit:zeit` erklärt Claude die
CLI und schreibt vor, dass Claude vor jeder Buchung einen Dry-Run zeigt. `zeit` liegt
außerdem im PATH der Claude-Session. Updates holst du mit `/plugin marketplace update gruppe7`.

### Als Kommandozeilen-Tool

```bash
uv tool install --editable .     # stellt den Befehl `zeit` bereit
# oder ohne Installation: uv run zeit.py ...  (Abhängigkeiten stehen im Skript-Header)
```

### Mit Nix (z. B. NixOS)

```bash
nix run .#zeit -- show           # direkt ausführen
nix profile install .#zeit       # ins Benutzerprofil installieren
```

In einer NixOS-Konfiguration als Flake-Input einbinden:

```nix
inputs.sap-zeit.url = "git+ssh://git@ssh.dev.azure.com/v3/btc-cloud-aws/KI%20Hackathon/gruppe7";
# …
environment.systemPackages = [ inputs.sap-zeit.packages.${pkgs.system}.default ];
# oder per Overlay: nixpkgs.overlays = [ inputs.sap-zeit.overlays.default ];  → pkgs.zeit
```

Das Standard-System lässt sich im Paket festlegen (entspricht `ZEIT_SYSTEM`, `-s` gilt weiterhin):

```nix
environment.systemPackages = [
  (inputs.sap-zeit.packages.${pkgs.system}.default.override { zeitSystem = "btp"; })
];
```

Das Paket bringt für die Browser-Anmeldung das Playwright-Chromium aus nixpkgs mit, falls weder
Edge noch Chrome installiert ist.

## Benutzung

```bash
zeit show                  # aktuelle Woche mit Ist/Soll pro Tag
zeit show gestern -t       # nur ein Tag
zeit projekte ewe          # Arbeitsvorrat durchsuchen

zeit add heute 8:30-10 "csirt op" CSIRT-71          # Projekt per Suchbegriff
zeit add mo 13-14 NE.066926.10.0004 CCEWE-11700     # oder per PSP-Element
zeit add heute 9-9:15 ausb "Ausbildung Nico" -n     # -n = Dry-Run

zeit edit 27753676 --zeit 8:45-9:30 --text CSIRT-71
zeit rm 27753676
zeit freigeben             # gespeicherte Buchungen der Woche freigeben

zeit alias add csop "csirt op"                      # Kurzname für ein Projekt
zeit alias add ausb ausbildungsbetreuung --bemot 02 # mit Default-BEMOT
```

Datumsangaben: `heute`/`h`, `gestern`/`g`, `morgen`, `mo`…`so` (aktuelle Woche), `-2`, `30.09.`, `2026-09-30`.

BEMOT: `01` abrechenbar (Default), `02` nicht abrechenbar, `05` Reisezeit.

### Zweites System mit M365-Anmeldung (SAP BTP)

Für Systeme ohne Kerberos, deren Anmeldung über Microsoft 365 läuft, gibt es eine Browser-Anmeldung
mit eigenem Profil. Deine Browser-Cookies werden dabei nicht ausgelesen. Voraussetzung ist ein
installiertes Microsoft Edge oder Google Chrome:

```bash
zeit login btp --url "https://…launchpad.cfapps.eu20.hana.ondemand.com/site?siteId=…"
```

Es öffnet sich ein Browserfenster. Melde dich an, wähle „Angemeldet bleiben“ und öffne die App
„Meine Zeiterfassung“. `zeit` erkennt dabei die Service-URL und schließt das Fenster.

Standardmäßig bleibt es bei Kerberos. Das zweite System wählst du pro Aufruf mit `-s`:

```bash
zeit -s btp show           # oder: ZEIT_SYSTEM=btp zeit show
zeit -s btp add heute 9-10 csop CSIRT-71
```

Soll `btp` der Standard sein, setzt `zeit login btp --standard` das in der Config (`"system": "btp"`).
`zeit -s kerberos …` nutzt dann trotzdem Kerberos. Ist die Microsoft-Anmeldung abgelaufen, meldet
dich `zeit login btp` neu an (die URL ist dann schon gespeichert).

Das Profil unter `~/.local/share/sap-zeit/browser/btp` hält die Microsoft-Anmeldung und ist damit
so schützenswert wie ein Passwort. Abmelden heißt: diesen Ordner löschen. Jeder Aufruf startet den
Browser kurz im Hintergrund und dauert deshalb einige Sekunden länger als mit Kerberos.

Die Konfiguration liegt in `~/.config/sap-zeit/config.json`. Dort stehen die Aliase und optional
`pernr`, `service_url`, `portal_url`, `sap_client`, `default_awart` und `default_bemot`. Unter `systems`
stehen weitere Systeme, die jeden dieser Werte überschreiben können, dazu `auth` (`kerberos` oder
`browser`), `login_url` und `browser` (`msedge`, `chrome` oder `chromium`).

## Technische Hinweise

- Das Portal lehnt Nicht-Browser-User-Agents ab und bietet nur den TLS-Cipher `AES128-GCM-SHA256` an.
  Das Gateway-Zertifikat stammt von einer internen CA. Deshalb nutzt die CLI den macOS-Trust-Store
  (`truststore`) und erlaubt diesen Cipher zusätzlich.
- Schreibzugriffe gehen als `$batch`-POST auf `TimeEntries` mit `TimeEntryOperation` C/U/D.
  Das Gateway erlaubt nur eine Operation pro Changeset.
- Die Leistungsart (LSTAR) leitet SAP selbst ab, im Moment `8990`.

## Weitere Dokumentation

- [docs/API.md](docs/API.md): SAP-Schnittstelle (Anmeldung, OData-Service, `$batch`-Format, Felder, Fehlerbilder)
- [docs/FUNKTIONSWEISE.md](docs/FUNKTIONSWEISE.md): Aufbau und Arbeitsweise der CLI, Befehle, Einschränkungen
