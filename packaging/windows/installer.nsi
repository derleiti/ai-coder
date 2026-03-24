; AI-Coder Windows Installer (NSIS) — minimal, robust
!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "0.3.8"
!endif
!ifndef VERSIONINFO
  !define VERSIONINFO "0.3.8.0"
!endif
!ifndef EXE_PATH
  !define EXE_PATH "dist\aicoder.exe"
!endif

Name "AI-Coder ${VERSION}"
OutFile "aicoder-${VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\aicoder"
InstallDirRegKey HKLM "Software\AILinux\ai-coder" "InstallDir"
RequestExecutionLevel admin

VIProductVersion "${VERSIONINFO}"
VIAddVersionKey "ProductName" "AI-Coder"
VIAddVersionKey "CompanyName" "AILinux"
VIAddVersionKey "FileDescription" "Terminal Coding and DevOps Agent"
VIAddVersionKey "FileVersion" "${VERSION}"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath $INSTDIR
  File "${EXE_PATH}"
  File "packaging\windows\add-to-path.ps1"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKLM "Software\AILinux\ai-coder" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\AILinux\ai-coder" "Version" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "DisplayName" "AI-Coder ${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "DisplayIcon" "$INSTDIR\aicoder.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "Publisher" "AILinux"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" "NoRepair" 1

  ; Run PATH setup via PowerShell
  ExecWait 'powershell.exe -ExecutionPolicy Bypass -File "$INSTDIR\add-to-path.ps1" -InstallDir "$INSTDIR"'
SectionEnd

Section "Uninstall"
  ExecWait 'powershell.exe -ExecutionPolicy Bypass -Command "$$p = [Environment]::GetEnvironmentVariable(\"Path\",\"Machine\"); $$c = ($$p -split \";\" | Where-Object { $$_ -ne \"$INSTDIR\" }) -join \";\"; [Environment]::SetEnvironmentVariable(\"Path\",$$c,\"Machine\")"'
  Delete "$INSTDIR\aicoder.exe"
  Delete "$INSTDIR\add-to-path.ps1"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\AILinux\ai-coder"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder"
SectionEnd
