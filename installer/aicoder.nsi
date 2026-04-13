; aicoder NSIS Installer
; Output: installer/aicoder-setup.exe
; Requires: EnVar plugin (PATH management)
; Build: makensis installer/aicoder.nsi

Unicode True

!define APPNAME    "aicoder"
!define APPVERSION "0.7.0"
!define PUBLISHER  "Markus Leitermann / AILinux"
!define APPDIR     "$PROGRAMFILES64\${APPNAME}"
!define REGKEY     "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPNAME} ${APPVERSION}"
OutFile "aicoder-setup.exe"
InstallDir "${APPDIR}"
InstallDirRegKey HKLM "${REGKEY}" "InstallLocation"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

; ── Seiten ──────────────────────────────────────────────────────
Page directory
Page instfiles

UninstPage uninstConfirm
UninstPage instfiles

; ── Install ─────────────────────────────────────────────────────
Section "Install"
  SetOutPath "$INSTDIR"

  ; Executable kopieren
  File "aicoder.exe"

  ; Uninstaller schreiben
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Registry-Einträge (Programme & Features)
  WriteRegStr   HKLM "${REGKEY}" "DisplayName"     "${APPNAME} ${APPVERSION}"
  WriteRegStr   HKLM "${REGKEY}" "DisplayVersion"  "${APPVERSION}"
  WriteRegStr   HKLM "${REGKEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKLM "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${REGKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKLM "${REGKEY}" "NoModify"        1
  WriteRegDWORD HKLM "${REGKEY}" "NoRepair"        1

  ; PATH erweitern (System-weit, dauerhaft)
  EnVar::SetHKLM
  EnVar::AddValue "PATH" "$INSTDIR"
  Pop $0
  DetailPrint "PATH update: $0"

  ; Hinweis: neues Terminal nötig damit PATH greift
  MessageBox MB_OK \
    "aicoder ${APPVERSION} wurde installiert.$\n$\n\
Installationspfad: $INSTDIR$\n$\n\
Öffne ein neues PowerShell-Fenster und starte:$\n\
  aicoder --help"
SectionEnd

; ── Uninstall ────────────────────────────────────────────────────
Section "Uninstall"
  Delete "$INSTDIR\aicoder.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir  "$INSTDIR"

  DeleteRegKey HKLM "${REGKEY}"

  EnVar::SetHKLM
  EnVar::DeleteValue "PATH" "$INSTDIR"
  Pop $0
  DetailPrint "PATH cleanup: $0"
SectionEnd
