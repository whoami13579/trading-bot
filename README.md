```bash
uv run hma.py
```

```bash
uv run hma.py --real --direction SELL --days 7
uv run hma.py --real --hma_period 16 --time_frame MINUTE_5 --direction SELL
uv run hma.py --real --hma_period 16 --time_frame MINUTE_5 --direction SELL --symbol AUDUSD
```

```bash
uv run multiple_hma.py --real --days 7
```

```bash
uv run hma.py --days 7 --size 0.001 --symbol ETHUSD
```

```bash
uv run multiple_hma.py --days 7 --size 0.002 --symbol ETHUSD
```