# Auftragsverlauf und dauerhafte Diagnose

Im System-Tab steht der Auftragsverlauf. Jeder Auftrag erhält eine Kennung, die im Neustart-Checkpoint erhalten bleibt. Aufgezeichnet werden Planung, Startanforderung, Gerätebestätigung, wechselnde Wartegründe, Gerätephasen, Raum- und Fleckenfortschritt sowie Abschluss oder Abbruch. Fortschritt wird in 5-Prozent-Schritten protokolliert. Planung angefordert und Planung übernommen sind getrennte Ereignisse. Neue Ereignisse sind nach Neuladen der Einstellungen sichtbar.

Auf dem Runtipi-Host liegen die Daten unter `/home/simon/ghostvacuum-observer/storage/`:

- `events.jsonl`: Auftragsereignisse des Automationsdienstes
- `dashboard/events.jsonl`: Planungsereignisse des Dashboards
- `dashboard/request_ids.json`: Verknüpfung vorgemerkter Aufträge
- `dashboard/dashboard.log`: Dienststart, Backendfehler und fehlgeschlagene HTTP-Anfragen
- `logs/appdaemon.log`, `logs/error.log`, `logs/access.log`, `logs/diag.log`: AppDaemon-Diagnose

Ereignisdateien rotieren bei 2 MiB mit 10 Sicherungen pro Datei. Textlogs rotieren bei 5 MiB mit 5 Sicherungen pro Datei. Der System-Tab zeigt maximal die letzten 20 Aufträge mit jeweils 100 Ereignissen. Ältere Einträge können noch in den rotierten Dateien liegen. Die Speicherung überlebt Updates und Containerneustarts, ist jedoch keine unbegrenzte Archivierung. Zur Sicherung außerhalb dieses Hosts das gesamte `storage`-Verzeichnis sichern.

Ereignisse werden mit Dateisperre und `fsync` geschrieben. Zugangsdaten und genaue Personenstandorte werden nicht in den Ereignisverlauf aufgenommen. Ein Schreibfehler wird als Warnung ausgegeben und verändert keine Roboterentscheidung. Bereits abgeschlossene Aufträge vor Einführung des Journals werden nicht nachträglich rekonstruiert.

`appdaemon.yaml` enthält die einzufügende Logkonfiguration. Das vorhandene `/conf`-Volume bleibt bestehen. Das Dashboard schreibt in sein bereits beschreibbares `storage/dashboard`-Volume. Verzeichnis `storage/logs` auf dem Host mit Modus 700 und Eigentümer des Containerprozesses anlegen.

Der Update-Prozess nimmt beide Journalmodule ins geprüfte Paket auf, sichert die Daten vor Installation und ersetzt beim Rollback ausschließlich Programmdateien. Die Gesundheitsprüfung berücksichtigt auch AppDaemon-Dateilogs ab dem aktuellen Containerstart. Konfiguration und laufende Journaldateien werden bei einem Codeupdate nicht überschrieben.
