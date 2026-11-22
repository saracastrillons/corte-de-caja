# Volume & performance

Everything here was measured on a laptop (Windows 11, Docker Desktop, PostgreSQL 18)
with the reports committed in [`reports/`](../reports).

## The volume scenario

| | |
|---|---|
| Store data (real) | Olist: 99,441 orders, 112,650 order items |
| Gateway (simulated from Olist payments, seed 42) | 105,081 payment events (+ resent copies), 1,668 daily settlements |
| Bank (simulated) | 1,685 deposits in 618 daily statements |
| Rows stored in `raw` | 320,891 |
| Days | 642 (Sep 2016 – Oct 2018) |

Reproduce it:

```bash
python -m cortecaja.simulate     # writes data/generated/olist/ + truth.csv
python -m cortecaja.volume       # loads everything into its own database and closes the period
python -m cortecaja.explain --label after