<#
.SYNOPSIS
    봇을 Windows 작업 스케줄러에 등록해 PC 를 켜면 자동으로 돌게 합니다.

.DESCRIPTION
    24시간 운영이 목적이라면 이 등록을 권장합니다. 작업이 죽으면 1분 뒤
    자동으로 다시 시작하도록 설정합니다.

    기본값은 "로그온할 때 시작"입니다. 로그인하지 않아도 돌게 하려면
    -AtStartup 을 주세요. 이 경우 계정 비밀번호를 한 번 입력해야 합니다
    (Windows 가 로그온 없이 작업을 실행하려면 자격 증명이 필요합니다).

.EXAMPLE
    # 관리자 PowerShell 에서
    .\install-startup-task.ps1

.EXAMPLE
    # 로그인하지 않아도 부팅 시 실행
    .\install-startup-task.ps1 -AtStartup

.EXAMPLE
    # 등록 해제
    .\install-startup-task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "IllegalSiteBot",
    [switch]$AtStartup,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "작업을 삭제했습니다: $TaskName"
    } else {
        Write-Host "등록된 작업이 없습니다: $TaskName"
    }
    return
}

# 가상환경이 있으면 그 파이썬을 씁니다.
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "파이썬을 찾을 수 없습니다. 먼저 scripts\windows\setup.bat 을 실행하세요."
    }
    $python = $command.Source
    Write-Warning "가상환경(.venv)이 없어 시스템 파이썬을 사용합니다: $python"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "bot.py start" -WorkingDirectory $root

if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
}

# 죽으면 1분 뒤 재시작, 실행 시간 제한 없음, 이미 돌고 있으면 새로 띄우지 않음
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$description = "불법홍보사이트에서 홍보되는 불법사이트 URL을 수집해 엑셀로 정리하는 봇 (24시간 운영)"

if ($AtStartup) {
    Write-Host "로그온 없이 실행하려면 이 계정의 비밀번호가 필요합니다."
    $credential = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
        -Message "봇을 실행할 계정의 비밀번호"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description `
        -User $credential.UserName `
        -Password $credential.GetNetworkCredential().Password `
        -RunLevel Limited -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -Force | Out-Null
}

Write-Host ""
Write-Host "등록 완료: $TaskName"
Write-Host "  실행 파일 : $python bot.py start"
Write-Host "  작업 폴더 : $root"
Write-Host "  시작 시점 : $(if ($AtStartup) { '부팅 시 (로그온 불필요)' } else { '로그온 시' })"
Write-Host ""
Write-Host "지금 바로 시작하려면 : Start-ScheduledTask -TaskName $TaskName"
Write-Host "상태 확인            : python bot.py status"
Write-Host "수집 ON/OFF          : python bot.py on / python bot.py off"
Write-Host "등록 해제            : .\install-startup-task.ps1 -Remove"
