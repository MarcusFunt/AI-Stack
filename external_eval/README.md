# Inspect AI smoke evaluation

This is a one-sample offline evaluation that calls AI-Stack's OpenAI-compatible chat API and uses Inspect's built-in `includes` scorer. It does not add dependencies to any AI-Stack service image.

Install Inspect in an isolated Python environment, make the existing gateway API key available to Inspect as `AI_STACK_API_KEY`, then run. The PowerShell assignment below reuses `AI_API_KEY` only if that credential is already present in the caller's environment; otherwise set `AI_STACK_API_KEY` through the environment provider used on that machine.

```powershell
python -m venv external_eval\venv
.\external_eval\venv\Scripts\Activate.ps1
python -m pip install -r external_eval\requirements.txt
$env:AI_STACK_API_KEY = $env:AI_API_KEY
inspect eval external_eval\ai_stack_smoke.py `
  --model openai-api/ai-stack/local-fast `
  --model-base-url http://127.0.0.1:8090/v1 `
  --limit 1
```

The model alias, API base URL, and provider-specific API-key environment variable can also be changed through Inspect's normal CLI options and provider conventions. The evaluation makes one ordinary chat request, so the supervisor may load the local-fast GPU worker if no other worker owns the GPU.
