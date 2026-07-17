@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0"

if not defined GAS_DIAGNOSIS_HOST set "GAS_DIAGNOSIS_HOST=0.0.0.0"
if not defined GAS_DIAGNOSIS_PORT set "GAS_DIAGNOSIS_PORT=8080"
if not defined GAS_DATA_DIR set "GAS_DATA_DIR=%CD%\runtime_data"

if not defined DEEPSEEK_API_KEY if exist "deepseek_api_key.txt" (
  set /p DEEPSEEK_API_KEY=<"deepseek_api_key.txt"
)

if not defined DEEPSEEK_MODEL if exist "deepseek_model.txt" (
  set /p DEEPSEEK_MODEL=<"deepseek_model.txt"
)

if not defined DEEPSEEK_MODEL set "DEEPSEEK_MODEL=deepseek-v4-flash"

set "PYTHON_EXE="
set "PYTHON_ARGS="

if defined GAS_DIAGNOSIS_PYTHON (
  set "PYTHON_EXE=%GAS_DIAGNOSIS_PYTHON%"
)

if not defined PYTHON_EXE if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
  set "CODEX_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  if exist "!CODEX_PY!" set "PYTHON_EXE=!CODEX_PY!"
)

if not defined PYTHON_EXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
  )
)

if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
  echo Python was not found. Please install Python 3.10 or later.
  pause
  exit /b 1
)

echo Using Python: "!PYTHON_EXE!" !PYTHON_ARGS!
echo Checking dependencies...
"!PYTHON_EXE!" !PYTHON_ARGS! -c "import pandas, numpy, openpyxl, sklearn, flask, waitress" >nul 2>nul
if errorlevel 1 (
  echo Missing dependencies. Trying to install from requirements.txt...
  "!PYTHON_EXE!" !PYTHON_ARGS! -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Dependency installation failed.
    echo If the network is unavailable, set GAS_DIAGNOSIS_PYTHON to a Python environment with all requirements installed.
    echo Example:
    echo   set GAS_DIAGNOSIS_PYTHON=C:\Path\To\python.exe
    pause
    exit /b 1
  )
)

if not exist "models\baseline_healthy.json" (
  echo models\baseline_healthy.json was not found. The diagnosis service cannot start.
  pause
  exit /b 1
)

echo Checking PDF renderer...
"!PYTHON_EXE!" !PYTHON_ARGS! -c "from gas_diagnosis.pdf_report import find_chromium; print('PDF renderer: ' + str(find_chromium()))"
if errorlevel 1 (
  echo Chromium, Chrome, or Microsoft Edge was not found. PDF reports cannot be generated.
  echo Install a supported browser or set GAS_CHROMIUM_PATH to its executable file.
  pause
  exit /b 1
)

echo.
if not exist "!GAS_DATA_DIR!" mkdir "!GAS_DATA_DIR!"

echo Starting gas regulator diagnosis production service...
echo Local URL: http://127.0.0.1:!GAS_DIAGNOSIS_PORT!/
echo Listen: !GAS_DIAGNOSIS_HOST!:!GAS_DIAGNOSIS_PORT!
echo Data directory: !GAS_DATA_DIR!
if defined DEEPSEEK_API_KEY (
  echo DeepSeek analysis: enabled, model !DEEPSEEK_MODEL!
) else (
  echo DeepSeek analysis: not configured, local template fallback will be used.
)
echo Close this window to stop the service.
echo.

"!PYTHON_EXE!" !PYTHON_ARGS! -m gas_diagnosis.production

echo.
echo Service stopped.
pause
