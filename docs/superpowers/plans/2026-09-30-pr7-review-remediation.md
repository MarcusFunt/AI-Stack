# PR #7 Review Remediation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PR #7 results, statistical comparisons, and provenance safe to interpret, then validate the public FLEURS path end to end.

**Architecture:** Keep the existing benchmark runner and report pipeline. Add explicit run status and a hashed evaluation protocol to runner results, carry model-by-dataset evidence and generic bootstrap groups through manifests and analysis, and make provenance content-addressed. Keep Sam3 diagnostics while clearly naming its chronological single-stream WER semantics.

**Tech Stack:** Python 3.11, `unittest`, Hugging Face `datasets`, existing PowerShell supervisor/GPU wrappers.

**Spec:** `D:\AI-Stack\.codex-remote-attachments\01a0f10b-137a-7290-8826-38f7635710cc\cf6cda9f-50c1-4fb9-8e4c-941ea414a7c3\1-PR7_REVIEW_REQUIRED_CHANGES.md`

## Global Constraints

- Keep the gateway/supervisor ownership and exclusive STT GPU lease contract intact.
- Never read, print, or modify `.env` or `.env.*`; let existing authenticated APIs consume configured credentials.
- Do not commit corpus media, gated data, model weights, or local benchmark results.
- Preserve schema-v1 manifest loading and PowerShell 5.1 compatibility.
- Never rank failed inference as a scored prediction or use non-decisive contamination evidence to select a model.

## Review Focus

- Model load/transcription exceptions with successful and failed aliases in the same run must preserve valid scores and exclude only failed candidates.
- Legacy result files without protocol/status fields must not be silently merged with new protocol results.
- Repeated clips from one speaker must be sampled as one bootstrap cluster; missing group IDs must fall back to independent recording units.
- A FLEURS score with checkpoint-selection overlap must remain visible but non-decisive for Saga-2-M.
- Dirty tracked or untracked benchmark code must change recorded preparation-code provenance deterministically.

---

### Task 1: Failed model execution semantics

**Files:** Modify `stt/benchmark/runner.py`; create `stt/benchmark/tests/test_failed_model_results.py`; update runner summary assertions.

**Interfaces:** Model rows expose `status` (`success` or `failed`) and structured `failure`; failed aggregate metrics are `null`; `ranking_by_content_wer` contains successful aliases only. Partial results are written before `main()` returns non-zero for a failed requested alias.

- [x] **Step 1: Write the failing tests**
  - Inject an OOM from adapter loading and assert `status == "failed"`, failure class/message are retained, all aggregate metrics are null, and no alias enters ranking or pairwise comparisons.
  - Inject one failed and one successful alias; assert successful metrics remain scored and the summary lists the failure separately.
  - Assert the CLI writes `results.json` and exits non-zero when any requested alias fails.
- [x] **Step 2: Run the focused tests and confirm they fail for missing failure semantics.**
- [x] **Step 3: Refactor runner model execution into a testable per-alias helper; retain empty hypotheses only as failure diagnostics and skip all metric aggregation after execution failure.**
- [x] **Step 4: Filter failed aliases from ranking, bootstrap comparisons, and recommendations; include a failed-candidate section in runner/report output.**
- [x] **Step 5: Run focused tests, then all benchmark tests.**

### Task 2: Evaluation protocol and merge compatibility

**Files:** Create `stt/benchmark/evaluation_protocol.py`; modify `runner.py`, `analysis/report.py`, and `stt/benchmark/tests/test_runner_result_support.py` / report tests.

**Interfaces:** `evaluation_protocol(collar_s: float, reference_semantics: str) -> dict` returns canonical fields; `canonical_json_sha256(value: Any) -> str` returns stable lowercase SHA-256. New results use schema version 3 and include the protocol object/hash. `_merge_runs()` rejects same-dataset runs with missing or unequal protocol hashes.

- [x] **Step 1: Test deterministic canonical hashes, same-protocol merge, and changed/missing protocol rejection.**
- [x] **Step 2: Run focused tests and confirm hash/protocol behavior is missing.**
- [x] **Step 3: Add the protocol object and fingerprint to each runner result; include normalization IDs, DER frame/collars, turn-merge gap, and metric schema version.**
- [x] **Step 4: Include the protocol hash in the dataset merge signature and emit the protocol in report provenance.**
- [x] **Step 5: Run focused tests and the benchmark suite.**

### Task 3: Model-by-dataset evidence and neutral recommendations

**Files:** Modify `analysis/report.py`, `analysis/comparison.py`, report tests, and benchmark README.

**Interfaces:** Replace family-only training notes with `MODEL_DATASET_EVIDENCE[alias][dataset]` records containing `status`, `decisive`, `source`, `source_url`, `note`, and the resolved `model_revision`. Pairwise/dataset summaries retain scores and evidence. Recommendations consider only successful, decisive candidates; raw speaker-attributed WER is descriptive and never a tiebreak.

- [x] **Step 1: Test Saga-2-M × FLEURS as visible but `checkpoint_selection_overlap` and non-decisive; test failed/non-decisive candidates cannot win and SA-WER cannot break a content-WER tie.**
- [x] **Step 2: Confirm those tests fail against the current report.**
- [x] **Step 3: Add revision-linked model-by-dataset evidence for Edda, Saga-2-M, and Hviske, including unknown/default relationships and the documented FLEURS checkpoint-selection caveat.**
- [x] **Step 4: Add evidence status to `dataset-summary.csv` and `provenance.json`; make recommendation text neutral when no decisive difference/candidate exists.**
- [x] **Step 5: Run focused report tests and the benchmark suite.**

### Task 4: Speaker/conversation clustered bootstrap

**Files:** Modify `datasets/speech_recognition.py`, `datasets/samtalebank.py`, `analysis/bootstrap.py`, `analysis/comparison.py`, and their tests.

**Interfaces:** Manifest metadata uses `bootstrap_group` and `bootstrap_group_type`. Sam3 uses its source recording; single-speaker suites use speaker IDs when available. `_paired_units()` supplies both recording/window and group IDs; pairwise rows keep ordinary CI plus clustered CI and identify each sampling/group unit.

- [x] **Step 1: Test repeated speaker clips cluster together, Sam3 windows retain source-recording clusters, and missing IDs fall back to recording/window.**
- [x] **Step 2: Confirm focused tests fail on current source-recording-only grouping.**
- [x] **Step 3: Add group metadata during preparation and make the bootstrap sample whole clusters with replacement.**
- [x] **Step 4: Preserve both intervals in CSV/report and use the clustered interval for decisions whenever present.**
- [x] **Step 5: Run focused bootstrap/preparation tests and the full benchmark suite.**

### Task 5: Self-contained provenance and dataset interpretation

**Files:** Modify `datasets/provenance.py`, dataset preparers, `runner.py`, `analysis/report.py`, `analysis/strata.py`, `datasets/speech_recognition.py`, Sam3/report tests, and README.

**Interfaces:** Result dataset metadata contains the dataset-lock SHA-256 and exact matching lock entry. Analysis input provenance stores logical name, source path, and SHA-256. Preparation locks record `preparation_code: {git_sha, dirty, working_tree_diff_sha256}`. Generic metadata strata produce per-model WER rows with `status=insufficient_n` below 1,000 reference words.

- [x] **Step 1: Test lock-entry/hash capture, input-file hash changes, deterministic dirty-code fingerprints including untracked benchmark files, and generic per-model strata thresholding.**
- [x] **Step 2: Confirm focused tests fail for missing content-addressed provenance and generic strata.**
- [x] **Step 3: Add canonical JSON/file/worktree hashing and embed lock/protocol/model-evidence provenance.**
- [x] **Step 4: Add generic single-speaker strata; label NST `utterance-held-out` and `speaker-overlapping`; label Sam3 content WER `chronological_single_stream`.**
- [x] **Step 5: Run focused provenance/strata tests and the full suite.**

### Task 6: Public FLEURS end-to-end validation and PR update

**Files:** Use `scripts/prepare-stt-benchmark.ps1`, `scripts/benchmark-stt.ps1`, `scripts/analyze-stt-benchmark.ps1`; do not commit generated data/results.

**Interfaces:** Prepare the pinned FLEURS `da_dk` test manifest, run Edda/Saga-2-M/Hviske through the existing exclusive supervisor lease, analyze successful/failed results, and replay at least one candidate using stored model/dataset SHAs. Preserve raw results under ignored `data/stt-benchmark/`.

- [x] **Step 1: Verify GPU/supervisor status and confirm no active lease before requesting a benchmark run.**
- [x] **Step 2: Prepare and validate FLEURS; verify pinned SHA, audio format, transcript mapping, and lock/provenance hashes.**
- [x] **Step 3: Run all available candidates sequentially through the existing wrapper; retain partial results and failure status if gated access blocks a model.**
- [x] **Step 4: Generate and inspect the combined report; replay one candidate with exact stored revisions and compare references/config/metrics.**
- [x] **Step 5: Run full local verification, push the follow-up commit(s), and confirm PR #7 CI is green. Do not merge.**

---

## Self-review

- Coverage: P0-1 through P0-3, P1-1 through P1-3, P2-1 through P2-3, and the required FLEURS validation each map to Tasks 1–6.
- Dependencies: result status and protocol are established before comparison/report changes; group metadata is added before grouped analysis; provenance and interpretation are validated before the FLEURS run.
- Review focus: failed aliases, legacy protocol omissions, repeated speaker clips, contamination evidence, and dirty code state each have a named regression test.
- Scope: the plan keeps the existing runner and supervisor design, and does not add the deferred overlap-aware cpWER/ORC metric or GUI.
