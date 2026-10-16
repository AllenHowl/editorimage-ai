# pedit

Editor di immagini a livelli guidabile da UI web, assistente in chat e server MCP — stesso
motore per tutti e tre.

**Solo per usarlo: `claude mcp add pedit -- uvx pedit-mcp`** — pacchetto PyPI `pedit-mcp`,
interfaccia web inclusa, niente clone.

**Per modificarlo, partenza da zero: `python scripts/setup.py`** (aggiungi `--json` se ti serve
l'esito in forma leggibile da programma). Installa dipendenze, compila l'interfaccia, registra
il server MCP e verifica con `pedit doctor` e i test.

Le istruzioni complete — verifica, mappa del codice, regole per modificarlo, uso da MCP — sono
in @AGENTS.md e valgono per qualunque agente.
