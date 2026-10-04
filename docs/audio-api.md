# Audio API

The gateway exposes transcription and synthesis at `/v1/audio/transcriptions` and `/v1/audio/speech`. Authenticate both with `Authorization: Bearer $AI_API_KEY`.

## Text to speech

Synthesis always uses English. The supported CustomVoice speaker IDs are `Aiden`, `Ryan`, `Vivian`, `Serena`, `Uncle_Fu`, `Dylan`, `Eric`, `Ono_Anna`, and `Sohee`.

```sh
curl http://127.0.0.1:8090/v1/audio/speech \
  -H "Authorization: Bearer $AI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"local-tts","input":"Hello there.","voice":"Vivian","language":"English","instruct":"Speak warmly, with a brief pause before the last sentence.","speed":1.0,"response_format":"mp3"}' \
  --output speech.mp3
```

`instruct` is optional free-form delivery guidance for emotion, tone, pacing, and prosody. The model may interpret specific requests such as laughter, sarcasm, and exact pause timing differently. The gateway rejects non-English TTS language values.

Supported request fields: `model`, `input`, `voice`, `language`, `instruct`, `speed`, and `response_format`. Supported formats are `mp3`, `wav`, `opus`, `flac`, and `pcm`. Speed must be between 0.25 and 4.0.

## Speech to text

Transcription accepts multipart audio uploads. Language is optional; without it, Whisper detects the language. Transcription itself does not generate speech, so this endpoint retains Whisper's language-detection and language-selection behavior.

```sh
curl http://127.0.0.1:8090/v1/audio/transcriptions \
  -H "Authorization: Bearer $AI_API_KEY" \
  -F model=local-stt \
  -F language=en \
  -F response_format=json \
  -F file=@recording.wav
```

Supported fields are `file` (up to 20 MiB), `model`, optional Whisper `language`, `prompt`, `temperature` (0 to 1), `timestamp_granularities[]` (`segment` and/or `word`), and `response_format` (`json`, `verbose_json`, `text`, `srt`, or `vtt`). JSON responses include the detected language, its probability, and segment start/end times. Request word granularity to include word-level start/end times in JSON. SRT and VTT contain segment timestamps.

## MCP tools

- `local_ai_synthesize_speech` accepts text, one of the supported speaker IDs, `instruct`, `speed`, and `response_format`. It always requests English speech and returns `audio_base64` plus `mime_type`.
- `local_ai_transcribe_audio` accepts base64 audio, filename, MIME type, optional Whisper language, prompt, temperature, timestamp granularity, and any supported response format. It returns the transcription result.

The `/v1/capabilities` response describes these supported fields for API clients and MCP discovery exposes tool schemas.
