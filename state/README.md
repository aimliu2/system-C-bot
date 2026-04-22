# System C State Backups

This folder holds local V2 clean-state templates used to recover the active
portfolio state files if they become corrupted.

Generated templates:

```text
state_live_portfolio_v2.template.json
state_paper_portfolio_v2.template.json
```

Recovery command from the bot repo:

```text
python3 -m runtime.state_store --restore live
python3 -m runtime.state_store --restore paper
```

The active V2 state files remain at the bot repo root because
`bot/config/config.yaml` currently declares:

```text
state_live_portfolio_v2.json
state_paper_portfolio_v2.json
```

