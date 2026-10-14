# Synthesise each narration line with Windows SAPI (System.Speech). Input: JSON list of {id, say}.
#   pwsh -File tts.ps1 -Lines ..\audio\lines.json -OutDir ..\audio -Voice "Microsoft Zira Desktop" -Rate 0
param(
  [Parameter(Mandatory = $true)][string]$Lines,
  [Parameter(Mandatory = $true)][string]$OutDir,
  [string]$Voice = "Microsoft Zira Desktop",
  [int]$Rate = 0
)
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 $Lines | ConvertFrom-Json
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($it in $items) {
  $out = Join-Path $OutDir ($it.id + ".wav")
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  try { $s.SelectVoice($Voice) } catch { Write-Host "voice $Voice not found, using default" }
  $s.Rate = $Rate
  $s.SetOutputToWaveFile($out, $fmt)
  $s.Speak([string]$it.say)
  $s.Dispose()
  Write-Host "tts $($it.id)"
}
