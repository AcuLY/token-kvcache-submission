param(
    [string]$Model = 'Qwen/Qwen2.5-1.5B-Instruct',
    [int]$Port = 18080
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv/gpu/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "GPU environment missing: $python"
}
$env:HF_MODEL_ID = $Model
$env:HF_DTYPE = 'float32'
$env:HF_DEVICE = 'cuda'
$env:HF_LOCAL_FILES_ONLY = '1'
$env:HF_HUB_OFFLINE = '1'
$env:HF_PROMPT_FORMAT = 'chat_template'
$env:HF_MAX_CONTEXT_TOKENS = '8192'
$env:HF_TURN_TEMPLATE = '现在轮到 {agent_id} 发言（第 {turn_id} 轮）。请按你的角色要求回应任务和上一轮内容，只输出你自己的回复，不写发言标记，不代替其他角色发言。'
& $python -u (Join-Path $PSScriptRoot 'run_local.py') --model-backend huggingface --port $Port
exit $LASTEXITCODE
