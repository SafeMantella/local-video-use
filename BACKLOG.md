# Backlog — helpers a construir

Ideias de helper que economizam trabalho manual da LLM no pipeline de edição. Construir quando estiver editando um vídeo de verdade (testar contra dado real). Contexto completo: SOP `sistema-social-media-pedro` no projeto social-media do Pedro.

## `helpers/verify_transcript.py` — double-check de completude da transcrição

Passo 2 do pipeline: depois de `transcribe.py`, confirmar que nada denso no waveform ficou de fora antes de corrigir à mão.

Dado `<edit>/transcripts/master.json` + o áudio da source:
1. `ffmpeg silencedetect` global → janelas de silêncio.
2. Split por gap interno entre palavras adjacentes no JSON word-level.
3. Pra cada janela suspeita (fala densa sem palavra correspondente, ou gap grande no meio de frase), re-transcrever **só aquela janela** isolada (whisper.cpp roda mais confiável perto de pausa).
4. Relatório: `[start, end]` suspeitos + texto re-transcrito de cada. Não auto-patchar o master.json.

Casos reais que motivaram (vídeo "explicando contexto em LLMs"): DTW pulou frase de ~5s; capou palavra antes de pausa longa em exatos 2.00s; erra início de palavra 1-5s dentro do silêncio pós-retake.

## `helpers/cadence_audit.py` — auditoria de cadência (benchmark Hormozi)

Passos 6 e 10 + benchmark Alex Hormozi: estímulo visual novo a cada ≤2-3s (corte, punch-in, B-roll, entrada/saída de gráfico — legenda **não** conta).

Dado `<edit>/edl.json` (`{sources, ranges:[{source,start,end,beat,quote}]}`):
1. Somar durações de cada range → timestamps dos cortes secos na timeline de saída.
2. Reportar: nº de cortes, duração média entre cortes, lista de intervalos > 3s.
3. `--extra-events events.json` opcional com punch-ins / entradas de overlay (não estão no edl.json) pra auditoria completa.

Caveat: sozinho, o edl.json só tem cortes secos. Punch-in (passo 6) e overlays (passo 7) vivem em outros lugares — auditoria pura de edl.json é primeira-passada.

## `helpers/assemble.py` — montagem final por manifesto

Passo 10: "1 comando ffmpeg, camadas por `-itsoffset` em ordem cronológica, legenda por último". Hoje a LLM constrói o comando à mão a partir dos metadados dos slots toda vez.

1. `assemble.py <manifest.json>` — manifesto = `base.mp4` + camadas `{file, offset_s, z}` (renders alpha dos slots + legenda por último). Constrói e roda o ffmpeg em background.
2. Verificação embutida: `ffprobe` no arquivo real (resolução, fps, duração, vídeo+áudio presentes) — nunca confiar no log.
3. `--preview`: versão comprimida (`scale=960:-2`, crf 24) + contact sheet, ou reusar `timeline_view.py`.
