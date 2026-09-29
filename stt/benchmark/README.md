# Danish three-speaker STT benchmark

This benchmark compares the three Danish ASR candidates against the same private recordings:

- `edda` -> `danish-foundation-models/edda-v0.1`
- `saga2` -> `capacit-ai/saga-2-m`
- `hviske` -> `syvai/hviske-v6`
- diarization -> `nvidia/Nemotron-3-Diarization`

It runs inside the existing `stt` GPU worker so the supervisor lease remains the single owner of GPU exclusivity.

## Dataset layout

Put private recordings under `data/stt-benchmark/`; that directory is already ignored by git. Start from `stt/benchmark/manifest.example.jsonl`.

Each JSONL row describes one recording. `audio` is relative to the manifest. For the full benchmark, provide timestamped `segments` with `start`, `end`, `speaker`, and verbatim `text`. The three real speaker names may be arbitrary; predicted Nemotron speaker IDs are permutation-matched during scoring.

If only `text` is supplied, ASR WER/CER still works, but DER and speaker-attributed WER are unavailable.

## Metrics

- **Content WER**: NFKC + lowercase + punctuation/symbol normalization, with common Danish hesitation fillers removed.
- **Verbatim WER**: the same normalization but fillers retained.
- **CER**: normalized character error rate.
- **DER**: 10 ms speaker frames, overlap included, optimal speaker permutation, 250 ms reference-boundary collar by default.
- **Speaker-attributed WER**: Nemotron turns are cut from the recording, transcribed, mapped to the reference speakers, and scored per speaker. This is deliberately separate from normal WER because short turn segmentation can hurt ASR quality.
- **RTF**: processing seconds / audio seconds. Accuracy is the default ranking criterion.

Results are written as `results.json`, `summary.csv`, and `summary.md`.

## Local setup

Saga-2-M is gated on Hugging Face. Accept its repository terms first and expose a read token to the container as `HF_TOKEN`; never commit the token.

Rebuild/recreate the STT worker after changing benchmark dependencies:

```powershell
.\scripts\ai.ps1 create
```

Then create `data\stt-benchmark\manifest.jsonl` and run:

```powershell
.\scripts\benchmark-stt.ps1
```

The wrapper acquires a supervisor lease for `stt`, runs the benchmark in the existing worker, and releases the lease in `finally`. It does not directly stop another active GPU job.

Useful options:

```powershell
.\scripts\benchmark-stt.ps1 -Models edda,hviske -BatchSize 1
.\scripts\benchmark-stt.ps1 -NoSpeakerAttributed
.\scripts\benchmark-stt.ps1 -CollarSeconds 0
```

For an RTX 3060 12 GB, start with batch size 2; retry with 1 if a custom model runs out of VRAM.
