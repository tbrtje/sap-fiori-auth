# zeit – SAP-Zeiterfassung per CLI

CLI für die Fiori-App „Meine Zeiterfassung“ (CATS, OData-Service `HCM_TIMESHEET_MAN_SRV`).
Die Anmeldung läuft per Kerberos: SPNEGO am Portal liefert das SSO-Cookie `MYSAPSSO2`,
das auch das Fiori-Gateway akzeptiert. Die CLI speichert keine Passwörter und keine Cookies.

## Installation

```bash
uv tool install --editable .     # stellt den Befehl `zeit` bereit
# oder ohne Installation: .venv/bin/python zeit.py ...
```

Voraussetzung ist ein gültiges Kerberos-Ticket (`klist`, bei Bedarf `kinit`).

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

Die Konfiguration liegt in `~/.config/sap-zeit/config.json`. Dort stehen die Aliase und optional
`pernr`, `service_url`, `portal_url`, `sap_client`, `default_awart` und `default_bemot`.

## Technische Hinweise

- Das Portal lehnt Nicht-Browser-User-Agents ab und bietet nur den TLS-Cipher `AES128-GCM-SHA256` an.
  Das Gateway-Zertifikat stammt von einer internen CA. Deshalb nutzt die CLI den macOS-Trust-Store
  (`truststore`) und erlaubt diesen Cipher zusätzlich.
- Schreibzugriffe gehen als `$batch`-POST auf `TimeEntries` mit `TimeEntryOperation` C/U/D.
  Das Gateway erlaubt nur eine Operation pro Changeset.
- Die Leistungsart (LSTAR) leitet SAP selbst ab, im Moment `8990`.
