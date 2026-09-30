# Danish three-speaker STT benchmark

This benchmark compares the three Danish ASR candidates against the same private recordings:

- `edda` -> `danish-foundation-models/edda-v0.1`
- `saga2` -> `capacit-ai/saga-2-m`
- `hviske` -> `syvai/hviske-v6`
- diarization -> `nvidia/Nemotron-3-Diarization`

It runs inside the existing `stt` GPU worker so the supervisor lease remains the single owner of GPU exclusivity.

## Dataset layout

Put private recordings under `data/stt-benchmark/`; that directory is already ignored by git. Start from `stt/benchmark/manifest.example.jsonl`.

Each JSONL row describes one recording. `audio` is relative to the manifest and every recording must have reference text, either in top-level `text` or by concatenating non-empty segment texts. For the full benchmark, provide timestamped `segments` with `start`, `end`, `speaker`, and verbatim `text`. The three real speaker names may be arbitrary; predicted Nemotron speaker IDs are permutation-matched during scoring.

If only top-level `text` is supplied, ASR WER/CER still works, but DER and speaker-attributed WER are unavailable. Extra speakers predicted by Nemotron are preserved and penalized by DER rather than silently dropped.

## Metrics

- **Content WER**: leaderboard-style Danish normalization (NFKC, number word/digit canonicalization, lowercase, punctuation/symbol normalization) with common Danish hesitation fillers removed.
- **Verbatim WER**: the same text/number normalization but fillers retained.
- **CER**: normalized character error rate.
- **DER**: 10 ms speaker frames, overlap included, optimal speaker permutation, 250 ms reference-boundary collar by default.
- **Speaker-attributed WER**: Nemotron turns are cut from the recording, transcribed, mapped to the reference speakers, and scored per speaker. This is deliberately separate from normal WER because short turn segmentation can hurt ASR quality.
- **RTF**: processing seconds / audio seconds. Accuracy is the default ranking criterion.

Results are written as `results.json`, `summary.csv`, and `summary.md`. Before loading each Hugging Face model, the benchmark resolves its requested ref to an immutable Hub commit SHA and stores that SHA in `results.json`. This makes the exact model weights and custom repository code auditable and replayable.

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

The wrapper acquires an **exclusive** supervisor lease for `stt` with the `benchmark` workload profile, runs the benchmark in the existing worker, and releases the lease in `finally`. If ordinary STT work is active, the benchmark waits for it to finish. Once the exclusive request is queued, new ordinary STT leases do not jump ahead; while the benchmark owns the lease, no live transcription can enter the worker. It does not directly stop an active request.

Useful options:

```powershell
.\scripts\benchmark-stt.ps1 -Models edda,hviske -BatchSize 1
.\scripts\benchmark-stt.ps1 -NoSpeakerAttributed
.\scripts\benchmark-stt.ps1 -CollarSeconds 0

# Re-run with exact Hub revisions recorded by an earlier results.json:
.\scripts\benchmark-stt.ps1 -Revision @(
  "edda=<40-char-sha>",
  "saga2=<40-char-sha>",
  "hviske=<40-char-sha>",
  "nemotron=<40-char-sha>"
)
```

For an RTX 3060 12 GB, start with batch size 2; retry with 1 if a custom model runs out of VRAM.

## Public dataset preparation

Public suites use schema-v2 manifest rows and retain dataset class, source hashes, transcript hashes, license, source URL, and reference transformation metadata. The existing runner continues to accept private schema-v1 manifests.

SamtaleBank Sam3 is the primary real three-speaker suite. Prepare it from locally downloaded and extracted files:

    # First register/log in to TalkBank and accept the applicable SamtaleBank ground rules.
    # Place the transcript and linked media files under data\stt-benchmark\raw\samtalebank-sam3.
    .\scripts\prepare-stt-benchmark.ps1 -Suite samtalebank-sam3 -SourcePath data\stt-benchmark\raw\samtalebank-sam3

Each .cha transcript must have its matching audio/video file named by @Media. Video conversion requires ffmpeg on PATH; extraction keeps the complete media timeline and writes mono 16 kHz PCM16 WAV without loudness normalization. The preparer never logs in to TalkBank or bypasses its access rules. If local Sam3 files are missing, it prints the steps needed to obtain them.

CHAT/CA speaker tiers, hidden millisecond time bullets, continuation lines, and overlap are retained as timed reference segments. raw_text is preserved beside deterministic talkbank-ca-v1 scoring text. Windows are selected in source order, do not overlap, target 90 seconds (60–120 seconds allowed), and require all three speakers with at least three seconds each. The preparer writes manifest.jsonl, dataset-validation.json, and the ignored local data\stt-benchmark\dataset-lock.json with SHA-256 hashes and the preparation revision.

The controlled synthetic K=3 suite uses every row with `num_speakers == 3` from the dataset's `test` split. Install the benchmark requirements and prepare it with:

```powershell
pip install -r stt\requirements-benchmark.txt
.\scripts\prepare-stt-benchmark.ps1 -Suite diarization-k3
```

The first run resolves the dataset's current Hub ref to a full commit SHA and stores it in `dataset-lock.json`; reruns reuse that pinned SHA. To intentionally upgrade, use a new lock file or clear the existing suite entry after reviewing the dataset revision. `HF_TOKEN` is consumed by the Hugging Face libraries for gated access and is never printed. This suite is marked `CONTROLLED-SYNTHETIC`: use it for diarization and overlap diagnostics, not as decisive ASR WER evidence, because its source audio pool overlaps candidate training data.

Single-speaker held-out suites are prepared independently so optional data is downloaded only when requested:

```powershell
# CoRal requires logging in to Hugging Face and accepting its gated dataset terms first.
.\scripts\prepare-stt-benchmark.ps1 -Suite coral-conversation-test
.\scripts\prepare-stt-benchmark.ps1 -Suite nst-da-test
.\scripts\prepare-stt-benchmark.ps1 -Suite fleurs-da-dk-test
```

CoRal reads the `conversational` config's `test` split; NST and FLEURS read their official `test` splits, with FLEURS using `da_dk`. Each manifest retains raw reference text, available speaker/dialect/age/gender metadata, audio duration, and an immutable dataset SHA. `strata-summary.json` groups available dialect, accent, age, gender, and duration categories; groups below 1,000 reference words are marked `insufficient_n`. CoRal uses the Hugging Face card's `openrail` license label, NST is CC0, and FLEURS is CC-BY-4.0.

The DanPASS sound archives are password-protected. Until the corpus password is configured, record the access state and continue other suites:

```powershell
.\scripts\prepare-stt-benchmark.ps1 -Suite danpass-dialogue
```

This writes `dataset-status.json` with `PENDING_ACCESS`, the corpus contact, and the official non-commercial attribution terms. Request the password from the listed contact, then download stereo dialogue audio, separate speaker channels, and TextGrids through the official DanPASS page. The command does not contact the corpus or request/store credentials.

Dataset interpretation remains separated by evidence class: Sam3 and DanPASS are PRIMARY-INDEPENDENTISH; CoRal/NST/FLEURS are held-out in-domain suites; the synthetic diarization dataset is CONTROLLED-SYNTHETIC. No overall score averages these classes together. Sam3 has no declared candidate fine-tuning overlap, though base-model pretraining overlap cannot be ruled out.
