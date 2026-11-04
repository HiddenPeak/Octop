@echo off
setlocal EnableDelayedExpansion

REM inhere-box Installer for Windows (cmd.exe)
REM Usage: install.bat [-Version X.Y.Z] [-FromSource] [-SourceDir DIR] [-Extras browser] [-Help]

if not defined INHERE_BOX_HOME if defined OCTOP_HOME set "INHERE_BOX_HOME=%OCTOP_HOME%"
if not defined INHERE_BOX_HOME (
    set "INHERE_BOX_HOME=%USERPROFILE%\.inhere-box"
    if not exist "%USERPROFILE%\.inhere-box" if exist "%USERPROFILE%\.octop" set "INHERE_BOX_HOME=%USERPROFILE%\.octop"
)
set "INHERE_BOX_VENV=%INHERE_BOX_HOME%\venv"
set "INHERE_BOX_BIN=%INHERE_BOX_HOME%\bin"
set "PYTHON_VERSION=3.12"
if defined INHERE_BOX_REPO (set "INHERE_BOX_REPO=%INHERE_BOX_REPO%") else (set "INHERE_BOX_REPO=https://github.com/TencentCloud/Octop.git")

set "ARG_VERSION="
set "ARG_FROM_SOURCE=0"
set "ARG_SOURCE_DIR="
set "ARG_EXTRAS="
set "ARG_UV_PATH="
set "CONSOLE_AVAILABLE=0"

:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="-Version"    (set "ARG_VERSION=%~2" & shift & shift & goto :parse_args)
if /i "%~1"=="-FromSource" (set "ARG_FROM_SOURCE=1" & shift & goto :parse_args)
if /i "%~1"=="-SourceDir"  (set "ARG_SOURCE_DIR=%~2" & shift & shift & goto :parse_args)
if /i "%~1"=="-Extras"     (set "ARG_EXTRAS=%~2" & shift & shift & goto :parse_args)
if /i "%~1"=="-UvPath"     (set "ARG_UV_PATH=%~2" & shift & shift & goto :parse_args)
if /i "%~1"=="-Help"       goto :show_help
shift
goto :parse_args

:show_help
echo inhere-box Installer for Windows
echo.
echo Usage: install.bat [OPTIONS]
echo   -Version ^<VER^>     Install specific version
echo   -FromSource        Install from source
echo   -SourceDir ^<DIR^>   Local source directory
echo   -Extras ^<EXTRAS^>  Extra components (e.g. desktop, browser)
echo   -UvPath ^<PATH^>     Pre-installed uv.exe
echo.
echo   Note: Playwright Chromium is not downloaded by default. Pass -Extras browser
echo         to install it, or install later from the dashboard.
exit /b 0

:done_args
echo [inhere-box] Installing inhere-box into %INHERE_BOX_HOME%

call :ensure_uv
if errorlevel 1 exit /b 1

if exist "%INHERE_BOX_VENV%" (
    echo [inhere-box] Existing environment found, upgrading...
) else (
    echo [inhere-box] Creating Python %PYTHON_VERSION% environment...
)

uv venv "%INHERE_BOX_VENV%" --python %PYTHON_VERSION% --quiet --clear
if errorlevel 1 (echo [inhere-box] ERROR: Failed to create venv & exit /b 1)

set "VENV_PYTHON=%INHERE_BOX_VENV%\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (echo [inhere-box] ERROR: venv python missing & exit /b 1)

for /f "delims=" %%v in ('"%VENV_PYTHON%" --version 2^>^&1') do set "PY_VERSION=%%v"
echo [inhere-box] Python environment ready (%PY_VERSION%)

set "EXTRAS_SUFFIX=[browser]"
if defined ARG_EXTRAS set "EXTRAS_SUFFIX=[browser,%ARG_EXTRAS%]"
set "VENV_INHERE_BOX=%INHERE_BOX_VENV%\Scripts\inhere-box.exe"

if "%ARG_FROM_SOURCE%"=="1" goto :install_from_source
goto :install_from_pypi

:install_from_source
if defined ARG_SOURCE_DIR goto :install_from_local
goto :install_from_git

:install_from_local
for %%I in ("%ARG_SOURCE_DIR%") do set "ARG_SOURCE_DIR=%%~fI"
echo [inhere-box] Installing from local source: %ARG_SOURCE_DIR%
call :prepare_console "%ARG_SOURCE_DIR%"
uv pip install "%ARG_SOURCE_DIR%%EXTRAS_SUFFIX%" --python "%VENV_PYTHON%"
if errorlevel 1 (echo [inhere-box] ERROR: install failed & exit /b 1)
goto :install_verify

:install_from_git
where git >nul 2>&1
if errorlevel 1 (echo [inhere-box] ERROR: git required & exit /b 1)
set "CLONE_DIR=%TEMP%\inhere-box-install-%RANDOM%"
git clone --depth 1 %INHERE_BOX_REPO% "%CLONE_DIR%"
if errorlevel 1 exit /b 1
call :prepare_console "%CLONE_DIR%"
uv pip install "%CLONE_DIR%%EXTRAS_SUFFIX%" --python "%VENV_PYTHON%"
set "_E=%errorlevel%"
if exist "%CLONE_DIR%" rd /s /q "%CLONE_DIR%"
if %_E% neq 0 exit /b 1
goto :install_verify

:install_from_pypi
REM PEP 508: extras must sit between name and version specifier
REM (inhere-box[browser]==x.y.z); inhere-box==x.y.z[browser] is an invalid requirement.
set "_PACKAGE=inhere-box%EXTRAS_SUFFIX%"
if defined ARG_VERSION set "_PACKAGE=inhere-box%EXTRAS_SUFFIX%==%ARG_VERSION%"
echo [inhere-box] Installing %_PACKAGE% from PyPI...
uv pip install "%_PACKAGE%" --python "%VENV_PYTHON%"
if errorlevel 1 (echo [inhere-box] ERROR: install failed & exit /b 1)

:install_verify
call :verify_install
if errorlevel 1 exit /b 1
if not exist "%VENV_INHERE_BOX%" (echo [inhere-box] ERROR: inhere-box CLI not found & exit /b 1)
echo [inhere-box] inhere-box installed successfully

if "%CONSOLE_AVAILABLE%"=="0" (
    "%VENV_PYTHON%" -c "import importlib.resources, inhere_box; p=importlib.resources.files('inhere_box')/'dashboard'/'index.html'; print('yes' if p.is_file() else 'no')" > "%TEMP%\_inhere_box_ui.tmp" 2>&1
    set /p _UI=<"%TEMP%\_inhere_box_ui.tmp"
    del "%TEMP%\_inhere_box_ui.tmp" >nul 2>&1
    if "!_UI!"=="yes" set "CONSOLE_AVAILABLE=1"
)

echo ",%ARG_EXTRAS%," | findstr /i /c:",browser," >nul
if errorlevel 1 (
    echo [inhere-box] Skipping Playwright Chromium download.
    echo [inhere-box] For remote-browser automation, re-run with -Extras browser, use the dashboard, or:
    echo [inhere-box]   "%VENV_PYTHON%" -m playwright install chromium
    goto :after_browser
)
call :detect_chrome
if defined INHERE_BOX_SYSTEM_CHROME (
    echo [inhere-box] Found system Chrome/Chromium: %INHERE_BOX_SYSTEM_CHROME%
    echo [inhere-box] Using system browser; skipping Playwright Chromium download.
    echo [inhere-box] To use bundled Chromium instead, run: "%VENV_PYTHON%" -m playwright install chromium
    goto :after_browser
)
echo [inhere-box] Installing Playwright Chromium browser...
"%VENV_PYTHON%" -m playwright install chromium
if errorlevel 1 (
    echo [inhere-box] WARNING: Playwright install failed. Run later: "%VENV_PYTHON%" -m playwright install chromium
)
:after_browser

if not exist "%INHERE_BOX_BIN%" mkdir "%INHERE_BOX_BIN%"

echo @echo off > "%INHERE_BOX_BIN%\inhere-box.cmd"
echo set "INHERE_BOX_HOME=%%INHERE_BOX_HOME%%" >> "%INHERE_BOX_BIN%\inhere-box.cmd"
echo if "%%INHERE_BOX_HOME%%"=="" set "INHERE_BOX_HOME=%%OCTOP_HOME%%" >> "%INHERE_BOX_BIN%\inhere-box.cmd"
echo if "%%INHERE_BOX_HOME%%"=="" for %%%%I in ("%%~dp0..") do set "INHERE_BOX_HOME=%%%%~fI" >> "%INHERE_BOX_BIN%\inhere-box.cmd"
echo "%%INHERE_BOX_HOME%%\venv\Scripts\inhere-box.exe" %%* >> "%INHERE_BOX_BIN%\inhere-box.cmd"

set "INHERE_BOX_BIN_FOR_PS=%INHERE_BOX_BIN%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$bin=$env:INHERE_BOX_BIN_FOR_PS; $cur=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $cur){$cur=''}; $parts=$cur.Split(';')|?{$_}; $ok=$false; foreach($p in $parts){if($p.TrimEnd('\') -ieq $bin.TrimEnd('\')){$ok=$true}}; if(-not $ok){$new=if($cur){$cur.TrimEnd(';')+';'+$bin}else{$bin}; [Environment]::SetEnvironmentVariable('Path',$new,'User'); Write-Host '[inhere-box] Added to User PATH'} else {Write-Host '[inhere-box] Already in User PATH'}"
set "INHERE_BOX_BIN_FOR_PS="
echo %PATH% | findstr /i /c:"%INHERE_BOX_BIN%" >nul || set "PATH=%INHERE_BOX_BIN%;%PATH%"

echo.
echo inhere-box installed successfully!
echo   Location: %INHERE_BOX_HOME%
echo   Python:   %PY_VERSION%
echo.
echo If 'inhere-box' is not found in this session, run:
echo   set PATH=%INHERE_BOX_BIN%;%%PATH%%
echo Or open a new terminal ^(saved to User PATH^).
echo.
echo Then run:
echo   inhere-box init
echo   inhere-box run
echo   inhere-box service start
echo   http://127.0.0.1:8088
exit /b 0

::detect_chrome
set "INHERE_BOX_SYSTEM_CHROME="
REM Prefer octop-browser's own detector (same path used at runtime)
"%VENV_PYTHON%" -c "from octop_browser.cdp.launcher import find_chrome; p=find_chrome(); print(p or '', end='')" > "%TEMP%\_inhere_box_chrome.tmp" 2>nul
if not errorlevel 1 (
    set /p _CHROME=<"%TEMP%\_inhere_box_chrome.tmp"
)
del "%TEMP%\_inhere_box_chrome.tmp" >nul 2>&1
if defined _CHROME (
    set "INHERE_BOX_SYSTEM_CHROME=%_CHROME%"
    goto :detect_chrome_done
)
REM Fallback: common commands on PATH
for %%c in (chrome google-chrome google-chrome-stable chromium chromium-browser) do (
    where %%c >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('where %%c') do (set "INHERE_BOX_SYSTEM_CHROME=%%p" & goto :detect_chrome_done)
    )
)
REM Fallback: well-known GUI install paths
for %%p in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles%\Chromium\Application\chrome.exe"
) do (
    if exist %%p (set "INHERE_BOX_SYSTEM_CHROME=%%~p" & goto :detect_chrome_done)
)
:detect_chrome_done
exit /b 0

:ensure_uv
if defined ARG_UV_PATH (
    if not exist "%ARG_UV_PATH%" (echo [inhere-box] ERROR: uv not found & exit /b 1)
    for %%I in ("%ARG_UV_PATH%") do set "PATH=%%~dpI;!PATH!"
    goto :ensure_uv_done
)
where uv >nul 2>&1
if not errorlevel 1 goto :ensure_uv_done
for %%c in ("%USERPROFILE%\.local\bin\uv.exe" "%LOCALAPPDATA%\uv\uv.exe") do (
    if exist %%c (set "PATH=%%~dpc;!PATH!" & goto :ensure_uv_done)
)
echo [inhere-box] Installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 -TimeoutSec 15 | iex" 2>nul
if not errorlevel 1 goto :ensure_uv_refresh
echo [inhere-box] astral.sh failed, trying GitHub...
call :download_uv_github
if errorlevel 1 exit /b 1
goto :ensure_uv_done

:ensure_uv_refresh
for %%p in ("%USERPROFILE%\.local\bin" "%LOCALAPPDATA%\uv") do (
    if exist %%p echo "!PATH!" | findstr /i /c:"%%~p" >nul || set "PATH=%%~p;!PATH!"
)
where uv >nul 2>&1 || (echo [inhere-box] ERROR: uv install failed & exit /b 1)
:ensure_uv_done
exit /b 0

:download_uv_github
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" (set "_A=aarch64") else (set "_A=x86_64")
set "_Z=%TEMP%\uv-%RANDOM%.zip"
set "_D=%LOCALAPPDATA%\uv"
curl -L -o "!_Z!" "https://github.com/astral-sh/uv/releases/latest/download/uv-!_A!-pc-windows-msvc.zip" 2>nul
if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-!_A!-pc-windows-msvc.zip' -OutFile '!_Z!' -UseBasicParsing"
if not exist "!_D!" mkdir "!_D!"
powershell -NoProfile -Command "Expand-Archive -Force '!_Z!' '!_D!'"
del "!_Z!" >nul 2>&1
set "PATH=!_D!;!PATH!"
exit /b 0

:prepare_console
set "_RD=%~1"
set "_DEST=%_RD%\src\inhere_box\dashboard"
if exist "%_DEST%\index.html" (set "CONSOLE_AVAILABLE=1" & exit /b 0)
if not exist "%_RD%\dashboard\package.json" exit /b 0
where npm >nul 2>&1 || exit /b 0
echo [inhere-box] Building dashboard...
pushd "%_RD%\dashboard"
npm ci && npm run build
popd
if exist "%_DEST%\index.html" set "CONSOLE_AVAILABLE=1"
exit /b 0

:verify_install
echo [inhere-box] Verifying installation...
"%VENV_PYTHON%" -c "from inhere_box.infra.agents.manager import AgentManager" >nul 2>&1
if errorlevel 1 (echo [inhere-box] ERROR: install verification failed & exit /b 1)
echo [inhere-box] Install verification passed
exit /b 0
