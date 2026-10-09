# Compatibility entrypoint for existing scheduled-task commands.
# The maintained implementation lives under scripts/.
& (Join-Path $PSScriptRoot 'scripts\backup.ps1') @args
