; ai-coder Windows Installer — NSIS
; Desktop + Start Menu Shortcuts für CLI und GUI

Unicode True

!define APP_NAME "ai-coder"
!define APP_EXE  "aicoder.exe"
!define INSTALL_DIR "$PROGRAMFILES64\ai-coder"

Name "${APP_NAME} ${VERSION}"
OutFile "aicoder-${VERSION}-setup.exe"
InstallDir "${INSTALL_DIR}"
InstallDirRegKey HKLM "Software\ai-coder" "Install_Dir"
RequestExecutionLevel admin
ShowInstDetails show

; --- Pages ---
Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; --- Install ---
Section "ai-coder (required)"
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "${EXE_PATH}"

  ; PATH eintragen
  EnVar::SetHKLM
  EnVar::AddValue "PATH" "$INSTDIR"

  ; Start Menu Shortcuts
  CreateDirectory "$SMPROGRAMS\ai-coder"
  CreateShortcut "$SMPROGRAMS\ai-coder\ai-coder Terminal.lnk" \
    "$INSTDIR\aicoder.exe" "" "$INSTDIR\aicoder.exe" 0 SW_SHOW
  CreateShortcut "$SMPROGRAMS\ai-coder\ai-coder GUI.lnk" \
    "$INSTDIR\aicoder.exe" "gui" "$INSTDIR\aicoder.exe" 0 SW_HIDE
  CreateShortcut "$SMPROGRAMS\ai-coder\Deinstallieren.lnk" \
    "$INSTDIR\uninstall.exe" "" "" 0

  ; Desktop Shortcuts
  CreateShortcut "$DESKTOP\ai-coder GUI.lnk" \
    "$INSTDIR\aicoder.exe" "gui" "$INSTDIR\aicoder.exe" 0 SW_HIDE
  CreateShortcut "$DESKTOP\ai-coder Terminal.lnk" \
    "$INSTDIR\aicoder.exe" "" "$INSTDIR\aicoder.exe" 0 SW_SHOW

  ; Registry
  WriteRegStr HKLM "Software\ai-coder" "Install_Dir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "DisplayName" "ai-coder"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "Publisher" "AILinux / Markus Leitermann"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder" \
    "URLInfoAbout" "https://ailinux.me"

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

; --- Uninstall ---
Section "Uninstall"
  EnVar::SetHKLM
  EnVar::DeleteValue "PATH" "$INSTDIR"

  Delete "$INSTDIR\aicoder.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  Delete "$SMPROGRAMS\ai-coder\ai-coder Terminal.lnk"
  Delete "$SMPROGRAMS\ai-coder\ai-coder GUI.lnk"
  Delete "$SMPROGRAMS\ai-coder\Deinstallieren.lnk"
  RMDir "$SMPROGRAMS\ai-coder"

  Delete "$DESKTOP\ai-coder GUI.lnk"
  Delete "$DESKTOP\ai-coder Terminal.lnk"

  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ai-coder"
  DeleteRegKey HKLM "Software\ai-coder"
SectionEnd
