; ============================================================
; AI-Coder Windows Installer (NSIS)
; - Installiert aicoder.exe nach C:\Program Files\aicoder
; - PATH via PowerShell (robust, kein Plugin nötig)
; - Sauberer Uninstaller mit Add/Remove Programs
; ============================================================

!include "MUI2.nsh"

; ── Defines (überschreibbar via makensis /D) ────────────────
!ifndef VERSION
  !define VERSION "0.3.2"
!endif
!ifndef VERSIONINFO
  !define VERSIONINFO "0.3.2.0"
!endif
!ifndef EXE_PATH
  !define EXE_PATH "dist\aicoder.exe"
!endif

; ── Installer-Metadaten ─────────────────────────────────────
Name "AI-Coder ${VERSION}"
OutFile "aicoder-${VERSION}-setup.exe"
InstallDir "$PROGRAMFILES64\aicoder"
InstallDirRegKey HKLM "Software\AILinux\ai-coder" "InstallDir"
RequestExecutionLevel admin

VIProductVersion "${VERSIONINFO}"
VIAddVersionKey "ProductName" "AI-Coder"
VIAddVersionKey "CompanyName" "AILinux"
VIAddVersionKey "LegalCopyright" "2026 AILinux / Markus Leitermann"
VIAddVersionKey "FileDescription" "Terminal Coding & DevOps Agent"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

; ── UI-Seiten ───────────────────────────────────────────────
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "German"

; ============================================================
; INSTALL
; ============================================================
Section "AI-Coder" SecMain
  SectionIn RO
  SetOutPath $INSTDIR

  ; ── Binary ────────────────────────────────────────────────
  File "${EXE_PATH}"

  ; ── Uninstaller ───────────────────────────────────────────
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; ── Registry: App-Info ────────────────────────────────────
  WriteRegStr HKLM "Software\AILinux\ai-coder" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\AILinux\ai-coder" "Version" "${VERSION}"

  ; ── Registry: Add/Remove Programs ─────────────────────────
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "DisplayName" "AI-Coder ${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "DisplayIcon" "$INSTDIR\aicoder.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "Publisher" "AILinux"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "URLInfoAbout" "https://ailinux.me/ai-coder/"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "NoRepair" 1

  ; ── PATH hinzufügen (PowerShell — robust) ─────────────────
  nsExec::ExecToLog 'powershell -ExecutionPolicy Bypass -Command "\
    $$machPath = [Environment]::GetEnvironmentVariable(\"Path\", \"Machine\"); \
    if ($$machPath -notlike \"*$INSTDIR*\") { \
      [Environment]::SetEnvironmentVariable(\"Path\", \"$$machPath;$INSTDIR\", \"Machine\") \
    }"'

SectionEnd

; ============================================================
; UNINSTALL
; ============================================================
Section "Uninstall"

  ; ── PATH entfernen ────────────────────────────────────────
  nsExec::ExecToLog 'powershell -ExecutionPolicy Bypass -Command "\
    $$machPath = [Environment]::GetEnvironmentVariable(\"Path\", \"Machine\"); \
    $$cleaned = ($$machPath -split \";\" | Where-Object { $$_ -ne \"$INSTDIR\" }) -join \";\"; \
    [Environment]::SetEnvironmentVariable(\"Path\", $$cleaned, \"Machine\")"'

  ; ── Dateien ───────────────────────────────────────────────
  Delete "$INSTDIR\aicoder.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  ; ── Registry ──────────────────────────────────────────────
  DeleteRegKey HKLM "Software\AILinux\ai-coder"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder"

SectionEnd
