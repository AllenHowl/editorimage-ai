# Contributing

Issues and pull requests are welcome.

- Read [AGENTS.md](AGENTS.md) first — it is the contract for humans and coding agents alike.
- Code and comments are **in Italian** (project convention); everything a user reads (UI text,
  error messages, tool and effect descriptions) is **in English**.
- Every new behaviour comes with a test. A colour filter needs a test that looks at *which way
  the pixels moved*, not only that it renders.
- New effect → `effects.py`. New MCP tool → a method in `store.py`, a wrapper in
  `mcp_server.py`, a test in `tests/test_mcp.py`. UI change → `cd frontend && npm run build`.
- Before claiming it works: `pytest`, plus `cd frontend && npm test` if you changed the UI.
- No new dependencies without a reason: the core stands on numpy, Pillow, FastAPI and mcp.
