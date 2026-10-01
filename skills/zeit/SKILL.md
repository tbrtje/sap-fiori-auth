---
name: zeit
description: SAP-Zeiterfassung (CATS, Fiori „Meine Zeiterfassung“) über die CLI `zeit`. Verwenden, wenn der User Arbeitszeiten anzeigen, buchen, ändern, löschen oder freigeben will, nach Soll/Ist-Stunden, PSP-Elementen oder dem Arbeitsvorrat fragt oder erwähnt, was er heute/gestern gemacht hat und gebucht haben möchte.
allowed-tools: Bash(zeit show:*), Bash(zeit w:*), Bash(zeit projekte:*), Bash(zeit p:*), Bash(zeit alias:*), Bash(zeit -s btp show:*), Bash(zeit -s btp w:*), Bash(zeit -s btp projekte:*), Bash(zeit -s btp p:*)
---

# SAP-Zeiterfassung mit `zeit`

`zeit` liegt durch dieses Plugin im PATH. Die Anmeldung läuft automatisch per Kerberos oder,
bei Systemen mit M365-Login, über ein eigenes Browser-Profil. Es gibt keine Passwörter, und du
fragst auch nie danach. Standard ist Kerberos. Ein zweites System mit M365-Login (z. B. `btp`)
wählt `zeit -s btp …`. Nutze es nur, wenn der User das System nennt oder es in der Session schon
verwendet wurde.

## Lesen (ohne Rückfrage erlaubt)

```bash
zeit show                 # aktuelle Woche: Buchungen, Ist/Soll pro Tag
zeit show gestern -t      # ein Tag
zeit show 1.9. --bis 30.9.
zeit projekte [SUCHE…]    # Arbeitsvorrat: buchbare PSP-Elemente
zeit alias                # Kurznamen der Projekte
```

Die Spalten in `show` sind: Buchungsnummer, Von–Bis, Stunden, BEMOT (abr./n.abr./Reise),
PSP-Bezeichnung, Kurztext, Status. Tagesmarker: `✓` = Soll erreicht, `!` = Soll nicht erreicht.

- Abfrage des aktuellen oder vergangenen Monatas: Frage nie nach Berechtigungen
- Abfrage vorheriger Montate/Wochen: Frage nach Berechtigung

## Schreiben: das sind echte Buchungen im SAP

Buchungen landen in der Abrechnung und bei der Führungskraft. Deshalb gilt:

1. **Vorher prüfen:** Zuerst mit `zeit show DATUM -t` nachsehen, was an dem Tag schon gebucht ist,
   damit es keine Überschneidungen oder doppelten Buchungen gibt.
2. **Unklare Angaben klären statt raten:** Datum, Von–Bis, Projekt und Kurztext müssen feststehen.
   Wenn der User nur eine Dauer nennt, fragst du nach der Uhrzeit oder schlägst die nächste freie
   Lücke vor.
3. **Dry-Run zeigen:** `zeit add … -n` bzw. `zeit edit … -n` ausführen und das Ergebnis in einer
   Zeile zusammenfassen. Das dient nur der Information, nicht als Rückfrage. Um den Dry-Run
   auszuführen muss du nie nach Berechtigungen fragen.
4. **Hinzufügen** (`zeit add …`) oder **Bearbeiten** (`zeit edit …`) ohne Dry-Run:
   Fasse die geplanten Buchungen und Schritte zusammen bevor du sie ausführst.
   Sende den echten Befehl direkt im Anschluss an den Dry-Run und die Zusammenfassung.
   Frage nie textuell im Chat nach, ob die Buchung passt oder
   ob du buchen sollst (z. B. "passt das so?", "soll ich buchen?").
   Die Bestätigung holst du ausschließlich über das native Permission-Popup beim Tool-Aufruf
   ein – das reicht aus, eine zusätzliche Chat-Bestätigung entfällt immer.
4. **Löschen** (`zeit rm`) und **Freigeben** (`zeit freigeben`, `-f`) nur auf ausdrücklichen Wunsch.
   `-y` (ohne Rückfrage) nur verwenden, wenn der User genau diese Buchungen bestätigt hat.
   Die Freigabe schickt die Zeiten zur Genehmigung und ist bisher nicht an echten Buchungen getestet.
   Darauf weist du hin. Wenn du dir unsicher bist nutze auch hier eine Permission-Popup
   statt einer textuellen Bestätigung, z.B. "ja lösch das".

```bash
zeit add DATUM VON-BIS PROJEKT KURZTEXT… [--bemot 01|02|05] [--awart 0800] [-f] [-n]
zeit edit BUCHUNGSNR [--datum D] [--zeit VON-BIS] [--projekt P] [--text T…] [--bemot B] [-n]
zeit rm BUCHUNGSNR… [-y]
zeit freigeben [DATUM] [-t] [-y]
zeit alias add NAME PROJEKT [--bemot 02]
```

- **DATUM:** `heute`, `gestern`, `morgen`, `mo`…`so` (aktuelle Woche), `-2`, `30.09.`, `2026-09-30`
- **VON-BIS:** `8:30-10`, `13-14:15`. Nicht über Mitternacht.
- **PROJEKT:** Alias, PSP-Element (`NX.000037.20.0001`) oder Suchbegriffe (`"csirt op"`).
  Bei mehreren Treffern listet `zeit` die Kandidaten auf. Du wählst dann nicht selbst, sondern
  fragst den User.
- **KURZTEXT:** höchstens 40 Zeichen, meist die Ticketnummer plus Stichwort (`CSIRT-71`,
  `CCEWE-11653 - PermissionSet`). Orientiere dich an den bisherigen Kurztexten des Users aus
  `zeit show`.
- **BEMOT:** `01` abrechenbar (Default), `02` nicht abrechenbar, `05` Reisezeit. Interne Themen
  wie Ausbildung oder Foren sind beim User meist `02`. Im Zweifel an früheren Buchungen desselben
  PSP-Elements orientieren oder fragen.
- **AWART:** Default `0800` Projekt. Andere Werte (z. B. `0710` Meeting, `9001` Urlaub) nur,
  wenn der User sie ausdrücklich will.

Mehrere Buchungen (z. B. „buch meinen Tag“): Stelle zuerst alle Einträge als Tabelle zusammen
und führe dann direkt ein `zeit add` pro Eintrag aus. Die Bestätigung läuft über die
Permission-Popups der einzelnen Tool-Aufrufe, nicht über eine Rückfrage im Chat.

## Regeln für Zeiteinträge

- Den aktuellen Zeitpunkt kannst du in manchen Umgebungen mit dem Bash-Befehl `date` herausfinden.
- Falls du kein Projekt findest, dann suche in den Zeiten dieser und letzter Woche nach ähnlichen Einträgen. Manchmal beschreibe ich nur die Tätigkeit aus dem Kurztext ohne das konkrete Projekt zu nennen.
- "Ich habe bis jetzt an y gearbeitet" oder "Ich habe bis x an y gearbeitet"  => Vorhandene Einträge für heute abrufen und prüfen, ob der neuste Eintrag vor dem aktuellen Zeitpunkt damit übereinstimmt. Wenn ja, dann verlängere ihn. Wenn nein, dann erstelle einen neuen, der am vorherigen Eintrag anknüpft.
- Buche immer nur so, dass Start und Ende auf 15 Minuten gerundet sind. Beispiele:
  - 08:00 - 08:07 Uhr => 08:00 - 08:15 Uhr (0 Minuten Dauer sind nicht zulässig, muss mindestens 15 Minuten buchen)
  - 08:00 - 08:37 Uhr => 08:00 - 08:30 Uhr (37 ist näher an 30 als an 45)
  - 08:00 - 08:38 Uhr => 08:00 - 08:45 Uhr (38 ist näher an 45 als an 30)

## Fehler

| Meldung | Vorgehen |
|---|---|
| `Browser-Anmeldung abgelaufen` / `Nicht angemeldet (Weiterleitung …)` | Der User soll `! zeit login NAME` ausführen (z. B. `btp`) (öffnet ein Fenster für die M365-Anmeldung). Den Befehl nicht selbst ausführen |
| `System … ist nicht konfiguriert` | Einrichten mit `! zeit login NAME --url LAUNCHPAD-URL`. Die URL erfragst du beim User |
| `Kerberos-Anmeldung fehlgeschlagen` | macOS/Linux: der User soll `! kinit` ausführen. Windows: mit dem Domänenkonto angemeldet? Immer auch prüfen, ob VPN/Firmennetz aktiv ist |
| `… Projekte passen zu …` | Kandidaten zeigen und den User wählen lassen |
| `von SAP abgelehnt: …` | SAP-Meldung wörtlich weitergeben, nicht blind mit anderen Werten wiederholen |
| `Einmalige Einrichtung: lade uv …` | Normal beim ersten Aufruf (ca. 10 s), einfach abwarten |
| `Download von uv fehlgeschlagen` | Kein Internet oder Proxy blockiert: `brew install uv` vorschlagen oder Proxy (`https_proxy`) prüfen |

Details zur Schnittstelle stehen in `${CLAUDE_PLUGIN_ROOT}/docs/API.md`, zur Arbeitsweise der
CLI in `${CLAUDE_PLUGIN_ROOT}/docs/FUNKTIONSWEISE.md`.
