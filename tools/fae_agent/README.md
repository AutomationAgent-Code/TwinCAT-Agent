# Beckhoff FAE local retriever

```powershell
$env:PYTHONPATH="$PWD\tools\fae_agent"
python -m fae.ask index --rebuild --prune
python -m fae.ask stats
python -m fae.ask ask "EtherCAT 从站掉线怎么排查"
```

The corpus defaults to `G:\Codex\Beckhoff-Virtual-Academy` and the generated
SQLite FTS5 index is stored in `tools/fae_agent/data/fae.sqlite3`.
