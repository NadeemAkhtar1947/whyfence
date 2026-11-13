# Records docs/demo.gif, the README's demo. Run from the repository root:
#
#   vhs docs/demo.tape
#
# Needs vhs, ffmpeg and ttyd on PATH, Git Bash at its default location, and
# Python with uv. The demo repositories are built off-camera by
# `demo.sh --setup-only`, so the recording only shows whyfence itself.

Output docs/demo.gif

Set Shell "powershell"
Set FontSize 18
Set Width 1240
Set Height 600
Set Padding 24
Set TypingSpeed 45ms
Set Theme "Catppuccin Mocha"

Hide
Type `$wf = (Get-Location).Path; $d = "$env:TEMP/whyfence-gif"` Enter
Type `& 'C:\Program Files\Git\bin\bash.exe' ./demo.sh --setup-only $d` Enter
Type `[Console]::OutputEncoding = [Text.Encoding]::UTF8; $env:PYTHONIOENCODING = 'utf-8'` Enter
Type `function whyfence { python -m uv run --quiet --project $wf whyfence @args }` Enter
Type `function prompt { "$ " }; cd "$d/stale-repo"; Clear-Host` Enter
Sleep 8s
Show

Type "# 1. Agents follow CLAUDE.md literally. Are its commands still real?" Enter
Sleep 600ms
Type "whyfence lint" Sleep 300ms Enter
Sleep 4s

Hide
Type `cd ../demo-app; Clear-Host` Enter
Sleep 1s
Show

Type "# 2. How did refresh() get here? It moved files along the way." Enter
Sleep 600ms
Type "whyfence history refresh" Sleep 300ms Enter
Sleep 4s

Enter
Type "# 3. Why is it this way? What an agent sees before touching it:" Enter
Sleep 600ms
Type "whyfence why refresh" Sleep 300ms Enter
Sleep 7s
