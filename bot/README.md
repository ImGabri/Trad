# bot/ — XAUUSD AI scalper v2 (PUPrime MT5)

See `ARCHITECTURE.md` for the design. Status: data pipeline, features, rules, risk and the backtester are done; AI model, live execution and monitoring come next.

## 1. Confirm your PUPrime symbols (run on the Windows VPS)

1. Open the PUPrime MT5 terminal, log in to the demo, tick *Save password*, enable **Algo Trading**.
2. In `bot/`, copy `.env.example` to `.env` and fill in login, password and server.
3. Run:

```bash
py -3.12 -m pip install -r requirements.txt
```

```bash
py -3.12 tools/probe_symbols.py
```

4. Send back `tools/probe_report.json` (remove the login number if you like). It lists the gold spec, which DXY/10Y/S&P symbols exist, the server offset, the live spread and how much M1 history PUPrime serves.

## 2. Offline tests (any OS)

```bash
python -m pytest -q tests
```
