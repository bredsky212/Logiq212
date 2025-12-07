# Developer Setup & Best Practices (Windows + PowerShell + VS Code + Codex)

This repo has a few gotchas when working on Windows/PowerShell with UTF‑8 content and the Codex CLI. Use this as a quick-start checklist to avoid repeating the same hurdles.

## System & Encoding
- **Always force UTF-8** when printing/processing files in PowerShell. Prefer `py -3 -Xutf8 -c "..."`
  or `Get-Content -Encoding UTF8` / `Set-Content -Encoding UTF8`. Avoid plain `python -c` or default `Get-Content` which defaults to CP1252 and mangles accents/arrow characters.
- When using `sed`/`bash` scripts on Windows, run from WSL or Git-Bash if possible. In PowerShell, keep paths quoted and explicit (`c:\...\visits`).
- If you need to dump file snippets from PowerShell reliably, use:
  ```powershell
  py -3 -Xutf8 -c "from pathlib import Path; print(Path('path/to/file').read_text(encoding='utf-8'))"
  ```

## Git & Branching
- Create feature branches off `develop` (or current working branch) before coding:
  ```powershell
  git checkout develop
  git pull
  git checkout -b feature/my-topic
  ```
- Avoid mixing unrelated changes; small focused commits make review/deploy easier.

## File Editing Tips
- **Avoid CP1252 corruption**: do not use default `Get-Content/Set-Content` without `-Encoding UTF8`.
- Use the Codex `apply_patch` tool for small edits; for larger ones, open in VS Code and ensure the file encoding is UTF-8.
- When viewing PHP or JS snippets from PowerShell, rely on `py -Xutf8` or `Get-Content -Encoding UTF8`.
- Beware CRLF conversions: Git warnings show when LF→CRLF will occur. If needed, set `core.autocrlf=input` in your Git config to keep LF.

## Running Commands
- The Codex CLI runs PowerShell by default. Avoid `&&` chaining (PowerShell doesn’t support it). Use `;` or separate commands:
  ```powershell
  cd path\to\repo; git status -sb
  ```


## Quick Troubleshooting
- **LF/CRLF warnings**: set Git `core.autocrlf=input` or ensure your editor saves with LF.
- **UTF-8 parsing errors**: rerun with `py -3 -Xutf8 ...` or `Get-Content -Encoding UTF8`.

## Discord Bot Pitfalls We Hit (Do These Next Time)
- **Bind slash-command groups from the cog instance**: in each `setup`, create the cog (`cog = MyCog(bot, ...)`), add it, then `bot.tree.add_command(cog.group)`. Adding the class attribute (`MyCog.group`) caused `CommandSignatureMismatch` and “application didn’t respond”.
- **Sync globally**: keep `/sync` as a global `tree.sync()` to refresh definitions; avoid guild-specific sync unless explicitly requested.
- **Defer before DB/IO**: call `await interaction.response.defer(ephemeral=True, thinking=True)` before long DB calls; send results via followups to avoid the 3s timeout.
- **Use discord.py’s timeout API**: on 2.x call `member.timeout(timedelta, reason=...)` and `member.timeout(None, reason=...)`; do not pass the raw field `communication_disabled_until`.
- **Propagate logging handlers**: ensure handlers are attached to the root logger so `logiq.*` and cog loggers show up under systemd.
- **Verify the runtime env**: confirm systemd `ExecStart` points to the venv’s Python and reinstall deps (`pip install -r requirements.txt`) if you see missing kwargs or version mismatches.

Keep this handy when setting up a fresh environment; it captures the main pitfalls we encountered (encoding, PowerShell chaining, dataset management, OCC usage).
